"""Docker Provider for ordinary Harbor images that do not embed ALE guestd."""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import time
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Literal

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    Capabilities,
    ExecOutputSink,
    ExecResult,
    GpuAllocation,
    Identity,
    ImageKind,
    ImageRef,
    PreparedTaskImage,
    ResolvedImage,
    ResourceAllocation,
    RetainedSandbox,
    Sandbox,
    SandboxRequest,
    SandboxState,
)
from ale.core.taskspec import NetworkMode
from ale.run.gpu import GpuLease
from ale.run.images import resolve_container_image, resolve_prepared_container_image
from ale.run.providers.docker import (
    GPU_LABEL,
    HANDLE_PREFIX,
    HOST_ALIAS,
    LABEL,
    MANAGED_LABEL,
    RETENTION_LABEL,
    ROLE_LABEL,
    DockerProvider,
    _docker,
    _gpu_run_args,
    _managed_gpu_ids,
    _proxy_env,
    _reachable,
    _storage_run_args,
    _verify_docker_gpu,
)

__all__ = ["HarborDockerProvider", "HarborDockerSandbox"]

_OUTPUT_LIMIT = 4 * 1024 * 1024


class HarborDockerSandbox(Sandbox):
    """A Harbor container operated directly through ``docker exec`` and ``docker cp``."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        request: SandboxRequest,
        container: str,
        network: str | None,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        resource_lease: GpuLease | None,
        agent_user: str,
        workdir: str,
    ) -> None:
        super().__init__(
            sandbox_id=sandbox_id,
            request=request,
            resolved_image=resolved_image,
            allocation=allocation,
            resource_lease=resource_lease,
        )
        self.container = container
        self.network = network
        self.agent_user = agent_user
        self.workdir = workdir
        self._sealed = False
        self.state = SandboxState.READY

    @property
    def gateway_url(self) -> str | None:
        return _reachable(self.request.gateway_url)

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        identity: Identity = Identity.FRAMEWORK,
        output_sink: ExecOutputSink | None = None,
    ) -> ExecResult:
        effective_env = dict(env or {})
        if (
            identity is Identity.AGENT
            and self._sealed
            and self.request.network.mode is NetworkMode.ALLOWLIST
        ):
            effective_env = _proxy_env(self.request) | effective_env
        command = ["docker", "exec"]
        if identity is Identity.FRAMEWORK:
            command += ["--user", "0"]
        if cwd or self.workdir:
            command += ["--workdir", cwd or self.workdir]
        for key, value in effective_env.items():
            command += ["--env", f"{key}={value}"]
        command += [self.container, *argv]
        return await _exec(command, timeout_sec=timeout_sec, output_sink=output_sink)

    async def write_file(
        self,
        path: PurePosixPath | str,
        data: bytes,
        *,
        identity: Identity = Identity.FRAMEWORK,
    ) -> None:
        target = str(path)
        parent = str(PurePosixPath(target).parent)
        code, _, stderr = await _docker(
            "exec", "--user", "0", self.container, "mkdir", "-p", parent
        )
        if code != 0:
            raise ProviderStartError(f"could not create {parent}: {stderr.strip()}")
        with tempfile.NamedTemporaryFile() as source:
            source.write(data)
            source.flush()
            code, _, stderr = await _docker("cp", source.name, f"{self.container}:{target}")
        if code != 0:
            raise ProviderStartError(f"write to {target} failed: {stderr.strip()}")
        await self._give_to_agent(target, identity)

    async def read_file(self, path: PurePosixPath | str) -> bytes:
        code, stdout, stderr = await _docker_bytes(
            "exec", "--user", "0", self.container, "cat", str(path), timeout=120
        )
        if code != 0:
            raise ProviderStartError(f"read from {path} failed: {stderr.decode(errors='replace')}")
        return stdout

    async def upload_dir(
        self,
        source: str,
        target: PurePosixPath | str,
        *,
        identity: Identity = Identity.FRAMEWORK,
    ) -> None:
        code, _, stderr = await _docker(
            "exec", "--user", "0", self.container, "mkdir", "-p", str(target)
        )
        if code != 0:
            raise ProviderStartError(f"could not create {target}: {stderr.strip()}")
        code, _, stderr = await _docker("cp", f"{Path(source)}/.", f"{self.container}:{target}")
        if code != 0:
            raise ProviderStartError(f"upload to {target} failed: {stderr.strip()}")
        await self._give_to_agent(str(target), identity, recursive=True)

    async def download_dir(self, source: PurePosixPath | str, target: str) -> None:
        Path(target).mkdir(parents=True, exist_ok=True)
        code, _, stderr = await _docker("cp", f"{self.container}:{source}/.", target)
        if code != 0:
            raise ProviderStartError(f"download from {source} failed: {stderr.strip()}")

    async def open_egress(self) -> None:
        if self.network is None:
            return
        code, _, stderr = await _docker("network", "connect", "bridge", self.container)
        if code != 0 and "already exists" not in stderr:
            raise ProviderStartError(f"could not open egress: {stderr.strip()}")
        self._sealed = False

    async def close_egress(self) -> None:
        if self.network is None:
            return
        code, _, stderr = await _docker("network", "disconnect", "bridge", self.container)
        if code != 0 and "is not connected" not in stderr:
            raise ProviderStartError(f"could not close egress: {stderr.strip()}")
        self._sealed = True

    async def destroy(self) -> None:
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED
        try:
            await _docker("rm", "-f", "-v", self.container, timeout=60)
            if self.network:
                await _docker("network", "rm", self.network, timeout=60)
        finally:
            self.release_resources()

    async def retain(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        devices = self.allocation.gpu.provider_addresses if self.allocation.gpu else ()
        handle = f"{HANDLE_PREFIX}{self.container}"
        return RetainedSandbox(
            provider="harbor-docker",
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"ale sandbox destroy {handle}",
            gpu_devices=devices,
        )

    async def _give_to_agent(
        self, path: str, identity: Identity, *, recursive: bool = False
    ) -> None:
        if identity is not Identity.AGENT or self.agent_user in {"", "0", "root"}:
            return
        argv = ["exec", "--user", "0", self.container, "chown"]
        if recursive:
            argv.append("-R")
        argv += [self.agent_user, path]
        code, _, stderr = await _docker(*argv)
        if code != 0:
            raise ProviderStartError(
                f"could not give {path} to {self.agent_user}: {stderr.strip()}"
            )


class HarborDockerProvider(DockerProvider):
    """Run Harbor's ordinary Linux images without imposing ALE's image labels."""

    name = "harbor-docker"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            os="linux",
            gui=False,
            network_modes=frozenset({NetworkMode.BLOCK, NetworkMode.ALLOWLIST, NetworkMode.OPEN}),
        )

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if image.kind is not ImageKind.CONTAINER:
            raise ProviderCapabilityError("HarborDockerProvider requires image.kind=container")
        prepared = await resolve_container_image(image) if isinstance(image, ImageRef) else image
        await resolve_prepared_container_image(prepared)
        return prepared

    async def create(self, request: SandboxRequest) -> Sandbox:
        self.accepts(request)
        if request.image_kind is not ImageKind.CONTAINER:
            raise ProviderCapabilityError("HarborDockerProvider requires image.kind=container")
        resolved = await resolve_prepared_container_image(request.prepared_image)
        sandbox_id = f"{request.episode_id}-{uuid.uuid4().hex[:6]}"
        container = f"ale-harbor-{sandbox_id}"
        network = await self._ensure_network(sandbox_id, request)
        lease: GpuLease | None = None
        selected: tuple[str, ...] = ()
        observed_gpu = ()

        argv = [
            "run",
            "-d",
            "--name",
            container,
            "--label",
            f"{LABEL}={request.episode_id}",
            "--label",
            f"{MANAGED_LABEL}=true",
            "--label",
            f"{ROLE_LABEL}={request.role.value}",
            "--label",
            f"{RETENTION_LABEL}={request.retention}",
            "--cpus",
            str(request.resources.cpus),
            "--memory",
            f"{request.resources.memory_mb}m",
        ]
        if network:
            argv += ["--network", network]
        for key, value in request.env.items():
            argv += ["--env", f"{key}={value}"]
        if request.gateway_url or request.proxy_url:
            host_ip = await self._bridge_host_ip(network)
            argv += ["--add-host", f"{HOST_ALIAS}:{host_ip}"]
            if request.gateway_url:
                argv += ["--env", f"ALE_GATEWAY_URL={_reachable(request.gateway_url)}"]
            if request.proxy_url:
                request = request.model_copy(
                    update={"proxy_url": _reachable(request.proxy_url, host=host_ip)}
                )
        if request.resources.gpus:
            host_devices = await self._host_gpus()
            configured = self.gpus or tuple(sorted(host_devices))
            candidates = tuple(host_devices[index].id for index in configured)
            available = tuple(key for key in candidates if key not in await _managed_gpu_ids())
            lease = GpuLease.acquire(available, request.resources.gpus, self.gpu_lock_dir)
            selected = lease.device_keys
            argv += _gpu_run_args(selected)
            argv += ["--label", f"{GPU_LABEL}={','.join(selected)}"]
        if request.resources.storage_mb is not None:
            argv += _storage_run_args(request.resources.storage_mb)
        argv += [resolved.observed_ref, "sh", "-c", "sleep infinity"]

        try:
            code, _, stderr = await _docker(*argv, timeout=300)
            if code != 0:
                raise ProviderStartError(f"could not start Harbor container: {stderr.strip()}")
            code, observed, stderr = await _docker(
                "inspect", "--format", "{{.Image}}", container, timeout=30
            )
            if code != 0 or observed.strip() != resolved.observed_identity:
                raise ProviderStartError(
                    "started Harbor image identity does not match the prepared image: "
                    f"{stderr.strip() or observed.strip()}"
                )
            agent_user, workdir = await _container_defaults(container)
            sandbox = HarborDockerSandbox(
                sandbox_id=sandbox_id,
                request=request,
                container=container,
                network=network,
                resolved_image=resolved,
                allocation=ResourceAllocation(
                    cpus=request.resources.cpus,
                    memory_mb=request.resources.memory_mb,
                    storage_mb=request.resources.storage_mb,
                    gpu=None,
                    sudo=False,
                    network_mode=request.network.mode,
                    provider=self.name,
                ),
                resource_lease=lease,
                agent_user=agent_user,
                workdir=workdir,
            )
            if selected:
                observed_gpu = await self._container_gpus_direct(sandbox)
                _verify_docker_gpu(selected, observed_gpu)
                runtime = await _docker("info", "--format", "{{.ServerVersion}}", timeout=30)
                sandbox.allocation = sandbox.allocation.model_copy(
                    update={
                        "gpu": GpuAllocation(
                            requested_count=request.resources.gpus,
                            provider_addresses=selected,
                            lease_keys=lease.device_keys if lease else (),
                            observed_devices=observed_gpu,
                            runtime_identity=(
                                f"docker-engine/{runtime[1].strip()}" if runtime[0] == 0 else None
                            ),
                        )
                    }
                )
        except BaseException:
            with contextlib.suppress(BaseException):
                await asyncio.shield(_docker("rm", "-f", "-v", container))
            if network:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(_docker("network", "rm", network))
            if lease:
                lease.release()
            raise
        if lease:
            lease.attach()
        return sandbox

    async def _container_gpus_direct(self, sandbox: HarborDockerSandbox):  # type: ignore[no-untyped-def]
        result = await sandbox.exec(
            [
                "nvidia-smi",
                "--query-gpu=uuid,name,pci.bus_id,driver_version",
                "--format=csv,noheader,nounits",
            ],
            timeout_sec=30,
        )
        if not result.ok:
            raise ProviderCapabilityError(
                f"Harbor GPU sandbox nvidia-smi failed: {result.stderr.strip()}"
            )
        from ale.run.gpu import parse_nvidia_smi

        return parse_nvidia_smi(result.stdout)


async def _container_defaults(container: str) -> tuple[str, str]:
    code, output, stderr = await _docker(
        "inspect",
        "--format",
        "{{json .Config.User}}|{{json .Config.WorkingDir}}",
        container,
        timeout=30,
    )
    if code != 0:
        raise ProviderStartError(f"could not inspect Harbor container defaults: {stderr.strip()}")
    raw_user, _, raw_workdir = output.strip().partition("|")
    user = json.loads(raw_user) or "root"
    workdir = json.loads(raw_workdir) or "/"
    return str(user), str(workdir)


async def _exec(
    argv: list[str],
    *,
    timeout_sec: float | None,
    output_sink: ExecOutputSink | None,
) -> ExecResult:
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False

    async def read(stream: asyncio.StreamReader, name: Literal["stdout", "stderr"]):
        captured = bytearray()
        truncated = False
        while chunk := await stream.read(65536):
            if output_sink is not None:
                await output_sink(name, chunk)
            remaining = _OUTPUT_LIMIT - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(captured), truncated

    async def communicate():
        assert process.stdout is not None
        assert process.stderr is not None
        async with asyncio.TaskGroup() as group:
            stdout_task = group.create_task(read(process.stdout, "stdout"))
            stderr_task = group.create_task(read(process.stderr, "stderr"))
            group.create_task(process.wait())
        return stdout_task.result(), stderr_task.result()

    try:
        if timeout_sec is None:
            stdout, stderr = await communicate()
        else:
            async with asyncio.timeout(timeout_sec):
                stdout, stderr = await communicate()
    except TimeoutError:
        timed_out = True
        process.kill()
        await process.wait()
        stdout = (b"", False)
        stderr = (b"", False)
    return ExecResult(
        exit_code=None if timed_out else process.returncode,
        stdout=stdout[0].decode("utf-8", "replace"),
        stderr=stderr[0].decode("utf-8", "replace"),
        duration_ms=int((time.monotonic() - started) * 1000),
        truncated=stdout[1] or stderr[1],
        timed_out=timed_out,
    )


async def _docker_bytes(*argv: str, timeout: float) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        "docker",
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ProviderStartError(f"docker {' '.join(argv[:2])} timed out") from None
    return process.returncode or 0, stdout, stderr
