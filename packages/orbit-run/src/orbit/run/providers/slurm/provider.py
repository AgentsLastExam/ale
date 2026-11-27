"""Rootless gVisor Sandboxes in Slurm allocations, driven from a login node of the cluster,
where Orbit runs.

A solver gets an allocation of its own, sized for it and for the verifiers its request
names, and every Sandbox booted from one of its snapshots, or mounting views of its volumes,
starts in that allocation while it has room, from its nodes' own stores: a snapshot is never
copied for its episode. The allocation ends with the last Sandbox and snapshot in it. Images
are prepared on the cluster too ([`images`](images.py)).

A Sandbox that spans several nodes runs on the allocation's first nodes, one container on
each: node 0 is where commands and files go, and every node gets the same network, its
Gateway and egress relay, the others on a network of their own, which ssh reaches, and a
shared directory of the cluster's file system at ``/shared``. A snapshot keeps every node's
disk and the shared directory: node ``k`` of a Sandbox booted from it starts from its node
``k``, and node ``k`` of one viewing its volumes views its node ``k``'s; a verifier sees the
shared directory as it was, read-only, and any other Sandbox a copy of its own.
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
from orbit.run.guestd import nodes as ssh
from orbit.run.providers import memory, relay, storage
from orbit.run.providers.desktop import CONTAINER_VARIABLE, require_resolution
from orbit.run.providers.docker import gvisor
from orbit.run.providers.privileges import set_and_prove_elevation
from orbit.run.providers.slurm import images, worker
from orbit.run.providers.slurm.images import Recipe
from orbit.run.providers.slurm.job import (
    SHARED,
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
#: How long copying a snapshot's shared directory for a Sandbox booted from it may take.
COPY_TIMEOUT_SEC = 4 * 3600
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
            max_nodes=partition.max_nodes,
            container_features=frozenset(common | {feature}),
            volumes=True,
            storage_limit=True,
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
    """CPUs, step memory and GPUs a Sandbox of ``resources`` holds on each of its nodes."""
    return resources.cpus, step_memory_mb(resources.memory_mb), resources.gpus


def _capacity(needs: SandboxNeeds, separate: Resources | None = None) -> tuple[int, int, int, int]:
    """The allocation that holds a Sandbox and, one at a time, each verifier booted from its
    snapshot and the ``separate`` verifier that mounts views of its volumes: each node's
    CPUs, step memory and GPUs, and its nodes."""
    sizes = [(*_size(needs.resources), needs.resources.nodes)]
    verifier = needs.verifier
    while verifier is not None:
        sizes.append((*_size(verifier.resources), verifier.resources.nodes))
        verifier = verifier.verifier
    if separate is not None:
        sizes.append((*_size(separate), separate.nodes))
    cpus, memory_mb, gpus, nodes = zip(*sizes, strict=True)
    return max(cpus), max(memory_mb), max(gpus), max(nodes)


def _handle(job: Job, index: str) -> str:
    return f"{PREFIX}{job.name}/{index}"


def _parse(handle: str) -> tuple[str, str]:
    name, _, index = handle.removeprefix(PREFIX).partition("/")
    if not handle.startswith(PREFIX) or not (name.startswith("orbit-") and index.isdigit()):
        raise ProviderCapabilityError(f"{handle!r} is no Slurm snapshot handle")
    return name, index


def _resolved(
    config: SlurmProviderConfig, image: PreparedTaskImage, user: str | None = None
) -> ResolvedImage:
    """An image prepared on the cluster, which the cache holds as it was prepared, with the
    framework for ``user`` where one is asked for."""
    record = images.recorded(config, image) if image.kind is ImageKind.CONTAINER else None
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
    images: tuple[str, ...]
    """Each node's commit, by its number in the Sandbox, in its store."""
    prepared_identity: str
    episode_id: str
    roles: tuple[Literal["solver", "verifier"], ...]
    reason: str
    boots: int
    """The Sandboxes its episode is still to boot from it in its allocation, which keep the
    allocation: a snapshot verifier, until it boots or the allocation's deadline comes."""
    stacks: tuple[dict[str, dict[str, object]], ...] = ()
    """For each node, each volume's layers in its store, topmost first, its root's owner and
    mode, and whether it is an engine's storage, by path."""
    archives: tuple[str, ...] = ()
    """Below the cache, each node's, once it was written for another allocation to boot
    from."""
    written: tuple[int, ...] = ()
    """What each node held over its image as it was saved, in bytes, its volumes' too, which
    the ``storage_mb`` of a Sandbox booted from it counts."""
    shared: str | None = None
    """The Sandbox's shared directory, below the cache, which no one changes any more."""

    def layers_of(self, rank: int) -> set[str]:
        """The layers of node ``rank``'s volumes, which its node's store holds."""
        stacks = self.stacks[rank] if rank < len(self.stacks) else {}
        return {str(layer) for stack in stacks.values() for layer in stack["layers"]}

    @property
    def layers(self) -> set[str]:
        return {layer for rank in range(len(self.stacks)) for layer in self.layers_of(rank)}


@dataclass(eq=False)
class _Chain:
    """An allocation and what runs and is saved in it."""

    job: Job
    ready: JobReady
    seeds: tuple[str, ...]
    """The image each node of the allocation loaded, by the ID Podman gives it."""
    cpus: int
    memory_mb: int
    gpus: int
    """Each node's."""
    running: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    """Each running Sandbox's CPUs, step memory and GPUs on each of the nodes it spans, and
    how many it spans, from the first."""
    snapshots: dict[str, _Snapshot] = field(default_factory=dict)
    images: set[str] = field(default_factory=set)
    """The images loaded since on every node, a separate verifier's, by the ID Podman gives
    them."""
    viewing: dict[str, set[str]] = field(default_factory=dict)
    """The layers each running Sandbox's views stack, which stay while it runs."""
    deadline: datetime | None = None
    """When Orbit must begin to save what runs here, before Slurm ends the allocation at its
    wall time, which leaves the drain to save it and write it out."""
    count: int = 0
    closed: bool = False

    @property
    def nodes(self) -> tuple[str, ...]:
        return self.ready.nodes

    def reserve(self, resources: Resources, *, lasting: float | None = None) -> str | None:
        """A new Sandbox's index in the allocation, if it fits beside what runs on each of
        the nodes it spans and, where it is to last ``lasting`` seconds, the allocation lasts
        that long."""
        size, count = _size(resources), resources.nodes
        capacity = (self.cpus, self.memory_mb, self.gpus)
        if self.closed or count > len(self.nodes):
            return None
        for node in range(count):
            # Every Sandbox spans the allocation's first nodes.
            used = [
                sum(held[part] for held in self.running.values() if held[3] > node)
                for part in range(3)
            ]
            if any(u + s > c for u, s, c in zip(used, size, capacity, strict=True)):
                return None
        if lasting is not None and not self.lasts(lasting):
            return None
        self.count += 1
        index = str(self.count)
        self.running[index] = (*size, count)
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
            snapshot.archives and not snapshot.boots for snapshot in self.snapshots.values()
        )


async def _guest(
    steps: SandboxSteps,
    ready: StepReady,
    user: str,
    watchers: Sequence[memory.GuestEvents],
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
        watchers,
    )
    client = GuestClient(transport)
    stack.push_async_callback(client.close)
    await transport.start()
    return client


async def _gpu_allocation(
    clients: Sequence[GuestClient],
    readies: Sequence[StepReady],
    resources: Resources,
    chain: _Chain,
) -> GpuAllocation | None:
    """Check each node's GPUs, as the step and the guest see them, and register the driver's
    libraries there; every node's, as one allocation."""
    if not resources.gpus:
        return None
    expected = chain.job.partition.gpu_model
    assert expected is not None
    for client, ready in zip(clients, readies, strict=True):
        ids = tuple(device.id for device in ready.devices)
        for device in ready.devices:
            if (
                not accepts_model((expected.name,), device.model)
                or not accepts_model(resources.gpu_types, device.model)
                or (device.memory_total_mb or 0)
                < max(expected.memory_mb or 0, resources.gpu_memory_mb or 0)
            ):
                raise ProviderCapabilityError(
                    f"Slurm allocated an incompatible GPU on {ready.node}: {device}"
                )
        code, _, err, _ = await client.exec(["ldconfig"])
        if code:
            raise ProviderStartError(f"could not register Slurm's GPU driver libraries: {err}")
        code, out, err, _ = await client.exec(
            ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"]
        )
        if code or set(out.split()) != set(ids) or len(set(ids)) != resources.gpus:
            raise ProviderStartError(
                f"Slurm GPU allocation does not match guest devices on {ready.node}: {out} {err}"
            )
    devices = tuple(device for ready in readies for device in ready.devices)
    ids = tuple(device.id for device in devices)
    return GpuAllocation(
        requested_count=resources.gpus * len(readies),
        provider_addresses=ids,
        lease_keys=ids,
        observed_devices=devices,
        runtime_identity=chain.ready.runtime,
    )


class SlurmSandbox(TunneledSandbox):
    """A Sandbox on one or more nodes of an allocation. Commands and files go to node 0; the
    seal, the egress relay and the proxy variables apply on every node."""

    def __init__(
        self,
        provider: SlurmProvider,
        chain: _Chain,
        nodes: Sequence[SandboxSteps],
        clients: Sequence[GuestClient],
        connections: AsyncExitStack,
        file_client: GuestClient,
        *,
        request: SandboxRequest,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        home: str,
        gateway: str | None,
        volumes: tuple[str, ...],
        variables: Sequence[dict[str, str]] = (),
    ) -> None:
        super().__init__(
            sandbox_id=f"{chain.job.id}.{nodes[0].index}",
            request=request,
            resolved_image=resolved_image,
            allocation=allocation,
            agent_user=request.user,
            agent_home=home,
        )
        self.provider = provider
        self.chain = chain
        self.steps = nodes[0]
        self.nodes = tuple(nodes)
        self.connections = connections
        self._clients = tuple(clients)
        self._client, self._file_client = clients[0], file_client
        self._local_gateway = gateway
        self._relay = None
        self._sealed = request.network.mode is not NetworkMode.OPEN
        if len(nodes) > 1:
            self._peers = (
                *(f"node{rank}" for rank in range(len(nodes))),
                worker.GROUP + "0/24",
            )
        #: Each node's group variables, which node 0's commands get from here and every
        #: node's ssh sessions from its sshd.
        self._node_variables = tuple(variables)
        if variables:
            self._variables = variables[0]
        self.volumes = volumes
        #: The nodes' ssh keys (``guestd/nodes.py``), new for each Sandbox of several.
        self.keys: dict[str, list[str]] = {}
        #: Its shared directory, below the cache.
        self.shared: str | None = None

    async def _network(self, mode: str) -> None:
        await self.chain.job.control(
            mode, self.steps.index, nodes=[node.node for node in self.nodes]
        )

    async def join(self, relays: Sequence[str | None]) -> None:
        """Join the nodes of a Sandbox of several with ssh, again as what a command takes
        changes: each node's sessions get what guestd's commands get on node 0, the node's
        group variables and, while the Sandbox is sealed, the proxy variables of its node's
        relay (``guestd/nodes.py``)."""
        count = len(self.nodes)
        if count < 2:
            return
        for rank, (client, address, own) in enumerate(
            zip(self._clients, relays, self._node_variables, strict=True)
        ):
            proxied = relay.variables(address, "127.0.0.1", self._peers) if address else {}
            config = dict(
                rank=rank,
                names=[f"node{each}" for each in range(count)],
                addresses=[f"{worker.GROUP}{each + 1}" for each in range(count)],
                account=self.request.user,
                keys=self.keys,
                variables=own | proxied,
            )
            # The keys go by a root process's environment, never a command line.
            code, _, error, _ = await client.exec(
                [guestd.PYTHON["linux"], "-I", "/opt/orbit/guestd/nodes.py", "join"],
                env={ssh.VARIABLE: json.dumps(config)},
            )
            if code:
                raise ProviderStartError(f"could not join node {rank} with ssh: {error.strip()}")

    async def open_egress(self) -> None:
        if self._sealed:
            for client in self._clients:
                await relay.stop(client, self.request)
            await self.join([None] * len(self._clients))
            await self._network("open")
            self._relay = None
            self._sealed = False

    async def close_egress(self) -> None:
        if self.request.network.mode is NetworkMode.OPEN:
            return
        if not self._sealed:
            await self._network("block")
        self._sealed = True
        relays = [
            await relay.start(
                client,
                self.request,
                self.allocation,
                proxy=f"http://127.0.0.1:{PROXY_PORT}",
                host="127.0.0.1",
                peers=self._peers,
            )
            for client in self._clients
        ]
        self._relay = relays[0]
        await self.join(relays)

    async def snapshot(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        """Stop the Sandbox and commit each node's disk to its node's store, which keeps it for
        the Sandboxes its episode boots from it, with its volumes' layers. A snapshot its Run
        keeps after the episode is written to the cluster's shared file system too, as the
        allocation ends then."""
        # The relay and the guest's sessions end before the container stops.
        for client in self._clients:
            await relay.stop(client, self.request)
        self._relay = None
        await self.connections.aclose()
        saved = await self.chain.job.save(self.steps.index, nodes=len(self.nodes))
        snapshot = _Snapshot(
            index=self.steps.index,
            images=tuple(image for image, _, _ in saved),
            prepared_identity=self.request.prepared_image.prepared_identity,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            boots=int(self.request.snapshot_verifier is not None),
            stacks=tuple(stacks for _, stacks, _ in saved),
            written=tuple(written for _, _, written in saved),
            shared=self.shared,
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
            try:
                await self.provider.release(self.chain, self.steps.index)
            finally:
                if self.shared is not None:
                    shared, self.shared = self.shared, None
                    self.provider.shared_users[shared] -= 1
                    await self.provider.release_shared(shared)
        self.state = SandboxState.DESTROYED


class SlurmProvider(Provider):
    name = "slurm"

    def __init__(self, config: SlurmProviderConfig) -> None:
        self.config = config
        #: The snapshots this process saved, by handle, with the allocation that holds them.
        self.snapshots: dict[str, tuple[_Chain, _Snapshot]] = {}
        #: How many running Sandboxes use each shared directory, below the cache.
        self.shared_users: dict[str, int] = {}

    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        return tuple(
            profile for p in self.config.partitions for profile in _profiles(p, self.config)
        )

    def _partition(
        self, needs: SandboxNeeds, *, separate: Resources | None = None
    ) -> SlurmPartition | None:
        cpus, memory_mb, gpus, nodes = _capacity(needs, separate)
        for part in self.config.partitions:
            if (
                bool(gpus) != bool(part.max_gpus)
                or gpus > part.max_gpus
                or cpus > part.max_cpus
                or memory_mb + JOB_OVERHEAD_MB > part.max_memory_mb
                or nodes > part.max_nodes
            ):
                continue
            if any(not unmet(profile, needs) for profile in _profiles(part, self.config)):
                return part
        return None

    def refusals(self, needs: SandboxNeeds, *, user: str, role: SandboxRole) -> tuple[str, ...]:
        if self._partition(needs) is None:
            return (
                "no configured Slurm partition serves that allocation"
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
                    "the cluster, which shares its cache with the compute nodes"
                )
        for part in dict.fromkeys(partitions):
            await slurm("scontrol", "show", "partition", part.name, "-o")

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        """Check an image prepared for the cluster: one a Task names is prepared there from
        its recipe (:meth:`prepare_recipe`), as no container runtime runs here."""
        if isinstance(image, ImageRef):
            if image.kind is not ImageKind.CONTAINER:
                raise ProviderCapabilityError("Slurm requires a Linux container image")
            # For the account Orbit's images run their agent as. No Task says whether one of
            # its Sandboxes spans several nodes, so the image carries the nodes' OpenSSH.
            pinned = await images.pin(image.reference)
            prepared, _ = await self.prepare_recipe(
                Recipe(
                    key=images.key(
                        "external-ref",
                        reference=pinned,
                        context=None,
                        user=USER,
                        keep_alive=False,
                        multinode=True,
                    ),
                    source="external-ref",
                    reference=pinned,
                    user=USER,
                    multinode=True,
                )
            )
            return prepared
        _resolved(self.config, image)
        return image

    async def prepare_recipe(self, recipe: Recipe) -> tuple[PreparedTaskImage, bool]:
        """Prepare an image on the cluster, once per key; return it and whether an earlier
        preparation had."""
        return await images.prepare(self.config, recipe)

    async def create(self, request: SandboxRequest) -> SlurmSandbox:
        await self.accepts(request)
        engines = _engines(request.resources)
        count = request.resources.nodes
        candidates: tuple[str, ...] = ()
        shared: str | None = None
        written: tuple[int, ...] = ()
        if request.snapshot is None and request.volumes_from is None:
            resolved = _resolved(self.config, request.prepared_image, request.user)
            # Room for the separate verifier only where a partition has it: nothing requires
            # it to boot here.
            separate = request.separate_verifier
            if separate is not None and self._partition(request.needs, separate=separate) is None:
                separate = None
            chain = await self._allocate(
                request.needs,
                seeds=[_seed(request.prepared_image.runtime_ref, resolved.observed_identity, {})],
                separate=separate,
                # A separate verifier's comes mid-episode, the agent's work done.
                patient=request.role is SandboxRole.VERIFIER,
            )
            index = chain.reserve(request.resources)
            assert index is not None, "an allocation holds the Sandbox it was sized for"
            loaded = list(chain.seeds[:count])
            # A candidate is a volume where the image has a directory, which the step finds.
            candidates = request.volumes
            specs = [[dict(path=path, kind="fresh", copied=True) for path in engines]] * count
        else:
            # Before the boot, which may end the allocation that holds the snapshot.
            shared, written = self._snapshot_state(request.snapshot or request.volumes_from)
            if request.snapshot is None:
                written = ()
            chain, index, resolved, loaded, specs = await self._boot(request, engines)
        readonly = shared is not None
        try:
            if shared is None and count > 1:
                shared, readonly = f"{SHARED}/{chain.job.name}-{index}", False
                (cache(self.config) / shared).mkdir(mode=0o700, parents=True)
            elif shared is not None and (
                request.volumes_from is None and request.role is not SandboxRole.VERIFIER
            ):
                # The snapshot's stays as it is, for whatever else boots from it.
                source, shared, readonly = shared, f"{SHARED}/{chain.job.name}-{index}", False
                await chain.job.control(
                    "copy", source, shared, nodes=(chain.nodes[0],), timeout=COPY_TIMEOUT_SEC
                )
            return await self._start(
                chain,
                index,
                request,
                resolved,
                loaded,
                specs,
                candidates=candidates,
                shared=(shared, readonly) if shared is not None else None,
                written=written,
            )
        except BaseException:
            await asyncio.shield(self.release(chain, index))
            await asyncio.shield(self.release_shared(shared))
            raise

    def _snapshot_state(self, handle: str) -> tuple[str | None, tuple[int, ...]]:
        """The shared directory a snapshot keeps, and what each of its nodes held."""
        found = self.snapshots.get(handle)
        if found is not None:
            return found[1].shared, found[1].written
        name, index = _parse(handle)
        try:
            saved = json.loads(
                (cache(self.config) / SNAPSHOTS / f"{name}-{index}.json").read_text()
            )
            return saved.get("shared"), tuple(int(each) for each in saved.get("written") or ())
        except (OSError, ValueError, TypeError, AttributeError):
            return None, ()  # Its boot says why.

    async def release_shared(self, path: str | None) -> None:
        """Remove a shared directory, below the cache, once no running Sandbox uses it and no
        snapshot keeps it, this process's or one written out."""
        if path is None or self.shared_users.get(path):
            return
        if any(snapshot.shared == path for _, snapshot in self.snapshots.values()):
            return
        for record in (cache(self.config) / SNAPSHOTS).glob("*.json"):
            with suppress(OSError, ValueError, AttributeError):
                if json.loads(record.read_text()).get("shared") == path:
                    return
        await asyncio.to_thread(worker.remove_tree, str(cache(self.config) / path))

    async def _boot(
        self, request: SandboxRequest, engines: Sequence[str]
    ) -> tuple[_Chain, str, ResolvedImage, list[str], list[list[dict[str, object]]]]:
        """Where a Sandbox booted from a snapshot, or one of another image that mounts views
        of a snapshot's volumes, runs: in the allocation that holds the snapshot while it has
        room, loading its image there if it is another's, else in one of its own that loads
        the snapshot or that image, and the snapshot's volume layers. Node ``k`` boots from
        the snapshot's node ``k``, or views its volumes; a viewing node the snapshot lacks
        views none."""
        handle = request.snapshot or request.volumes_from
        assert handle is not None
        name, index = _parse(handle)
        found = self.snapshots.get(handle)
        if found is not None:
            chain, snapshot = found
            images, prepared, stacks = snapshot.images, snapshot.prepared_identity, snapshot.stacks
        else:
            # Written out by an earlier process, whose allocation has ended.
            try:
                saved = json.loads(
                    (cache(self.config) / SNAPSHOTS / f"{name}-{index}.json").read_text()
                )
            except FileNotFoundError:
                raise ProviderCapabilityError(f"Slurm holds no snapshot {handle}") from None
            chain, snapshot = None, None
            try:
                images, prepared = tuple(saved["images"]), saved["prepared_identity"]
                stacks = tuple(dict(each) for each in saved["stacks"])
            except (KeyError, TypeError, ValueError):
                raise ProviderCapabilityError(
                    f"{handle!r} was written out by an engine that records snapshots otherwise"
                ) from None
        viewing = request.volumes_from is not None
        count = request.resources.nodes
        if not viewing and count > len(images):
            raise ProviderCapabilityError(
                f"{handle!r} keeps {len(images)} node(s), fewer than the {count} the Sandbox "
                "booted from it spans"
            )
        if viewing:
            resolved = _resolved(self.config, request.prepared_image, request.user)
        else:
            if prepared != request.prepared_image.prepared_identity:
                raise ProviderCapabilityError(
                    f"{handle!r} was not taken from the Task's prepared solver image"
                )
            resolved = ResolvedImage(
                kind=ImageKind.CONTAINER,
                prepared_identity=prepared,
                observed_identity=images[0],
                observed_ref=images[0],
            )
        specs = [
            _volume_specs(stacks[rank] if rank < len(stacks) else {}, engines, viewing=viewing)
            for rank in range(count)
        ]
        needed = [
            {str(layer) for spec in each for layer in spec.get("layers", ())} for each in specs
        ]
        if chain is not None and snapshot is not None and not await chain.job.state():
            # Slurm ended the allocation, and the store on its nodes with it.
            if not snapshot.archives:
                # Each boot from the snapshot says so. Destroying it as the episode ends finds
                # the job gone and closes the allocation.
                ended = await chain.job.ended()
                raise AllocationEndedError(
                    f"Slurm ended job {chain.job.id} ({ended or 'gone'}), and with it snapshot "
                    f"{handle}, which was not written out"
                )
            # Written out before it ended: it boots from its archives, in one of its own.
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
                    loaded = (
                        [await self._load(chain, request.prepared_image)] * count
                        if viewing
                        else list(images[:count])
                    )
                except BaseException:
                    chain.running.pop(booted, None)
                    raise
                if not viewing:
                    snapshot.boots = max(0, snapshot.boots - 1)
                chain.viewing[booted] = set().union(*needed)
                return chain, booted, resolved, loaded, specs
            archives = list(snapshot.archives or await self.write_out(chain, snapshot))
            if not viewing:
                # It boots in an allocation of its own, so no more in this one.
                snapshot.boots = max(0, snapshot.boots - 1)
            if chain.lasts(MIN_LEFT_SEC):
                # Rather than idle until the episode ends, it ends once nothing needs it.
                await self._end_if_idle(chain)
            else:
                await self._drain(chain)
        else:
            archives = [_archive(name, index, rank) for rank in range(len(images))]
        layers = [
            {
                layer: f"{SNAPSHOTS}/{worker.layer_archive(Path(archives[rank]).name, layer)}"
                for layer in needed[rank]
            }
            for rank in range(count)
        ]
        # Mid-episode: the snapshot holds the agent's work, and a wait in the queue spends
        # none of its time, so it waits for as long as Slurm keeps it pending.
        own = await self._allocate(
            request.needs,
            seeds=[
                _seed(request.prepared_image.runtime_ref, resolved.observed_identity, layers[rank])
                if viewing
                else _seed(archives[rank], images[rank], layers[rank])
                for rank in range(count)
            ],
            patient=True,
        )
        booted = own.reserve(request.resources)
        assert booted is not None, "an allocation holds the Sandbox it was sized for"
        own.viewing[booted] = set().union(*needed)
        return own, booted, resolved, list(own.seeds[:count]), specs

    async def _load(self, chain: _Chain, image: PreparedTaskImage) -> str:
        """Load an image prepared for the cluster into an allocation that runs, on each node
        that does not hold it; return the ID Podman gives it."""
        loaded = image.prepared_identity
        lacking = [
            node for node, seed in zip(chain.nodes, chain.seeds, strict=True) if seed != loaded
        ]
        if lacking and loaded not in chain.images:
            await chain.job.control(
                "load",
                image.runtime_ref,
                loaded.removeprefix("sha256:"),
                nodes=lacking,
                timeout=3600,
            )
            chain.images.add(loaded)
        return loaded

    async def _allocate(
        self,
        needs: SandboxNeeds,
        *,
        seeds: Sequence[dict[str, object]],
        separate: Resources | None = None,
        patient: bool = False,
    ) -> _Chain:
        """An allocation for ``needs``, and the ``separate`` verifier, each of whose nodes is
        seeded with an image from its archive below the cache: an image prepared for the
        cluster, or a node of a written-out snapshot, which Podman loads under its own ID,
        with that node's volume layers, by name, below the cache; nodes past ``seeds`` take
        the first. It waits in the queue for ``queue_timeout_sec``, or, ``patient``, for as
        long as Slurm keeps it pending."""
        part = self._partition(needs, separate=separate)
        if part is None:
            raise ProviderCapabilityError("no configured Slurm partition serves that allocation")
        cpus, memory_mb, gpus, nodes = _capacity(needs, separate)
        each = [seeds[rank] if rank < len(seeds) else seeds[0] for rank in range(nodes)]
        job = Job(self.config, part)
        try:
            job.stage(dict(seeds=each, version=gvisor.VERSION, build=gvisor.BUILD_ID, gpus=gpus))
            await job.submit(
                cpus=cpus,
                memory_mb=memory_mb + JOB_OVERHEAD_MB,
                gpus=gpus,
                minutes=self.config.walltime_minutes,
                nodes=nodes,
            )
            ready = await job.ready(None if patient else self.config.queue_timeout_sec)
            if len(ready.nodes) != nodes:
                raise ProviderStartError(
                    f"Slurm job {job.id} has {len(ready.nodes)} nodes, not {nodes}"
                )
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
            seeds=tuple("sha256:" + str(seed["image"]) for seed in each),
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
        loaded: Sequence[str],
        volumes: Sequence[list[dict[str, object]]],
        *,
        candidates: Sequence[str] = (),
        shared: tuple[str, bool] | None = None,
        written: Sequence[int] = (),
    ) -> SlurmSandbox:
        """Start a Sandbox in ``chain`` on its first nodes, node ``k`` from the image Podman
        holds there as ``loaded[k]``, with ``volumes[k]`` made in its store, one of its own at
        each of the volume ``candidates`` that is a directory in the image, the ``shared``
        directory below the cache, read-only or not, and what the snapshot it boots from held
        on each node, ``written[k]``."""
        gateway, listeners, targets = tunnel_plan(request)
        resources = request.resources
        count = resources.nodes
        env = dict(request.env)
        env[CONTAINER_VARIABLE] = str(request.desktop_resolution)
        if gateway:
            env["ORBIT_GATEWAY_URL"] = gateway
        runtime, args = _runtime(resources.container_features)
        sealed = request.network.mode is not NetworkMode.OPEN
        nodes = [SandboxSteps(chain.job, index, node) for node in chain.nodes[:count]]
        spec: dict[str, object] = dict(
            nodes=[
                dict(
                    image=loaded[rank].removeprefix("sha256:"),
                    volumes=volumes[rank],
                    written=written[rank] if rank < len(written) else 0,
                )
                for rank in range(count)
            ],
            runtime_args=args,
            gpus=resources.gpus,
            cpus=resources.cpus,
            memory_mb=step_memory_mb(resources.memory_mb),
            shm_mb=memory.shm_mb(resources.memory_mb),
            env=env,
            sealed=sealed,
            candidates=list(candidates),
        )
        if shared is not None:
            # A Sandbox that views another's volumes holds none of the shared directory it
            # sees, as it holds none of theirs.
            spec["shared"] = dict(
                path=shared[0],
                readonly=shared[1],
                account=request.user,
                counted=request.volumes_from is None,
            )
        if resources.storage_mb is not None:
            spec["storage"] = storage.measure_schedule(resources.storage_mb, writers=count)
        stack = AsyncExitStack()
        try:
            readies = await chain.job.start(index, spec, nodes=count, timeout=START_TIMEOUT_SEC)
            if [ready.node for ready in readies] != [node.node for node in nodes]:
                raise ProviderStartError(
                    f"Slurm ran Sandbox {index} on {[r.node for r in readies]}, not its nodes"
                )
            watchers: list[memory.GuestEvents] = []
            for node in nodes:
                watcher = await memory.GuestEvents.start(
                    node.step(
                        "exec", guestd.PYTHON["linux"], "-I", "-c", memory.WATCHER, gvisor.OOM_KILLS
                    ),
                    counter="the Sentry",
                )
                stack.push_async_callback(watcher.close)
                watchers.append(watcher)
            clients = [
                await _guest(node, ready, request.user, watchers, stack)
                for node, ready in zip(nodes, readies, strict=True)
            ]
            file_client = await _guest(nodes[0], readies[0], request.user, watchers, stack)
            healths = await asyncio.gather(
                *(client.health(timeout_sec=90) for client in (*clients, file_client))
            )
            home = str(healths[0].get("agent_home") or "")
            if not home:
                raise ProviderStartError("Slurm guestd did not report an agent home")
            for client in clients:
                await set_and_prove_elevation(client, request, request.user)
            gpu = await _gpu_allocation(clients, readies, resources, chain)
            desktop = None
            if healths[0].get("gui") and request.role is not SandboxRole.VERIFIER:
                async with asyncio.timeout(120):
                    while True:
                        try:
                            await clients[0].screenshot()
                            break
                        except MemoryExhaustedError:
                            raise
                        except (GuestUnreachableError, TimeoutError):
                            await asyncio.sleep(1)
                desktop = await require_resolution(
                    clients[0], request.desktop_resolution, container=True
                )
            variables = _group_variables(readies)
            if listeners:
                # Every node reaches the Gateway and the egress proxy, as node 0 does.
                for node in nodes:
                    tunnel = GatewayTunnel(node, listeners=listeners, targets=targets)
                    stack.push_async_callback(tunnel.close)
                    await tunnel.start()
            sandbox = SlurmSandbox(
                self,
                chain,
                nodes,
                clients,
                stack,
                file_client,
                request=request,
                resolved_image=resolved,
                allocation=ResourceAllocation(
                    cpus=resources.cpus,
                    memory_mb=resources.memory_mb,
                    storage_mb=resources.storage_mb,
                    gpu=gpu,
                    sudo=request.sudo,
                    network_mode=request.network.mode,
                    provider=self.name,
                    leaf=self.name,
                    runtime=runtime,
                    node=readies[0].node,
                    nodes=tuple(ready.node for ready in readies) if count > 1 else (),
                    desktop_resolution=desktop,
                ),
                home=home,
                gateway=gateway,
                volumes=(
                    *readies[0].volumes,
                    *(str(v["path"]) for v in volumes[0] if not v.get("copied")),
                ),
                variables=variables,
            )
            sandbox.deadline = chain.deadline
            sandbox.state = SandboxState.READY
            if count > 1:
                sandbox.keys = await _keys(clients[0])
            if sealed:
                await sandbox.close_egress()
            else:
                await sandbox.join([None] * count)
            if shared is not None:
                sandbox.shared = shared[0]
                self.shared_users[shared[0]] = self.shared_users.get(shared[0], 0) + 1
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
        """End the allocation; Sandboxes boot from its snapshots' written-out files now, and
        a snapshot never written out goes, its shared directory too."""
        chain.closed = True
        for snapshot in chain.snapshots.values():
            self.snapshots.pop(_handle(chain.job, snapshot.index), None)
        await asyncio.shield(chain.job.stop())
        for snapshot in chain.snapshots.values():
            if not snapshot.archives:
                await asyncio.shield(self.release_shared(snapshot.shared))

    async def _drain(self, chain: _Chain) -> None:
        """End an allocation whose deadline has come once what it holds goes on elsewhere: no
        Sandbox starts there any more, so its snapshots are written out and none is to boot
        there."""
        for snapshot in chain.snapshots.values():
            if not snapshot.archives:
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

    async def write_out(self, chain: _Chain, snapshot: _Snapshot) -> tuple[str, ...]:
        """Write a snapshot to the cluster's shared file system, each node's from that node at
        once, from which an allocation of its own loads it, and return where each node's is
        below the cache."""
        name = f"{chain.job.name}-{snapshot.index}"
        archives = tuple(
            _archive(chain.job.name, snapshot.index, rank) for rank in range(len(snapshot.images))
        )
        # Every node's at once, each to its end before a failure is raised.
        exported = await asyncio.gather(
            *(
                chain.job.control(
                    "export",
                    image.removeprefix("sha256:"),
                    archives[rank],
                    *sorted(snapshot.layers_of(rank)),
                    nodes=(chain.nodes[rank],),
                    timeout=3600,
                )
                for rank, image in enumerate(snapshot.images)
            ),
            return_exceptions=True,
        )
        if failed := next((each for each in exported if isinstance(each, BaseException)), None):
            raise failed
        record = dict(
            handle=_handle(chain.job, snapshot.index),
            images=list(snapshot.images),
            prepared_identity=snapshot.prepared_identity,
            episode=snapshot.episode_id,
            roles=list(snapshot.roles),
            reason=snapshot.reason,
            stacks=list(snapshot.stacks),
            written=list(snapshot.written),
            shared=snapshot.shared,
        )
        write(cache(self.config) / SNAPSHOTS / f"{name}.json", json.dumps(record).encode())
        snapshot.archives = archives
        return archives

    async def list_retained(self) -> list[dict[str, object]]:
        records: dict[str, dict[str, object]] = {}
        for path in sorted((cache(self.config) / SNAPSHOTS).glob("*.json")):
            try:
                saved = json.loads(path.read_text())
                record = _record(
                    saved["handle"], saved["episode"], saved["roles"], saved["images"][0]
                )
            except (OSError, ValueError, KeyError, IndexError):
                # Unreadable, or written by an engine that recorded it otherwise.
                continue
            records[saved["handle"]] = record
        for handle, (_, snapshot) in self.snapshots.items():
            records[handle] = _record(
                handle, snapshot.episode_id, snapshot.roles, snapshot.images[0]
            )
        return sorted(records.values(), key=lambda item: str(item["handle"]))

    async def destroy_retained(self, handle: str) -> None:
        name, index = _parse(handle)
        shared, _ = self._snapshot_state(handle)
        found = self.snapshots.get(handle)
        if found is not None:
            chain, snapshot = found
            if not chain.closed:
                try:
                    # Not forced: a Sandbox booted from the snapshot still uses its layers.
                    await chain.job.control(
                        "rmi", snapshot.index, nodes=chain.nodes[: len(snapshot.images)]
                    )
                except ProviderStartError:
                    if await chain.job.state():
                        raise
                    # Slurm ended the allocation, and its nodes' stores with it.
                    await self._close(chain)
            self.snapshots.pop(handle, None)
            chain.snapshots.pop(index, None)
            if not chain.closed:
                # Its volumes' layers that no other snapshot and no running view stacks, from
                # the node that holds each.
                kept = {layer for other in chain.snapshots.values() for layer in other.layers}
                kept.update(layer for held in chain.viewing.values() for layer in held)
                for rank in range(len(snapshot.stacks)):
                    if dropped := sorted(snapshot.layers_of(rank) - kept):
                        await chain.job.control(
                            "drop", *dropped, nodes=(chain.nodes[rank],), timeout=600
                        )
        stem = f"{name}-{index}"
        for path in (cache(self.config) / SNAPSHOTS).glob(f"{stem}.*"):
            path.unlink(missing_ok=True)
        await self.release_shared(shared)
        if found is not None:
            await self._close_if_idle(found[0])


async def _keys(client: GuestClient) -> dict[str, list[str]]:
    """New ssh keys for a Sandbox of several nodes, made on its node 0."""
    code, out, error, _ = await client.exec(
        [guestd.PYTHON["linux"], "-I", "/opt/orbit/guestd/nodes.py", "keys"]
    )
    if code:
        raise ProviderStartError(f"could not make the Sandbox's ssh keys: {error.strip()}")
    return dict(json.loads(out))


def _group_variables(readies: Sequence[StepReady]) -> list[dict[str, str]]:
    """What every command on each node of a Sandbox that spans several gets, below its own
    variables: the nodes, its own number, the nodes' interface for NCCL's and Gloo's sockets
    and, where every node has InfiniBand, NCCL on it alone, so that a fallback to sockets is
    an error rather than a slow job. None for one node."""
    if len(readies) < 2:
        return []
    common = {
        "ORBIT_NODES": ",".join(f"node{rank}" for rank in range(len(readies))),
        "NCCL_SOCKET_IFNAME": worker.GROUP_INTERFACE,
        "GLOO_SOCKET_IFNAME": worker.GROUP_INTERFACE,
    }
    if all(ready.hcas for ready in readies):
        common["NCCL_NET"] = "IB"
    return [common | {"ORBIT_NODE_RANK": str(rank)} for rank in range(len(readies))]


def _seed(archive: str, image: str, layers: dict[str, str]) -> dict[str, object]:
    """What a node of a new allocation loads: an image's archive below the cache, the ID
    Podman gives the image, and volume layers' archives, by layer."""
    return dict(archive=archive, image=image.removeprefix("sha256:"), layers=layers)


def _archive(name: str, index: str, rank: int) -> str:
    """Where node ``rank`` of snapshot ``index`` of job ``name`` is written out, below the
    cache."""
    return f"{SNAPSHOTS}/{name}-{index}.{rank}.tar"


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
