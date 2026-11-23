"""Rootless gVisor Sandboxes in single-node Slurm allocations, driven from a login node of
the cluster, where Orbit runs.

A solver gets an allocation of its own, sized for it and for the verifiers its request
names, and every Sandbox booted from one of its snapshots, or mounting views of its volumes,
starts in that allocation while it has room, from the node's own store: a snapshot is never
copied for its episode. The allocation ends with the last Sandbox and snapshot in it. Images
are prepared on the cluster too ([`images`](images.py)).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from orbit.core.capability import Arch, CapabilityProfile, SandboxNeeds, accepts_model, unmet
from orbit.core.config import SlurmPartition, SlurmProviderConfig
from orbit.core.errors import (
    AllocationEndedError,
    GuestUnreachableError,
    MemoryExhaustedError,
    ProviderCapabilityError,
    ProviderStartError,
)
from orbit.core.sandbox import (
    GpuAllocation,
    ImageKind,
    ImageRef,
    PreparedTaskImage,
    Provider,
    ResolvedImage,
    ResourceAllocation,
    RetainedSandbox,
    SandboxRequest,
    SandboxRole,
    SandboxState,
)
from orbit.core.taskspec import ContainerFeature, NetworkMode, OperatingSystem, Resources
from orbit.run import guestd
from orbit.run.providers import memory, relay
from orbit.run.providers.desktop import CONTAINER_VARIABLE, require_resolution
from orbit.run.providers.docker import gvisor
from orbit.run.providers.privileges import set_and_prove_elevation
from orbit.run.providers.slurm import images, worker
from orbit.run.providers.slurm.images import Recipe
from orbit.run.providers.slurm.job import (
    SNAPSHOTS,
    Job,
    JobReady,
    JobTransport,
    SandboxSteps,
    StepReady,
    cache,
    slurm,
    write,
)
from orbit.run.providers.tunnel import PROXY_PORT, GatewayTunnel, TunneledSandbox, tunnel_plan
from orbit.run.transport import GuestClient

#: The batch step, Podman's namespace and the control steps of every Sandbox in the job.
JOB_OVERHEAD_MB = 512
#: What a Sandbox's own step holds beside its container: the step's process and conmon.
STEP_OVERHEAD_MB = 32
#: Measured: a chain of 200 commits, each booted from the one before, over a one-layer image.
MAX_SAVES = 200
#: How long a Sandbox's step may take to start once its allocation runs.
START_TIMEOUT_SEC = 900
#: How long before its deadline an allocation still takes a Sandbox that names no duration:
#: a solver, which moves where it lasts once its allocation's deadline comes.
MIN_LEFT_SEC = 300
PREFIX = "slurm:"
#: The account an image named by its reference alone is prepared for.
USER = "user"


def step_memory_mb(memory_mb: int) -> int:
    """The memory of the step that holds a Sandbox of ``memory_mb``: gVisor's Sentry, gofer
    and stubs are charged to it, as to a docker:gvisor container."""
    return gvisor.memory_mb(memory_mb) + STEP_OVERHEAD_MB


def _largest_memory_mb(partition: SlurmPartition) -> int:
    memory_mb = partition.max_memory_mb - JOB_OVERHEAD_MB - STEP_OVERHEAD_MB - 108
    while memory_mb > 1 and step_memory_mb(memory_mb) + JOB_OVERHEAD_MB > partition.max_memory_mb:
        memory_mb -= 1
    return max(1, memory_mb)


def _profiles(
    partition: SlurmPartition, config: SlurmProviderConfig
) -> tuple[CapabilityProfile, ...]:
    common = {ContainerFeature.FUSE, ContainerFeature.USER_NAMESPACES, ContainerFeature.MOUNT}
    return tuple(
        CapabilityProfile(
            provider="slurm",
            kind=ImageKind.CONTAINER,
            os=OperatingSystem.LINUX,
            arch=Arch.X86_64,
            gui=True,
            network_modes=frozenset(NetworkMode),
            snapshot=True,
            max_saves=MAX_SAVES,
            gpus=partition.gpu_model is not None,
            gpu_models=(partition.gpu_model,) if partition.gpu_model else (),
            max_cpus=partition.max_cpus,
            max_memory_mb=_largest_memory_mb(partition),
            container_features=frozenset(common | {feature}),
            volumes=True,
            # An allocation's wall time, less what it keeps to drain.
            max_duration_sec=(config.walltime_minutes - config.drain_minutes) * 60,
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
    return runtime, args


def _engines(resources: Resources) -> tuple[str, ...]:
    """A Sandbox's container engine's storage, which is volumes of its own."""
    if ContainerFeature.NESTED_CONTAINERS in resources.container_features:
        return gvisor.ENGINE_STORAGE
    return ()


def _volume_specs(
    stacks: dict[str, dict[str, object]], engines: Sequence[str], *, viewing: bool
) -> list[dict[str, object]]:
    """The volumes of a Sandbox booted from a snapshot with ``stacks``, or ``viewing`` its
    volumes from a Sandbox of another image: a view of each volume, and a copy of each of
    ``engines`` the snapshot holds, never a view, as an engine's own layers need their
    whiteouts; an engine path the snapshot lacks gets a volume of its own."""
    specs: list[dict[str, object]] = []
    for path, stack in stacks.items():
        if not stack["copied"]:
            specs.append(dict(path=path, kind="view", layers=stack["layers"], root=stack["root"]))
        elif path in engines and not viewing:
            specs.append(dict(path=path, kind="copy", layers=stack["layers"], copied=True))
    held = {str(spec["path"]) for spec in specs}
    specs.extend(dict(path=path, kind="fresh", copied=True) for path in engines if path not in held)
    return specs


def _size(resources: Resources) -> tuple[int, int, int]:
    """CPUs, step memory and GPUs a Sandbox of ``resources`` holds in its allocation."""
    return resources.cpus, step_memory_mb(resources.memory_mb), resources.gpus


def _capacity(needs: SandboxNeeds, separate: Resources | None = None) -> tuple[int, int, int]:
    """The allocation that holds a Sandbox and, one at a time, each verifier booted from its
    snapshot and the ``separate`` verifier that mounts views of its volumes."""
    sizes = [_size(needs.resources)]
    verifier = needs.verifier
    while verifier is not None:
        sizes.append(_size(verifier.resources))
        verifier = verifier.verifier
    if separate is not None:
        sizes.append(_size(separate))
    cpus, memory_mb, gpus = zip(*sizes, strict=True)
    return max(cpus), max(memory_mb), max(gpus)


def _handle(job: Job, index: str) -> str:
    return f"{PREFIX}{job.name}/{index}"


def _parse(handle: str) -> tuple[str, str]:
    name, _, index = handle.removeprefix(PREFIX).partition("/")
    if not handle.startswith(PREFIX) or not (name.startswith("orbit-") and index.isdigit()):
        raise ProviderCapabilityError(f"{handle!r} is no Slurm snapshot handle")
    return name, index


def _resolved(image: PreparedTaskImage, user: str | None = None) -> ResolvedImage:
    """An image prepared on the cluster, which the cache holds as it was prepared, with the
    framework for ``user`` where one is asked for."""
    record = images.recorded(image) if image.kind is ImageKind.CONTAINER else None
    if record is None:
        raise ProviderCapabilityError(
            f"Slurm's cache holds no image {image.prepared_identity} prepared for the cluster"
        )
    if user is not None and record.get("user") != user:
        raise ProviderCapabilityError(
            f"{image.prepared_identity} was prepared for the account {record.get('user')!r}, "
            f"not {user!r}"
        )
    return ResolvedImage(
        kind=ImageKind.CONTAINER,
        prepared_identity=image.prepared_identity,
        observed_identity=image.prepared_identity,
        observed_ref=image.runtime_ref,
    )


@dataclass(eq=False)
class _Snapshot:
    index: str
    image: str
    prepared_identity: str
    episode_id: str
    roles: tuple[Literal["solver", "verifier"], ...]
    reason: str
    boots: int
    """The Sandboxes its episode is still to boot from it in its allocation, which keep the
    allocation: a snapshot verifier, until it boots or the allocation's deadline comes."""
    stacks: dict[str, dict[str, object]] = field(default_factory=dict)
    """Each volume's layers in the allocation's store, topmost first, its root's owner and
    mode, and whether it is an engine's storage, by path."""
    archive: str | None = None
    """Below the cache, once it was written for another allocation to boot from."""

    @property
    def layers(self) -> set[str]:
        return {str(layer) for stack in self.stacks.values() for layer in stack["layers"]}


@dataclass(eq=False)
class _Chain:
    """An allocation and what runs and is saved in it."""

    job: Job
    ready: JobReady
    seed: str
    """The image the allocation loaded, by the ID Podman gives it."""
    cpus: int
    memory_mb: int
    gpus: int
    running: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    snapshots: dict[str, _Snapshot] = field(default_factory=dict)
    images: set[str] = field(default_factory=set)
    """The images loaded since, a separate verifier's, by the ID Podman gives them."""
    viewing: dict[str, set[str]] = field(default_factory=dict)
    """The layers each running Sandbox's views stack, which stay while it runs."""
    deadline: datetime | None = None
    """When Orbit must begin to save what runs here, before Slurm ends the allocation at its
    wall time, which leaves the drain to save it and write it out."""
    count: int = 0
    closed: bool = False

    def reserve(self, resources: Resources, *, lasting: float | None = None) -> str | None:
        """A new Sandbox's index in the allocation, if it fits beside what runs there and,
        where it is to last ``lasting`` seconds, the allocation lasts that long."""
        size = _size(resources)
        used = [sum(held) for held in zip(*self.running.values(), strict=True)] or [0, 0, 0]
        capacity = (self.cpus, self.memory_mb, self.gpus)
        if self.closed or any(u + s > c for u, s, c in zip(used, size, capacity, strict=True)):
            return None
        if lasting is not None and not self.lasts(lasting):
            return None
        self.count += 1
        index = str(self.count)
        self.running[index] = size
        return index

    def lasts(self, seconds: float) -> bool:
        """Whether the allocation's deadline leaves ``seconds`` more."""
        return self.deadline is None or (self.deadline - datetime.now(UTC)).total_seconds() >= (
            seconds
        )

    @property
    def idle(self) -> bool:
        """Nothing runs, and every snapshot left was written out and is booted from no more
        here, so the allocation holds nothing its episode still needs."""
        return not self.running and all(
            snapshot.archive and not snapshot.boots for snapshot in self.snapshots.values()
        )


async def _guest(
    steps: SandboxSteps,
    ready: StepReady,
    user: str,
    watcher: memory.GuestEvents,
    stack: AsyncExitStack,
) -> GuestClient:
    transport = JobTransport(
        steps.step(
            "exec",
            "env",
            f"ORBIT_AGENT_USER={user}",
            guestd.PYTHON["linux"],
            "-I",
            "/opt/orbit/guestd/main.py",
            "--stdio",
        ),
        steps,
        ready,
        watcher,
    )
    client = GuestClient(transport)
    stack.push_async_callback(client.close)
    await transport.start()
    return client


async def _gpu_allocation(
    client: GuestClient, ready: StepReady, resources: Resources, chain: _Chain
) -> GpuAllocation | None:
    if not resources.gpus:
        return None
    ids = tuple(device.id for device in ready.devices)
    expected = chain.job.partition.gpu_model
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
        runtime_identity=chain.ready.runtime,
    )


class SlurmSandbox(TunneledSandbox):
    def __init__(
        self,
        provider: SlurmProvider,
        chain: _Chain,
        steps: SandboxSteps,
        connections: AsyncExitStack,
        client: GuestClient,
        file_client: GuestClient,
        *,
        request: SandboxRequest,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        home: str,
        gateway: str | None,
        volumes: tuple[str, ...],
    ) -> None:
        super().__init__(
            sandbox_id=f"{chain.job.id}.{steps.index}",
            request=request,
            resolved_image=resolved_image,
            allocation=allocation,
            agent_user=request.user,
            agent_home=home,
        )
        self.provider = provider
        self.chain = chain
        self.steps = steps
        self.connections = connections
        self._client, self._file_client = client, file_client
        self._local_gateway = gateway
        self._relay = None
        self._sealed = request.network.mode is not NetworkMode.OPEN
        self.volumes = volumes

    async def open_egress(self) -> None:
        if self._sealed:
            await relay.stop(self._client, self.request)
            await self.steps.network("open")
            self._relay = None
            self._sealed = False

    async def close_egress(self) -> None:
        if self.request.network.mode is NetworkMode.OPEN:
            return
        if not self._sealed:
            await self.steps.network("block")
        self._sealed = True
        self._relay = await relay.start(
            self._client,
            self.request,
            self.allocation,
            proxy=f"http://127.0.0.1:{PROXY_PORT}",
            host="127.0.0.1",
        )

    async def snapshot(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        """Stop the Sandbox and commit its disk to its allocation's store, which it keeps for
        the Sandboxes its episode boots from it, with its volumes' layers. A snapshot its Run
        keeps after the episode is written to the cluster's shared file system too, as the
        allocation ends then."""
        # The relay and the guest's sessions end with the container.
        await relay.stop(self._client, self.request)
        self._relay = None
        await self.connections.aclose()
        image, stacks = await self.chain.job.save(self.steps.index)
        snapshot = _Snapshot(
            index=self.steps.index,
            image=image,
            prepared_identity=self.request.prepared_image.prepared_identity,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            boots=int(self.request.snapshot_verifier is not None),
            stacks=stacks,
        )
        handle = _handle(self.chain.job, self.steps.index)
        self.chain.snapshots[snapshot.index] = snapshot
        self.provider.snapshots[handle] = (self.chain, snapshot)
        if self.request.retention == "snapshot" and not self.request.planned_saves:
            await self.provider.write_out(self.chain, snapshot)
        return RetainedSandbox(
            provider="slurm",
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"orbit sandbox destroy {handle}",
        )

    async def destroy(self) -> None:
        if self.state is SandboxState.DESTROYED:
            return
        try:
            await self.connections.aclose()
        finally:
            await self.provider.release(self.chain, self.steps.index)
        self.state = SandboxState.DESTROYED


class SlurmProvider(Provider):
    name = "slurm"

    def __init__(self, config: SlurmProviderConfig) -> None:
        self.config = config
        #: The snapshots this process saved, by handle, with the allocation that holds them.
        self.snapshots: dict[str, tuple[_Chain, _Snapshot]] = {}

    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        return tuple(
            profile for p in self.config.partitions for profile in _profiles(p, self.config)
        )

    def _partition(
        self, needs: SandboxNeeds, *, separate: Resources | None = None
    ) -> SlurmPartition | None:
        cpus, memory_mb, gpus = _capacity(needs, separate)
        for part in self.config.partitions:
            if (
                bool(gpus) != bool(part.max_gpus)
                or gpus > part.max_gpus
                or cpus > part.max_cpus
                or memory_mb + JOB_OVERHEAD_MB > part.max_memory_mb
            ):
                continue
            if any(not unmet(profile, needs) for profile in _profiles(part, self.config)):
                return part
        return None

    def refusals(self, needs: SandboxNeeds, *, user: str, role: SandboxRole) -> tuple[str, ...]:
        if self._partition(needs) is None:
            return (
                "no configured Slurm partition serves that single-node allocation"
                + (" with the verifier booted from its snapshot" if needs.verifier else ""),
            )
        return ()

    async def preflight(self, needs: Sequence[SandboxNeeds] = ()) -> None:
        partitions = (
            self.config.partitions
            if not needs
            else tuple(part for need in needs if (part := self._partition(need)) is not None)
        )
        for command in ("sbatch", "srun", "squeue", "scancel", "sacct", "scontrol"):
            if shutil.which(command) is None:
                raise ProviderStartError(
                    f"Slurm's {command} is not installed here: Orbit runs on a login node of "
                    "the cluster, whose home its compute nodes share"
                )
        for part in dict.fromkeys(partitions):
            await slurm("scontrol", "show", "partition", part.name, "-o")

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        """Check an image prepared for the cluster: one a Task names is prepared there from
        its recipe (:meth:`prepare_recipe`), as no container runtime runs here."""
        if isinstance(image, ImageRef):
            if image.kind is not ImageKind.CONTAINER:
                raise ProviderCapabilityError("Slurm requires a Linux container image")
            # For the account Orbit's images run their agent as.
            pinned = await images.pin(image.reference)
            prepared, _ = await self.prepare_recipe(
                Recipe(
                    key=images.key(
                        "external-ref", reference=pinned, context=None, user=USER, keep_alive=False
                    ),
                    source="external-ref",
                    reference=pinned,
                    user=USER,
                )
            )
            return prepared
        _resolved(image)
        return image

    async def prepare_recipe(self, recipe: Recipe) -> tuple[PreparedTaskImage, bool]:
        """Prepare an image on the cluster, once per key; return it and whether an earlier
        preparation had."""
        return await images.prepare(self.config, recipe)

    async def create(self, request: SandboxRequest) -> SlurmSandbox:
        await self.accepts(request)
        engines = _engines(request.resources)
        candidates: tuple[str, ...] = ()
        if request.snapshot is None and request.volumes_from is None:
            resolved = _resolved(request.prepared_image, request.user)
            # Room for the separate verifier only where a partition has it: nothing requires
            # it to boot here.
            separate = request.separate_verifier
            if separate is not None and self._partition(request.needs, separate=separate) is None:
                separate = None
            chain = await self._allocate(
                request.needs,
                image=resolved.observed_identity,
                archive=request.prepared_image.runtime_ref,
                separate=separate,
                # A separate verifier's comes mid-episode, the agent's work done.
                patient=request.role is SandboxRole.VERIFIER,
            )
            index = chain.reserve(request.resources)
            assert index is not None, "an allocation holds the Sandbox it was sized for"
            loaded = chain.seed
            # A candidate is a volume where the image has a directory, which the step finds.
            candidates = request.volumes
            specs = [dict(path=path, kind="fresh", copied=True) for path in engines]
        else:
            chain, index, resolved, loaded, specs = await self._boot(request, engines)
        try:
            return await self._start(
                chain, index, request, resolved, loaded, specs, candidates=candidates
            )
        except BaseException:
            await asyncio.shield(self.release(chain, index))
            raise

    async def _boot(
        self, request: SandboxRequest, engines: Sequence[str]
    ) -> tuple[_Chain, str, ResolvedImage, str, list[dict[str, object]]]:
        """Where a Sandbox booted from a snapshot, or one of another image that mounts views
        of a snapshot's volumes, runs: in the allocation that holds the snapshot while it has
        room, loading its image there if it is another's, else in one of its own that loads
        the snapshot or that image, and the snapshot's volume layers."""
        handle = request.snapshot or request.volumes_from
        assert handle is not None
        name, index = _parse(handle)
        found = self.snapshots.get(handle)
        if found is not None:
            chain, snapshot = found
            image, prepared, stacks = snapshot.image, snapshot.prepared_identity, snapshot.stacks
        else:
            # Written out by an earlier process, whose allocation has ended.
            try:
                saved = json.loads((cache() / SNAPSHOTS / f"{name}-{index}.json").read_text())
            except FileNotFoundError:
                raise ProviderCapabilityError(f"Slurm holds no snapshot {handle}") from None
            chain, snapshot = None, None
            image, prepared = saved["image"], saved["prepared_identity"]
            stacks = dict(saved.get("stacks") or {})
        viewing = request.volumes_from is not None
        if viewing:
            resolved = _resolved(request.prepared_image, request.user)
        else:
            if prepared != request.prepared_image.prepared_identity:
                raise ProviderCapabilityError(
                    f"{handle!r} was not taken from the Task's prepared solver image"
                )
            resolved = ResolvedImage(
                kind=ImageKind.CONTAINER,
                prepared_identity=prepared,
                observed_identity=image,
                observed_ref=image,
            )
        specs = _volume_specs(stacks, engines, viewing=viewing)
        needed = {str(layer) for spec in specs for layer in spec.get("layers", ())}
        if chain is not None and snapshot is not None and not await chain.job.state():
            # Slurm ended the allocation, and the store on its node with it.
            if snapshot.archive is None:
                # Each boot from the snapshot says so. Destroying it as the episode ends finds
                # the job gone and closes the allocation.
                ended = await chain.job.ended()
                raise AllocationEndedError(
                    f"Slurm ended job {chain.job.id} ({ended or 'gone'}), and with it snapshot "
                    f"{handle}, which was not written out"
                )
            # Written out before it ended: it boots from its archive, in one of its own.
            with suppress(ProviderStartError):
                await self._close(chain)
            chain = snapshot = None
        if chain is not None and snapshot is not None:
            served = any(
                not unmet(p, request.needs) for p in _profiles(chain.job.partition, self.config)
            )
            lasting = request.duration_sec or MIN_LEFT_SEC
            booted = chain.reserve(request.resources, lasting=lasting) if served else None
            if booted is not None:
                try:
                    loaded = await self._load(chain, request.prepared_image) if viewing else image
                except BaseException:
                    chain.running.pop(booted, None)
                    raise
                if not viewing:
                    snapshot.boots = max(0, snapshot.boots - 1)
                chain.viewing[booted] = needed
                return chain, booted, resolved, loaded, specs
            archive = snapshot.archive or await self.write_out(chain, snapshot)
            if not viewing:
                # It boots in an allocation of its own, so no more in this one.
                snapshot.boots = max(0, snapshot.boots - 1)
            if chain.lasts(MIN_LEFT_SEC):
                # Rather than idle until the episode ends, it ends once nothing needs it.
                await self._end_if_idle(chain)
            else:
                await self._drain(chain)
        else:
            archive = f"{SNAPSHOTS}/{name}-{index}.tar"
        layers = {
            layer: f"{SNAPSHOTS}/{worker.layer_archive(Path(archive).name, layer)}"
            for layer in needed
        }
        # Mid-episode: the snapshot holds the agent's work, and a wait in the queue spends
        # none of its time, so it waits for as long as Slurm keeps the job pending.
        own = await self._allocate(
            request.needs,
            image=resolved.observed_identity if viewing else image,
            archive=request.prepared_image.runtime_ref if viewing else archive,
            layers=layers,
            patient=True,
        )
        booted = own.reserve(request.resources)
        assert booted is not None, "an allocation holds the Sandbox it was sized for"
        own.viewing[booted] = needed
        return own, booted, resolved, own.seed, specs

    async def _load(self, chain: _Chain, image: PreparedTaskImage) -> str:
        """Load an image prepared for the cluster into an allocation that runs; return the
        ID Podman gives it."""
        loaded = image.prepared_identity
        if loaded != chain.seed and loaded not in chain.images:
            await chain.job.control(
                "load", image.runtime_ref, loaded.removeprefix("sha256:"), timeout=3600
            )
            chain.images.add(loaded)
        return loaded

    async def _allocate(
        self,
        needs: SandboxNeeds,
        *,
        image: str,
        archive: str,
        layers: dict[str, str] | None = None,
        separate: Resources | None = None,
        patient: bool = False,
    ) -> _Chain:
        """An allocation for ``needs``, and the ``separate`` verifier, seeded with ``image``
        from its ``archive`` below the cache: an image prepared for the cluster, or a
        written-out snapshot, which Podman loads under its own ID, with that snapshot's
        volume ``layers``, by name, below the cache. It waits in the queue for
        ``queue_timeout_sec``, or, ``patient``, for as long as Slurm keeps it pending."""
        part = self._partition(needs, separate=separate)
        if part is None:
            raise ProviderCapabilityError(
                "no configured Slurm partition serves that single-node allocation"
            )
        cpus, memory_mb, gpus = _capacity(needs, separate)
        job = Job(part)
        try:
            job.stage(
                dict(
                    archive=archive,
                    image=image.removeprefix("sha256:"),
                    version=gvisor.VERSION,
                    gpus=gpus,
                    layers=layers or {},
                )
            )
            await job.submit(
                cpus=cpus,
                memory_mb=memory_mb + JOB_OVERHEAD_MB,
                gpus=gpus,
                minutes=self.config.walltime_minutes,
            )
            ready = await job.ready(None if patient else self.config.queue_timeout_sec)
            left = await job.time_left()
        except BaseException:
            await asyncio.shield(job.stop())
            raise
        deadline = None
        if left is not None:
            # Slurm's own count, so neither clock's offset matters.
            drain = timedelta(minutes=self.config.drain_minutes)
            deadline = datetime.now(UTC) + timedelta(seconds=left) - drain
        return _Chain(
            job=job,
            ready=ready,
            seed=image,
            cpus=cpus,
            memory_mb=memory_mb,
            gpus=gpus,
            deadline=deadline,
        )

    async def _start(
        self,
        chain: _Chain,
        index: str,
        request: SandboxRequest,
        resolved: ResolvedImage,
        loaded: str,
        volumes: list[dict[str, object]],
        *,
        candidates: Sequence[str] = (),
    ) -> SlurmSandbox:
        """Start a Sandbox in ``chain`` from the image Podman holds there as ``loaded``, with
        ``volumes`` made in the allocation's store, and one of its own at each of the volume
        ``candidates`` that is a directory in the image."""
        gateway, listeners, targets = tunnel_plan(request)
        resources = request.resources
        env = dict(request.env)
        env[CONTAINER_VARIABLE] = str(request.desktop_resolution)
        if gateway:
            env["ORBIT_GATEWAY_URL"] = gateway
        runtime, args = _runtime(resources.container_features)
        sealed = request.network.mode is not NetworkMode.OPEN
        steps = SandboxSteps(chain.job, index)
        stack = AsyncExitStack()
        try:
            ready = await chain.job.start(
                index,
                dict(
                    image=loaded.removeprefix("sha256:"),
                    runtime_args=args,
                    gpus=resources.gpus,
                    cpus=resources.cpus,
                    memory_mb=step_memory_mb(resources.memory_mb),
                    shm_mb=memory.shm_mb(resources.memory_mb),
                    env=env,
                    sealed=sealed,
                    volumes=volumes,
                    candidates=list(candidates),
                ),
                timeout=START_TIMEOUT_SEC,
            )
            watcher = await memory.GuestEvents.start(
                steps.step(
                    "exec", guestd.PYTHON["linux"], "-I", "-c", memory.WATCHER, gvisor.OOM_KILLS
                ),
                counter="the Sentry",
            )
            stack.push_async_callback(watcher.close)
            client = await _guest(steps, ready, request.user, watcher, stack)
            file_client = await _guest(steps, ready, request.user, watcher, stack)
            health, _ = await asyncio.gather(
                client.health(timeout_sec=90), file_client.health(timeout_sec=90)
            )
            home = str(health.get("agent_home") or "")
            if not home:
                raise ProviderStartError("Slurm guestd did not report an agent home")
            await set_and_prove_elevation(client, request, request.user)
            gpu = await _gpu_allocation(client, ready, resources, chain)
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
                tunnel = GatewayTunnel(steps, listeners=listeners, targets=targets)
                stack.push_async_callback(tunnel.close)
                await tunnel.start()
            sandbox = SlurmSandbox(
                self,
                chain,
                steps,
                stack,
                client,
                file_client,
                request=request,
                resolved_image=resolved,
                allocation=ResourceAllocation(
                    cpus=resources.cpus,
                    memory_mb=resources.memory_mb,
                    gpu=gpu,
                    sudo=request.sudo,
                    network_mode=request.network.mode,
                    provider=self.name,
                    leaf=self.name,
                    runtime=runtime,
                    node=chain.ready.node,
                    desktop_resolution=desktop,
                ),
                home=home,
                gateway=gateway,
                volumes=(
                    *ready.volumes,
                    *(str(v["path"]) for v in volumes if not v.get("copied")),
                ),
            )
            sandbox.deadline = chain.deadline
            sandbox.state = SandboxState.READY
            if sealed:
                await sandbox.close_egress()
            return sandbox
        except BaseException:
            await stack.aclose()
            raise

    async def release(self, chain: _Chain, index: str) -> None:
        """End a Sandbox's step, then its allocation if nothing it holds is needed."""
        try:
            if not chain.closed:
                await chain.job.end(index)
        finally:
            chain.running.pop(index, None)
            chain.viewing.pop(index, None)
        await self._close_if_idle(chain)

    async def _close_if_idle(self, chain: _Chain) -> None:
        if chain.closed or not chain.idle:
            return
        await self._close(chain)

    async def _close(self, chain: _Chain) -> None:
        """End the allocation; Sandboxes boot from its snapshots' written-out files now."""
        chain.closed = True
        for snapshot in chain.snapshots.values():
            self.snapshots.pop(_handle(chain.job, snapshot.index), None)
        await asyncio.shield(chain.job.stop())

    async def _drain(self, chain: _Chain) -> None:
        """End an allocation whose deadline has come once what it holds goes on elsewhere: no
        Sandbox starts there any more, so its snapshots are written out and none is to boot
        there."""
        for snapshot in chain.snapshots.values():
            if snapshot.archive is None:
                await self.write_out(chain, snapshot)
            snapshot.boots = 0
        await self._end_if_idle(chain)

    async def _end_if_idle(self, chain: _Chain) -> None:
        """End the allocation if it now holds nothing its episode still needs there. A failed
        end is logged rather than raised: nothing that goes on needs it, and Slurm ends it at
        its wall time."""
        try:
            await self._close_if_idle(chain)
        except ProviderStartError as error:
            logging.getLogger("orbit.execution").warning(
                "a Slurm allocation nothing needs did not end; it ends at its wall time",
                extra={"orbit_data": {"job": chain.job.id, "error": str(error)}},
            )

    async def write_out(self, chain: _Chain, snapshot: _Snapshot) -> str:
        """Write a snapshot to the cluster's shared file system, from which an allocation of
        its own loads it, and return where below the cache."""
        name = f"{chain.job.name}-{snapshot.index}"
        archive = f"{SNAPSHOTS}/{name}.tar"
        await chain.job.control(
            "export",
            snapshot.image.removeprefix("sha256:"),
            archive,
            *sorted(snapshot.layers),
            timeout=3600,
        )
        record = dict(
            handle=_handle(chain.job, snapshot.index),
            image=snapshot.image,
            prepared_identity=snapshot.prepared_identity,
            episode=snapshot.episode_id,
            roles=list(snapshot.roles),
            reason=snapshot.reason,
            stacks=snapshot.stacks,
        )
        write(cache() / SNAPSHOTS / f"{name}.json", json.dumps(record).encode())
        snapshot.archive = archive
        return archive

    async def list_retained(self) -> list[dict[str, object]]:
        records: dict[str, dict[str, object]] = {}
        for path in sorted((cache() / SNAPSHOTS).glob("*.json")):
            try:
                saved = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            records[saved["handle"]] = _record(
                saved["handle"], saved["episode"], saved["roles"], saved["image"]
            )
        for handle, (_, snapshot) in self.snapshots.items():
            records[handle] = _record(handle, snapshot.episode_id, snapshot.roles, snapshot.image)
        return sorted(records.values(), key=lambda item: str(item["handle"]))

    async def destroy_retained(self, handle: str) -> None:
        name, index = _parse(handle)
        found = self.snapshots.get(handle)
        if found is not None:
            chain, snapshot = found
            if not chain.closed:
                try:
                    # Not forced: a Sandbox booted from the snapshot still uses its layers.
                    await chain.job.control("rmi", snapshot.index)
                except ProviderStartError:
                    if await chain.job.state():
                        raise
                    # Slurm ended the allocation, and the node's store with it.
                    await self._close(chain)
            self.snapshots.pop(handle, None)
            chain.snapshots.pop(index, None)
            if not chain.closed:
                # Its volumes' layers that no other snapshot and no running view stacks.
                kept = {layer for other in chain.snapshots.values() for layer in other.layers}
                kept.update(layer for held in chain.viewing.values() for layer in held)
                if dropped := sorted(snapshot.layers - kept):
                    await chain.job.control("drop", *dropped, timeout=600)
        stem = f"{name}-{index}"
        for path in (cache() / SNAPSHOTS).glob(f"{stem}.*"):
            path.unlink(missing_ok=True)
        if found is not None:
            await self._close_if_idle(found[0])


def _record(handle: str, episode: str, roles: Sequence[str], image: str) -> dict[str, object]:
    return {
        "provider": "slurm",
        "handle": handle,
        "episode": episode,
        "role": ",".join(roles),
        "image": image,
        "gpus": (),
        "running": False,
        "cleanup_command": f"orbit sandbox destroy {handle}",
    }
