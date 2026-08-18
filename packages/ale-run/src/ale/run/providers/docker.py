"""Container backend.

Shells out to the docker CLI rather than binding an SDK: the CLI is stable, already
installed wherever docker is, and keeps this provider readable.

Network isolation is topological, not rule-based. Each run gets its own
``--internal`` bridge, which has no route off the host, and the gateway is attached to
that bridge. Deny-all with a single controlled egress therefore falls out of the
network's shape — there are no firewall rules to survive a daemon restart, and nothing
inside the sandbox can lift them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import quote

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    Capabilities,
    ExecOutputSink,
    ExecResult,
    GpuAllocation,
    GpuDevice,
    Identity,
    ImageKind,
    ImageRef,
    PreparedTaskImage,
    Provider,
    ResolvedImage,
    ResourceAllocation,
    RetainedSandbox,
    Sandbox,
    SandboxRequest,
    SandboxState,
)
from ale.core.taskspec import NetworkMode
from ale.run.gpu import GpuLease, parse_nvidia_smi
from ale.run.images import resolve_container_image, resolve_prepared_container_image
from ale.run.sources import cache_root
from ale.run.transport import GuestClient, StdioTransport

__all__ = ["DockerProvider", "DockerSandbox", "destroy_retained", "list_retained"]

GUESTD_DIR = PurePosixPath("/opt/ale/guestd")

#: An image sets this to "true" when it starts a desktop, so provisioning waits for it.
GUI_LABEL = "ale.gui"

#: The unprivileged account an image intends the agent to be. Read rather than assumed:
#: the image chose the name, created the home directory and owns the desktop session.
USER_LABEL = "ale.user"

#: Used when an image declares nothing, so an image predating the contract still runs.
DEFAULT_AGENT_USER = "user"

LABEL = "ale.episode"
MANAGED_LABEL = "ale.managed"
ROLE_LABEL = "ale.role"
RETENTION_LABEL = "ale.retention"
GPU_LABEL = "ale.gpus"
HANDLE_PREFIX = "docker:"

#: The name a sandbox uses for its gateway. It is mapped to the host's address *on the
#: container's own bridge* rather than to docker's usual host-gateway: an ``--internal``
#: network has no default route at all, so the only address that works is one inside the
#: sandbox's own subnet. That is the provider's half of the deny-all bargain — no route
#: off the host, and exactly one destination still reachable.
HOST_ALIAS = "ale-gateway.internal"

_NVIDIA_QUERY = "uuid,name,pci.bus_id,driver_version"
_HOST_NVIDIA_QUERY = f"index,{_NVIDIA_QUERY}"


async def _managed_gpu_ids() -> set[str]:
    code, output, _ = await _docker(
        "ps",
        "--filter",
        f"label={MANAGED_LABEL}=true",
        "--format",
        f'{{{{.Label "{GPU_LABEL}"}}}}',
        timeout=30,
    )
    if code != 0:
        return set()
    return {device for line in output.splitlines() for device in line.split(",") if device}


async def list_retained() -> list[dict[str, object]]:
    code, output, stderr = await _docker(
        "ps",
        "-a",
        "-q",
        "--filter",
        f"label={MANAGED_LABEL}=true",
        "--filter",
        f"label={RETENTION_LABEL}=keep",
        timeout=30,
    )
    if code != 0:
        raise ProviderStartError(f"could not list ALE sandboxes: {stderr.strip()}")
    handles = output.split()
    if not handles:
        return []
    code, raw, stderr = await _docker("inspect", *handles, timeout=30)
    if code != 0:
        raise ProviderStartError(f"could not inspect ALE sandboxes: {stderr.strip()}")
    records = json.loads(raw)
    found: list[dict[str, object]] = []
    for record in records:
        labels = record.get("Config", {}).get("Labels", {}) or {}
        gpu = tuple(filter(None, labels.get(GPU_LABEL, "").split(",")))
        container = record.get("Name", "").removeprefix("/")
        handle = f"{HANDLE_PREFIX}{container}"
        found.append(
            {
                "provider": "docker",
                "handle": handle,
                "episode": labels.get(LABEL, ""),
                "role": labels.get(ROLE_LABEL, ""),
                "image": record.get("Image", ""),
                "gpus": gpu,
                "running": bool(record.get("State", {}).get("Running")),
                "cleanup_command": f"ale sandbox destroy {handle}",
            }
        )
    return sorted(found, key=lambda item: str(item["handle"]))


async def destroy_retained(handle: str) -> None:
    if not handle.startswith(HANDLE_PREFIX):
        raise ProviderCapabilityError(f"{handle!r} is not a Docker sandbox handle")
    container = handle.removeprefix(HANDLE_PREFIX)
    code, raw, stderr = await _docker("inspect", container, timeout=30)
    if code != 0:
        raise ProviderStartError(f"sandbox {handle!r} does not exist: {stderr.strip()}")
    record = json.loads(raw)[0]
    labels = record.get("Config", {}).get("Labels", {}) or {}
    if labels.get(MANAGED_LABEL) != "true" or labels.get(RETENTION_LABEL) != "keep":
        raise ProviderCapabilityError(f"{handle!r} is not an ALE-retained Docker sandbox")
    networks = tuple(
        name
        for name in (record.get("NetworkSettings", {}).get("Networks", {}) or {})
        if name.startswith("ale-net-")
    )
    code, _, stderr = await _docker("rm", "-f", "-v", container, timeout=60)
    if code != 0:
        raise ProviderStartError(f"could not destroy {handle}: {stderr.strip()}")
    for network in networks:
        await _docker("network", "rm", network, timeout=30)


async def _docker(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise ProviderStartError(
            f"docker {' '.join(argv[:2])} timed out after {timeout:g}s"
        ) from None
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def _command(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise ProviderStartError(f"{argv[0]} timed out after {timeout:g}s") from None
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


def _parse_nvidia_smi(output: str) -> tuple[GpuDevice, ...]:
    return parse_nvidia_smi(output)


def _parse_host_nvidia_smi(output: str) -> dict[int, GpuDevice]:
    devices: dict[int, GpuDevice] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            raw_index, device_row = line.split(",", 1)
            index = int(raw_index.strip())
        except ValueError as exc:
            raise ProviderCapabilityError(
                f"could not parse nvidia-smi GPU index: {line!r}"
            ) from exc
        if index < 0 or index in devices:
            raise ProviderCapabilityError(f"invalid or duplicate nvidia-smi GPU index: {index}")
        devices[index] = parse_nvidia_smi(device_row)[0]
    if not devices:
        raise ProviderCapabilityError("nvidia-smi reported no physical GPUs")
    return devices


def _gpu_run_args(uuids: tuple[str, ...]) -> list[str]:
    return [
        "--gpus",
        f"device={','.join(uuids)}",
        "--env",
        "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
    ]


def _storage_run_args(storage_mb: int) -> list[str]:
    return ["--storage-opt", f"size={storage_mb}M"]


def _proxy_env(request: SandboxRequest) -> dict[str, str]:
    if not request.proxy_url:
        return {}
    proxy = _reachable(request.proxy_url) or ""
    if request.proxy_token:
        proxy = proxy.replace("://", f"://{quote(request.proxy_token, safe='')}:@", 1)
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": f"{HOST_ALIAS},localhost,127.0.0.1",
    }


def _verify_docker_gpu(
    selected: tuple[str, ...], observed: tuple[GpuDevice, ...]
) -> tuple[GpuDevice, ...]:
    actual = tuple(sorted(device.id for device in observed))
    expected = tuple(sorted(selected))
    if actual != expected:
        raise ProviderCapabilityError(
            f"Docker GPU identity mismatch: selected {expected}, observed {actual}"
        )
    return observed


class DockerSandbox(Sandbox):
    """One container, driven through the guest service."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        request: SandboxRequest,
        container: str,
        network: str | None,
        client: GuestClient,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        resource_lease: GpuLease | None = None,
        agent_user: str = DEFAULT_AGENT_USER,
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
        self._client = client
        self.agent_user = agent_user
        self._sealed = False
        self.state = SandboxState.READY

    def _as(self, identity: Identity) -> str | None:
        """The unix user for a role, or ``None`` to keep the container's default.

        Framework work stays as the image's own user, which for a sandbox image is root:
        installing the guest service and collecting artifacts have to work whatever the
        agent did to its own files.
        """
        return self.agent_user if identity is Identity.AGENT else None

    @property
    def gateway_url(self) -> str | None:
        """The gateway as this container can reach it.

        The host binds to every interface; a container cannot use that address, so the
        URL is rewritten here rather than in the gateway — which is exactly the point of
        the gateway knowing nothing about providers.
        """
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
        if (
            identity is Identity.AGENT
            and self._sealed
            and self.request.network.mode is NetworkMode.ALLOWLIST
        ):
            env = _proxy_env(self.request) | (env or {})
        loop = asyncio.get_running_loop()
        started = loop.time()
        exit_code, stdout, stderr, timed_out = await self._client.exec(
            argv,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
            run_as=self._as(identity),
            output_sink=output_sink,
        )
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((loop.time() - started) * 1000),
            timed_out=timed_out,
        )

    async def write_file(
        self, path: PurePosixPath | str, data: bytes, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        await self._client.write_file(str(path), data, run_as=self._as(identity))

    async def read_file(self, path: PurePosixPath | str) -> bytes:
        return await self._client.read_file(str(path))

    async def upload_dir(
        self, source: str, target: PurePosixPath | str, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        """Copy a directory in, owned by whoever the caller said it is for.

        `docker cp` writes as root whatever `--user` would say, so ownership is set
        afterwards rather than assumed. Staging content the agent must be able to change
        and leaving it root-owned is the failure this exists to prevent — and one the
        framework must not paper over later, since a task decides what it opens up.
        """
        await self._client.mkdirs(str(target))
        code, _, stderr = await _docker("cp", f"{Path(source)}/.", f"{self.container}:{target}")
        if code != 0:
            raise ProviderStartError(f"upload to {target} failed: {stderr.strip()}")
        if (owner := self._as(identity)) is not None:
            code, _, stderr = await _docker(
                "exec", self.container, "chown", "-R", owner, str(target)
            )
            if code != 0:
                raise ProviderStartError(f"could not give {target} to {owner}: {stderr.strip()}")

    async def download_dir(self, source: PurePosixPath | str, target: str) -> None:
        Path(target).mkdir(parents=True, exist_ok=True)
        code, _, stderr = await _docker("cp", f"{self.container}:{source}/.", target)
        if code != 0:
            raise ProviderStartError(f"download from {source} failed: {stderr.strip()}")

    async def screenshot(self) -> bytes:
        return await self._client.screenshot()

    async def inject_input(self, actions: Sequence[object]) -> int:
        # Accept either raw dicts or DesktopAction models, so callers need no adapter.
        payload = [
            action if isinstance(action, dict) else action.model_dump(mode="json")  # type: ignore[union-attr]
            for action in actions
        ]
        return await self._client.inject_input(payload)

    async def open_egress(self) -> None:
        """Attach the default bridge, which is where a route off the host comes from.

        The isolated network stays attached throughout, so the gateway is reachable in
        both states and its address never moves — an agent handed one URL at the start of
        an episode must not find it stale halfway through.
        """
        if self.network is None:
            return  # the task declared `open`; it is already on the bridge
        code, _, stderr = await _docker("network", "connect", "bridge", self.container)
        if code != 0 and "already exists" not in stderr:
            raise ProviderStartError(f"could not open egress: {stderr.strip()}")
        self._sealed = False

    async def close_egress(self) -> None:
        """Detach it again, leaving only the isolated network and the gateway on it."""
        if self.network is None:
            return
        code, _, stderr = await _docker("network", "disconnect", "bridge", self.container)
        if code != 0 and "is not connected" not in stderr:
            # Loud, because the alternative is an agent measured with a network it was
            # never meant to have and a lock file that says otherwise.
            raise ProviderStartError(f"could not close egress: {stderr.strip()}")
        self._sealed = True

    async def destroy(self) -> None:
        """Idempotent: teardown also runs on failure paths, sometimes twice."""
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED
        try:
            await self._client.close()
            await _docker("rm", "-f", "-v", self.container, timeout=60)
            if self.network:
                await _docker("network", "rm", self.network, timeout=60)
        finally:
            self.release_resources()

    async def retain(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        await self._client.close()
        gpu_devices = (
            self.allocation.gpu.provider_addresses if self.allocation.gpu is not None else ()
        )
        handle = f"{HANDLE_PREFIX}{self.container}"
        return RetainedSandbox(
            provider="docker",
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"ale sandbox destroy {handle}",
            gpu_devices=gpu_devices,
        )


class DockerProvider(Provider):
    """Supplies container sandboxes."""

    name = "docker"

    def __init__(
        self,
        *,
        guestd_source: Path | None = None,
        gpus: tuple[int, ...] = (),
        gpu_lock_dir: Path | None = None,
    ) -> None:
        self._guestd_source = guestd_source or Path(__file__).resolve().parents[1] / "guestd"
        self.gpus = gpus
        self.gpu_lock_dir = gpu_lock_dir or cache_root() / "gpu-locks" / self.name
        # An image's conformance cannot change while it is pinned to a digest, and the
        # check costs a container start — so a sweep of ten thousand episodes pays for it
        # once rather than ten thousand times.
        self._checked: dict[str, list[str]] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            os="linux",
            gui=True,  # depends on the image; the GUI base image provides a desktop
            network_modes=frozenset({NetworkMode.BLOCK, NetworkMode.ALLOWLIST, NetworkMode.OPEN}),
        )

    async def preflight(self) -> None:
        if shutil.which("docker") is None:
            raise ProviderStartError("docker is not installed — see `just doctor`")
        code, _, stderr = await _docker("info", "--format", "{{.ServerVersion}}", timeout=30)
        if code != 0:
            raise ProviderStartError(f"docker daemon unreachable: {stderr.strip()}")

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if image.kind is not ImageKind.CONTAINER:
            raise ProviderCapabilityError("DockerProvider requires image.kind=container")
        prepared = await resolve_container_image(image) if isinstance(image, ImageRef) else image
        resolved = await resolve_prepared_container_image(prepared)
        if problems := await self.check_image(resolved.observed_ref):
            raise ProviderCapabilityError(
                f"{prepared.runtime_ref} cannot serve as a sandbox: {'; '.join(problems)}. "
                "See docs/specs/sandbox-image.md"
            )
        return prepared

    async def create(self, request: SandboxRequest) -> Sandbox:
        self.accepts(request)
        if request.image_kind is not ImageKind.CONTAINER:
            raise ProviderCapabilityError("DockerProvider requires image.kind=container")
        lease: GpuLease | None = None
        selected: tuple[str, ...] = ()
        candidates: tuple[str, ...] = ()
        if request.resources.gpus:
            host_devices = await self._host_gpus()
            configured = self.gpus or tuple(sorted(host_devices))
            unknown = sorted(set(configured) - set(host_devices))
            if unknown:
                raise ProviderCapabilityError(
                    "configured Docker GPU indices were not discovered: "
                    + ", ".join(str(index) for index in unknown)
                )
            candidates = tuple(host_devices[index].id for index in configured)
        resolved_image = await resolve_prepared_container_image(request.prepared_image)
        if problems := await self.check_image(resolved_image.observed_ref):
            raise ProviderCapabilityError(
                f"{request.prepared_image.runtime_ref} cannot serve as a sandbox: "
                f"{'; '.join(problems)}. "
                "See docs/specs/sandbox-image.md"
            )

        sandbox_id = f"{request.episode_id}-{uuid.uuid4().hex[:6]}"
        container = f"ale-{sandbox_id}"
        network = await self._ensure_network(sandbox_id, request)

        argv = [
            "run", "-d", "--name", container,
            "--label", f"{LABEL}={request.episode_id}",
            "--label", f"{MANAGED_LABEL}=true",
            "--label", f"{ROLE_LABEL}={request.role.value}",
            "--label", f"{RETENTION_LABEL}={request.retention}",
            "--cpus", str(request.resources.cpus),
            "--memory", f"{request.resources.memory_mb}m",
        ]  # fmt: skip
        if network:
            argv += ["--network", network]
        for key, value in request.env.items():
            argv += ["-e", f"{key}={value}"]
        if request.gateway_url or request.proxy_url:
            host_ip = await self._bridge_host_ip(network)
            argv += ["--add-host", f"{HOST_ALIAS}:{host_ip}"]
            if request.gateway_url:
                argv += ["-e", f"ALE_GATEWAY_URL={_reachable(request.gateway_url)}"]
            if request.proxy_url:
                request = request.model_copy(
                    update={"proxy_url": _reachable(request.proxy_url, host=host_ip)}
                )
        if request.resources.storage_mb is not None:
            argv += _storage_run_args(request.resources.storage_mb)
        if request.resources.gpus:
            used = await _managed_gpu_ids()
            available = tuple(key for key in candidates if key not in used)
            lease = GpuLease.acquire(available, request.resources.gpus, self.gpu_lock_dir)
            selected = lease.device_keys
            used_after_lock = await _managed_gpu_ids()
            if set(selected) & used_after_lock:
                lease.release()
                lease = None
                available = tuple(key for key in candidates if key not in used_after_lock)
                lease = GpuLease.acquire(available, request.resources.gpus, self.gpu_lock_dir)
                selected = lease.device_keys
            argv += _gpu_run_args(selected)
            argv += ["--label", f"{GPU_LABEL}={','.join(selected)}"]
        # The image's own command, always. A container lives exactly as long as its
        # command, and substituting one replaces whatever the image starts for itself —
        # for a desktop image, the entire graphical session. Images that have nothing to
        # do declare a command that waits; that is part of the image contract, so there
        # is nothing left here to decide.
        argv += [resolved_image.observed_ref]

        try:
            code, _, stderr = await _docker(*argv, timeout=300)
            if code != 0:
                if request.resources.storage_mb is not None:
                    raise ProviderCapabilityError(
                        "Docker could not enforce the requested writable-layer quota "
                        f"({request.resources.storage_mb} MB): {stderr.strip()}"
                    )
                if request.resources.gpus:
                    raise ProviderCapabilityError(
                        "Docker could not inject the selected NVIDIA GPUs through its "
                        f"runtime/CDI path: {stderr.strip()}"
                    )
                raise ProviderStartError(f"could not start container: {stderr.strip()}")

            code, started_content, stderr = await _docker(
                "inspect", "--format", "{{.Image}}", container, timeout=30
            )
            if code != 0 or started_content.strip() != resolved_image.observed_identity:
                raise ProviderStartError(
                    "started container image identity does not match the resolved image: "
                    f"{stderr.strip() or started_content.strip()}"
                )

            runtime_identity = None
            if lease:
                code, version, stderr = await _docker(
                    "info", "--format", "{{.ServerVersion}}", timeout=30
                )
                if code != 0 or not version.strip():
                    raise ProviderCapabilityError(
                        f"could not identify the Docker GPU runtime: {stderr.strip()}"
                    )
                runtime_identity = f"docker-engine/{version.strip()}"

            agent_user = await self._agent_user(resolved_image.observed_ref)
            await self._install_guestd(container)
            if request.sudo:
                await self._grant_sudo(container, agent_user)
            client = await self._connect(container)
            observed_gpu = await self._container_gpus(client) if selected else ()
            if selected:
                _verify_docker_gpu(selected, observed_gpu)
            if await self._has_desktop(resolved_image.observed_ref):
                await self._await_desktop(client)
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
        return DockerSandbox(
            sandbox_id=sandbox_id,
            request=request,
            container=container,
            network=network,
            client=client,
            resolved_image=resolved_image,
            allocation=ResourceAllocation(
                cpus=request.resources.cpus,
                memory_mb=request.resources.memory_mb,
                storage_mb=request.resources.storage_mb,
                gpu=(
                    GpuAllocation(
                        requested_count=request.resources.gpus,
                        provider_addresses=selected,
                        lease_keys=lease.device_keys,
                        observed_devices=observed_gpu,
                        runtime_identity=runtime_identity,
                    )
                    if lease
                    else None
                ),
                sudo=request.sudo,
                network_mode=request.network.mode,
                provider=self.name,
            ),
            resource_lease=lease,
            agent_user=agent_user,
        )

    async def _host_gpus(self) -> dict[int, GpuDevice]:
        if shutil.which("nvidia-smi") is None:
            raise ProviderCapabilityError("nvidia-smi is not installed on the Docker host")
        code, stdout, stderr = await _command(
            "nvidia-smi",
            f"--query-gpu={_HOST_NVIDIA_QUERY}",
            "--format=csv,noheader,nounits",
            timeout=30,
        )
        if code != 0:
            raise ProviderCapabilityError(f"host nvidia-smi failed: {stderr.strip()}")
        return _parse_host_nvidia_smi(stdout)

    async def _container_gpus(self, client: GuestClient) -> tuple[GpuDevice, ...]:
        code, stdout, stderr, _ = await client.exec(
            [
                "nvidia-smi",
                f"--query-gpu={_NVIDIA_QUERY}",
                "--format=csv,noheader,nounits",
            ],
            timeout_sec=30,
        )
        if code != 0:
            raise ProviderCapabilityError(
                f"Docker started the GPU sandbox but its nvidia-smi path failed: {stderr.strip()}"
            )
        return _parse_nvidia_smi(stdout)

    async def _ensure_network(self, sandbox_id: str, request: SandboxRequest) -> str | None:
        """Create the per-episode network.

        ``block`` and ``allowlist`` both use ``--internal``: the bridge has no route off
        the host, so the only reachable thing is the gateway. They differ in what the
        gateway will then forward, not in what the sandbox can dial — which is why
        allowlisting needs no privileged host rules and no tooling inside an image we do
        not control. ``open`` uses the default bridge.
        """
        if request.network.mode is NetworkMode.OPEN:
            return None
        network = f"ale-net-{sandbox_id}"
        code, _, stderr = await _docker("network", "create", "--internal", network, timeout=60)
        if code != 0:
            raise ProviderStartError(f"could not create isolated network: {stderr.strip()}")
        return network

    async def _bridge_host_ip(self, network: str | None) -> str:
        """The host's address on a container's network.

        On an isolated bridge this is the only host address a sandbox can dial, which is
        why it is read from the network rather than assumed.
        """
        if network is None:
            return "host-gateway"
        code, out, stderr = await _docker(
            "network", "inspect", network, "--format", "{{(index .IPAM.Config 0).Gateway}}"
        )
        if code != 0 or not out.strip():
            raise ProviderStartError(f"could not read the gateway address of {network}: {stderr}")
        return out.strip()

    async def check_image(self, reference: str) -> list[str]:
        """What this image is missing from the sandbox contract, if anything.

        Checked before the sandbox is used rather than discovered through its symptoms:
        an image with no long-lived command dies immediately, and one with no agent
        account fails on the first thing the agent tries to do — neither failure names
        its cause. See `docs/specs/sandbox-image.md`.

        Partial by construction: whether a command *stays* running cannot be known
        without running it, so this catches an image that declares none at all. The rest
        surfaces as a sandbox that dies on start, which at least says so immediately.
        """
        if reference in self._checked:
            return self._checked[reference]

        missing: list[str] = []

        code, out, _ = await _docker(
            "image",
            "inspect",
            "--format",
            "{{json .Config.Cmd}}{{json .Config.Entrypoint}}",
            reference,
        )
        if code != 0:
            return [f"image {reference} could not be inspected; is it pulled?"]
        if out.strip().startswith("null"):
            missing.append(
                "no command: a container lives exactly as long as its command, and the "
                "engine will not supply one"
            )

        probe = await _docker(
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            reference,
            "-c",
            "if command -v python3 >/dev/null; then "
            "python3 -c 'import sys; raise SystemExit(sys.version_info < (3,12))' "
            ">/dev/null 2>&1 || echo old-python3; else echo no-python3; fi; "
            f"id -u {await self._agent_user(reference)} >/dev/null 2>&1 || echo no-agent-user",
            timeout=120,
        )
        for line in probe[1].split():
            if line == "no-python3":
                missing.append("no python3 on PATH: the guest service runs on it")
            elif line == "old-python3":
                missing.append("python3 on PATH is older than 3.12: ale_verify cannot run on it")
            elif line == "no-agent-user":
                user = await self._agent_user(reference)
                missing.append(
                    f"no unprivileged account {user!r}: the agent would have to run as root, "
                    "and could then change the conditions it is measured under"
                )

        self._checked[reference] = missing
        return missing

    async def _agent_user(self, reference: str) -> str:
        """The account this image intends the agent to be.

        Asked of the image because the image is what created it, gave it a home and runs
        the desktop session as it. Guessing here would mean the engine deciding who owns
        files in somebody else's filesystem.
        """
        return await self._label(reference, USER_LABEL) or DEFAULT_AGENT_USER

    async def _grant_sudo(self, container: str, agent_user: str) -> None:
        """Let the agent elevate, because its task said it needs to.

        Granted at provisioning rather than left to the task's setup, so that the
        privilege is something the run configured and recorded — not something a script
        arranged where provenance would never see it.
        """
        rule = f"{agent_user} ALL=(ALL) NOPASSWD: ALL"
        code, _, stderr = await _docker(
            "exec",
            container,
            "sh",
            "-c",
            f"mkdir -p /etc/sudoers.d && printf '%s\\n' '{rule}' > /etc/sudoers.d/ale-agent "
            "&& chmod 0440 /etc/sudoers.d/ale-agent",
        )
        if code != 0:
            raise ProviderStartError(
                f"the task asked for elevated privileges and this image cannot grant them: "
                f"{stderr.strip()}"
            )

        # Writing a sudoers rule succeeds in an image with no sudo at all, so the grant is
        # confirmed by using it. Reporting a privilege that was never conferred is the
        # failure this whole declaration exists to avoid — the task would run, fail on
        # access, and provenance would record an isolation level that never applied.
        code, _, stderr = await _docker("exec", "-u", agent_user, container, "sudo", "-n", "true")
        if code != 0:
            raise ProviderStartError(
                f"elevated privileges were requested but {agent_user} still cannot elevate; "
                f"this image does not support them: {stderr.strip()}"
            )

    async def _has_desktop(self, reference: str) -> bool:
        """Whether this image brings a desktop up, and so must be waited for.

        Asked of the image rather than derived from the harness. A task's own setup can
        need the screen — this one opens a window on it — so a desktop image has to be
        ready whichever agent is about to run, including one that never looks at it.
        Deriving it from the harness made `--agent nop` skip the wait and fail in setup
        with "Can't open display", while the same task passed under a stepwise agent.
        """
        return await self._label(reference, GUI_LABEL) == "true"

    async def _label(self, reference: str, name: str) -> str:
        code, out, _ = await _docker(
            "image", "inspect", "--format", f'{{{{index .Config.Labels "{name}"}}}}', reference
        )
        return out.strip() if code == 0 else ""

    async def _await_desktop(self, client: GuestClient, timeout_sec: float = 120) -> None:
        """Block until the screen can actually be captured.

        A desktop image starts an X server, a window manager and a session from its own
        command, and none of that is instant. Waiting on a marker file would work for
        one image and be a lie for the next, so this waits on the capability itself:
        a sandbox is ready for GUI work when a screenshot succeeds.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        last: Exception | None = None
        while loop.time() < deadline:
            try:
                await client.screenshot()
                return
            except Exception as exc:
                last = exc
                await asyncio.sleep(1.0)
        raise ProviderStartError(
            f"the desktop did not come up within {timeout_sec:g}s; last attempt: {last}"
        )

    async def _install_guestd(self, container: str) -> None:
        """Copy the guest service in.

        Base images bake it, but copying keeps the provider usable with any image and
        guarantees the running code matches this checkout — worth the few milliseconds.
        """
        code, _, stderr = await _docker("exec", container, "mkdir", "-p", str(GUESTD_DIR))
        if code != 0:
            raise ProviderStartError(f"could not prepare guest directory: {stderr.strip()}")
        for name in ("main.py", "protocol.py", "gui.py"):
            source = self._guestd_source / name
            if not source.exists():
                continue
            code, _, stderr = await _docker(
                "cp", str(source), f"{container}:{GUESTD_DIR}/{name}", timeout=60
            )
            if code != 0:
                raise ProviderStartError(f"could not install {name}: {stderr.strip()}")

    async def _connect(self, container: str) -> GuestClient:
        transport = StdioTransport(
            [
                "docker",
                "exec",
                "-i",
                container,
                # One interpreter, and it is the image's own. An image that needs the
                # guest service to have Pillow installs it there; choosing between
                # interpreters was a rule that existed only to be got wrong.
                "python3",
                str(GUESTD_DIR / "main.py"),
                "--stdio",
            ]
        )
        await transport.start()
        client = GuestClient(transport)
        try:
            await asyncio.wait_for(client.health(), timeout=60)
        except Exception as exc:
            await client.close()
            raise ProviderStartError(f"guest service did not answer: {exc}") from exc
        return client


def _reachable(url: str | None, *, host: str = HOST_ALIAS) -> str | None:
    """Rewrite a host-side gateway URL into one a container can dial."""
    if not url:
        return url
    for bound in ("0.0.0.0", "127.0.0.1", "localhost", "[::]"):
        if f"//{bound}:" in url:
            return url.replace(f"//{bound}:", f"//{host}:", 1)
    return url
