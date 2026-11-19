"""One rootless gVisor sandbox per single-node Slurm allocation."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import AsyncExitStack

from ale.core.capability import Arch, CapabilityProfile, SandboxNeeds, accepts_model, unmet
from ale.core.config import SlurmPartition, SlurmProviderConfig
from ale.core.errors import (
    GuestUnreachableError,
    MemoryExhaustedError,
    ProviderCapabilityError,
    ProviderStartError,
)
from ale.core.sandbox import (
    GpuAllocation,
    ImageKind,
    ImageRef,
    PreparedTaskImage,
    Provider,
    ResolvedImage,
    ResourceAllocation,
    SandboxRequest,
    SandboxRole,
    SandboxState,
)
from ale.core.taskspec import ContainerFeature, NetworkMode, OperatingSystem, Resources
from ale.run import guestd
from ale.run.framework import with_framework
from ale.run.images import resolve_container_image, resolve_prepared_container_image
from ale.run.providers import memory, relay
from ale.run.providers.desktop import CONTAINER_VARIABLE, require_resolution
from ale.run.providers.docker import gvisor
from ale.run.providers.privileges import set_and_prove_elevation
from ale.run.providers.slurm.job import Job, JobReady, JobTransport, config_digest
from ale.run.providers.tunnel import PROXY_PORT, GatewayTunnel, TunneledSandbox, tunnel_plan
from ale.run.transport import GuestClient

# The batch worker, Podman, gVisor, and the overlapping control steps share the job.
JOB_OVERHEAD_MB = 512


def _profiles(partition: SlurmPartition) -> tuple[CapabilityProfile, ...]:
    common = {ContainerFeature.FUSE, ContainerFeature.USER_NAMESPACES, ContainerFeature.MOUNT}
    return tuple(
        CapabilityProfile(
            provider="slurm",
            kind=ImageKind.CONTAINER,
            os=OperatingSystem.LINUX,
            arch=Arch.X86_64,
            gui=True,
            network_modes=frozenset(NetworkMode),
            gpus=partition.gpu_model is not None,
            gpu_models=(partition.gpu_model,) if partition.gpu_model else (),
            max_cpus=partition.max_cpus,
            max_memory_mb=max(1, partition.max_memory_mb - JOB_OVERHEAD_MB),
            container_features=frozenset(common | {feature}),
        )
        for feature in (ContainerFeature.RAW_SOCKETS, ContainerFeature.NESTED_CONTAINERS)
    )


def _runtime(features: Sequence[ContainerFeature]) -> tuple[str, list[str]]:
    if ContainerFeature.NESTED_CONTAINERS in features:
        runtime = gvisor.NESTED
        caps = (*gvisor.ENGINE_CAPABILITIES, "MKNOD", "AUDIT_WRITE", "NET_RAW")
    elif ContainerFeature.RAW_SOCKETS in features:
        runtime = gvisor.NET_RAW
        caps = ("NET_RAW",)
    else:
        runtime = gvisor.RUNTIME
        caps = ()
    args = ["--runtime-flag=" + flag.removeprefix("--") for flag in gvisor.ARGUMENTS[runtime]]
    args.extend("--cap-add=" + cap for cap in caps)
    if ContainerFeature.MOUNT in features and "SYS_ADMIN" not in caps:
        args.append("--cap-add=SYS_ADMIN")
    if runtime == gvisor.NESTED:
        args.extend("--volume=" + volume for volume in gvisor.ENGINE_STORAGE)
    return runtime, args


async def _guest(
    job: Job, user: str, watcher: memory.GuestEvents, stack: AsyncExitStack
) -> GuestClient:
    transport = JobTransport(
        job.step(
            "exec",
            "env",
            f"ALE_AGENT_USER={user}",
            guestd.PYTHON["linux"],
            "-I",
            "/opt/ale/guestd/main.py",
            "--stdio",
        ),
        job,
        watcher,
    )
    client = GuestClient(transport)
    stack.push_async_callback(client.close)
    await transport.start()
    return client


async def _gpu_allocation(
    client: GuestClient, ready: JobReady, resources: Resources, part: SlurmPartition
) -> GpuAllocation | None:
    if not resources.gpus:
        return None
    ids = tuple(device.id for device in ready.devices)
    expected = part.gpu_model
    assert expected is not None
    for device in ready.devices:
        if (
            not accepts_model((expected.name,), device.model)
            or not accepts_model(resources.gpu_types, device.model)
            or (device.memory_total_mb or 0)
            < max(expected.memory_mb or 0, resources.gpu_memory_mb or 0)
        ):
            raise ProviderCapabilityError(f"Slurm allocated an incompatible GPU: {device}")
    code, _, err, _ = await client.exec(["ldconfig"])
    if code:
        raise ProviderStartError(f"could not register Slurm's GPU driver libraries: {err}")
    code, out, err, _ = await client.exec(
        ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"]
    )
    if code or set(out.split()) != set(ids) or len(set(ids)) != resources.gpus:
        raise ProviderStartError(f"Slurm GPU allocation does not match guest devices: {out} {err}")
    return GpuAllocation(
        requested_count=resources.gpus,
        provider_addresses=ids,
        lease_keys=ids,
        observed_devices=ready.devices,
        runtime_identity=ready.runtime,
    )


class SlurmSandbox(TunneledSandbox):
    def __init__(
        self,
        job: Job,
        connections: AsyncExitStack,
        client: GuestClient,
        file_client: GuestClient,
        *,
        request: SandboxRequest,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        home: str,
        gateway: str | None,
    ) -> None:
        super().__init__(
            sandbox_id=f"{job.partition.host}/{job.id}",
            request=request,
            resolved_image=resolved_image,
            allocation=allocation,
            agent_user=request.user,
            agent_home=home,
        )
        self.job = job
        self.connections = connections
        self._client, self._file_client = client, file_client
        self._local_gateway = gateway
        self._relay = None
        self._sealed = request.network.mode is not NetworkMode.OPEN

    async def open_egress(self) -> None:
        if self._sealed:
            await relay.stop(self._client, self.request)
            await self.job.network("open")
            self._relay = None
            self._sealed = False

    async def close_egress(self) -> None:
        if self.request.network.mode is NetworkMode.OPEN:
            return
        if not self._sealed:
            await self.job.network("block")
        self._sealed = True
        self._relay = await relay.start(
            self._client,
            self.request,
            self.allocation,
            proxy=f"http://127.0.0.1:{PROXY_PORT}",
            host="127.0.0.1",
        )

    async def destroy(self) -> None:
        if self.state is SandboxState.DESTROYED:
            return
        try:
            await self.connections.aclose()
        finally:
            await self.job.stop()
        self.state = SandboxState.DESTROYED


class SlurmProvider(Provider):
    name = "slurm"

    def __init__(self, config: SlurmProviderConfig) -> None:
        self.config = config

    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        return tuple(profile for p in self.config.partitions for profile in _profiles(p))

    def _partition(self, needs: SandboxNeeds) -> SlurmPartition | None:
        for part in self.config.partitions:
            if (
                bool(needs.resources.gpus) != bool(part.max_gpus)
                or needs.resources.gpus > part.max_gpus
                or needs.resources.memory_mb + JOB_OVERHEAD_MB > part.max_memory_mb
            ):
                continue
            if any(not unmet(profile, needs) for profile in _profiles(part)):
                return part
        return None

    def refusals(self, needs: SandboxNeeds, *, user: str, role: SandboxRole) -> tuple[str, ...]:
        if self._partition(needs) is None:
            return ("no configured Slurm partition serves that single-node allocation",)
        return ()

    async def preflight(self, needs: Sequence[SandboxNeeds] = ()) -> None:
        partitions = (
            self.config.partitions
            if not needs
            else tuple(part for need in needs if (part := self._partition(need)) is not None)
        )
        for part in dict.fromkeys(partitions):
            await Job(part).remote(
                "command -v sbatch srun scancel sacct >/dev/null && "
                f"scontrol show partition {part.name} -o"
            )

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if image.kind is not ImageKind.CONTAINER:
            raise ProviderCapabilityError("Slurm requires a Linux container image")
        prepared = await resolve_container_image(image) if isinstance(image, ImageRef) else image
        await resolve_prepared_container_image(prepared)
        return prepared

    async def create(self, request: SandboxRequest) -> SlurmSandbox:
        await self.accepts(request)
        part = self._partition(request.needs)
        assert part is not None
        job = Job(part)
        resolved = await resolve_prepared_container_image(request.prepared_image)
        image = await with_framework(resolved.observed_identity, user=request.user)
        gateway, listeners, targets = tunnel_plan(request)
        resources = request.resources
        env = dict(request.env)
        env[CONTAINER_VARIABLE] = str(request.desktop_resolution)
        if gateway:
            env["ALE_GATEWAY_URL"] = gateway
        runtime, args = _runtime(resources.container_features)
        sealed = request.network.mode is not NetworkMode.OPEN
        stack = AsyncExitStack()
        try:
            # Podman names the image it loads by its config's digest.
            loaded = await config_digest(image)
            await job.stage(
                image,
                dict(
                    image=image,
                    loaded=loaded,
                    version=gvisor.VERSION,
                    runtime_args=args,
                    gpus=resources.gpus,
                    cpus=resources.cpus,
                    job_memory_mb=resources.memory_mb + JOB_OVERHEAD_MB,
                    env=env,
                    sealed=sealed,
                ),
            )
            await job.submit(
                cpus=resources.cpus,
                memory_mb=resources.memory_mb + JOB_OVERHEAD_MB,
                gpus=resources.gpus,
                minutes=self.config.walltime_minutes,
            )
            ready = await job.ready(self.config.queue_timeout_sec)
            if ready.image != loaded:
                raise ProviderStartError("Slurm started a different Task image")
            watcher = await memory.GuestEvents.start(
                job.step(
                    "exec", guestd.PYTHON["linux"], "-I", "-c", memory.WATCHER, gvisor.OOM_KILLS
                ),
                counter="the Sentry",
            )
            stack.push_async_callback(watcher.close)
            client = await _guest(job, request.user, watcher, stack)
            file_client = await _guest(job, request.user, watcher, stack)
            health, _ = await asyncio.gather(
                client.health(timeout_sec=90), file_client.health(timeout_sec=90)
            )
            home = str(health.get("agent_home") or "")
            if not home:
                raise ProviderStartError("Slurm guestd did not report an agent home")
            await set_and_prove_elevation(client, request, request.user)
            gpu = await _gpu_allocation(client, ready, resources, part)
            desktop = None
            if health.get("gui") and request.role is not SandboxRole.VERIFIER:
                async with asyncio.timeout(120):
                    while True:
                        try:
                            await client.screenshot()
                            break
                        except MemoryExhaustedError:
                            raise
                        except (GuestUnreachableError, TimeoutError):
                            await asyncio.sleep(1)
                desktop = await require_resolution(
                    client, request.desktop_resolution, container=True
                )
            if listeners:
                tunnel = GatewayTunnel(job, listeners=listeners, targets=targets)
                stack.push_async_callback(tunnel.close)
                await tunnel.start()
            sandbox = SlurmSandbox(
                job,
                stack,
                client,
                file_client,
                request=request,
                resolved_image=resolved.model_copy(
                    update={"observed_identity": image, "observed_ref": image}
                ),
                allocation=ResourceAllocation(
                    cpus=resources.cpus,
                    memory_mb=resources.memory_mb,
                    gpu=gpu,
                    sudo=request.sudo,
                    network_mode=request.network.mode,
                    provider=self.name,
                    leaf=self.name,
                    runtime=runtime,
                    node=ready.node,
                    desktop_resolution=desktop,
                ),
                home=home,
                gateway=gateway,
            )
            sandbox.state = SandboxState.READY
            if sealed:
                await sandbox.close_egress()
            return sandbox
        except BaseException:
            try:
                await stack.aclose()
            finally:
                await asyncio.shield(job.stop())
            raise
