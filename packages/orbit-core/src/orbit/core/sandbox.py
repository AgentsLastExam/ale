"""Sandbox and provider contracts.

A sandbox is an isolated execution instance; a provider supplies them. Everything that
happens inside a sandbox — commands, files, screenshots, input — goes through the guest
service, so this interface is identical for containers and virtual machines and there
is no provider-specific path for callers to special-case.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ale.core.capability import (
    Arch,
    CapabilityProfile,
    GpuHold,
    SandboxNeeds,
    closest,
    solver_saves,
)
from ale.core.errors import ProviderCapabilityError
from ale.core.taskspec import ImageKind, NetworkMode, NetworkPolicy, OperatingSystem, Resources

__all__ = [
    "SANDBOX_GATEWAY_PORT",
    "DesktopResolution",
    "ExecOutputSink",
    "ExecResult",
    "GpuAllocation",
    "GpuDevice",
    "Identity",
    "ImageKind",
    "ImageRef",
    "PreparedTaskImage",
    "Provider",
    "ResolvedImage",
    "ResourceAllocation",
    "RetainedSandbox",
    "Sandbox",
    "SandboxCheck",
    "SandboxFile",
    "SandboxRequest",
    "SandboxRole",
    "SandboxState",
    "ShownDirectory",
    "snapshot_verifier_needs",
    "verifiers_needs",
]

#: The port at which every Sandbox reaches its Gateway, whatever port the Host serves it on.
#: An agent may keep its endpoint in its native session, and a later episode continues
#: that session in the kept Sandbox or in one booted from its snapshot, with its own Gateway.
SANDBOX_GATEWAY_PORT = 7412


class ImageRef(BaseModel):
    """Engine-owned image reference used only at the Provider boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ImageKind
    reference: str = Field(min_length=1)

    def __str__(self) -> str:
        return self.reference

    @field_validator("reference")
    @classmethod
    def _nonblank_reference(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("image reference must not be blank")
        return value


class PreparedTaskImage(BaseModel):
    """Validated immutable image passed from preparation to provisioning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ImageKind
    source: Literal["solver-local", "verifier-local", "validation-local", "external-ref"]
    input_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    image_source_identity: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_ref: str = Field(min_length=1)
    prepared_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    base_materials: tuple[str, ...] = ()
    resolved_reference: str | None = None
    builder_identity: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    """What built a local VM image on its base, the framework it installed included."""

    # --- validation ---

    @field_validator("runtime_ref", "resolved_reference")
    @classmethod
    def _nonblank_refs(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("image references must not be blank")
        return value

    @model_validator(mode="after")
    def _valid_shape(self) -> Self:
        local = self.source != "external-ref"
        if local and self.image_source_identity is None:
            raise ValueError("local prepared images require image_source_identity")
        if not local and self.resolved_reference is None:
            raise ValueError("external prepared images require resolved_reference")
        if local and self.resolved_reference is not None:
            raise ValueError("local prepared images cannot declare resolved_reference")
        if not local and self.image_source_identity is not None:
            raise ValueError("external prepared images cannot declare image_source_identity")

        if self.kind is ImageKind.VM and local:
            if self.builder_identity is None or not self.base_materials:
                raise ValueError("a local VM image requires builder_identity and base_materials")
        elif self.builder_identity is not None:
            raise ValueError("only a local VM image has a builder_identity")
        return self


class ResourceLease(Protocol):
    def release(self) -> None: ...


class SandboxState(StrEnum):
    CREATED = "created"
    READY = "ready"
    DESTROYED = "destroyed"


class SandboxRole(StrEnum):
    SOLVER = "solver"
    VERIFIER = "verifier"
    SHARED = "shared"


class ExecOutputSink(Protocol):
    async def __call__(self, stream: Literal["stdout", "stderr"], data: bytes) -> None: ...


class ExecResult(BaseModel):
    """Outcome of one command run inside a sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    truncated: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Identity(StrEnum):
    """Who an operation acts as inside a sandbox.

    Naming the two roles rather than a user account keeps the decision at the level it is
    actually made: some work is the framework's and some is the agent's, and which unix
    user each maps to is the image's business.

    The distinction exists so an agent cannot alter the conditions it is being measured
    under. It is not a hardening pass — an agent with root can change the network policy,
    the clock and the guest service, and a result obtained that way says nothing
    reproducible.
    """

    FRAMEWORK = "framework"
    """The engine's own work: installing the guest service, running a task's stages,
    collecting artifacts. Must succeed regardless of what the agent did to its workspace."""

    AGENT = "agent"
    """The thing being measured — and the oracle that stands in for it, so that
    calibration meets the same limits a real run will."""


class DesktopResolution(BaseModel):
    """The size of a Sandbox's desktop in logical pixels, written ``WIDTHxHEIGHT``.

    Logical pixels are what windows are laid out in: points on macOS, whose Retina desktop
    draws each one with two pixels each way, and plain pixels elsewhere. The same value
    therefore fits the same amount of a program's window on every operating system. The
    bounds are what every Provider starts: macOS draws no desktop narrower than 1024 points,
    and Windows' firmware none larger than 3200x1800.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    width: int = Field(default=1024, ge=1024, le=3200, strict=True)
    height: int = Field(default=768, ge=480, le=1800, strict=True)

    def __str__(self) -> str:
        return f"{self.width}x{self.height}"

    # --- validation ---

    @model_validator(mode="before")
    @classmethod
    def _from_text(cls, value: object) -> object:
        if isinstance(value, (dict, DesktopResolution)):
            return value
        width, separator, height = str(value).strip().partition("x")
        if not (separator and width.isdigit() and height.isdigit()):
            raise ValueError(f"{value!r} is not WIDTHxHEIGHT, such as 1024x768")
        return {"width": int(width), "height": int(height)}


class ShownDirectory(BaseModel):
    """A Host directory the agent reads in its Sandbox, at ``target``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    """Its absolute path on the Host."""
    target: str = Field(min_length=1)


class SandboxRequest(BaseModel):
    """What the engine asks a provider for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: str
    os: OperatingSystem = OperatingSystem.LINUX
    role: SandboxRole = SandboxRole.SOLVER
    retention: Literal["destroy", "keep", "snapshot"] = "destroy"
    prepared_image: PreparedTaskImage
    snapshot: str | None = None
    """A snapshot handle to boot from instead of the prepared image's own disk."""
    snapshot_verifier: Resources | None = None
    """The resources of the snapshot verifier that boots from this solver's snapshot: the
    solver is saved as a snapshot whatever its retention, on a leaf that serves both."""
    round_verifier: Resources | None = None
    """Under shared verification, the resources of the verifier each validation round boots
    from this solver's snapshot (:func:`~ale.core.capability.round_verifier_resources`), on
    the leaf that saved it, which serves both."""
    verifier_network_mode: NetworkMode = NetworkMode.OPEN
    """The network mode of ``snapshot_verifier``, the Task's verifier network's
    (:attr:`~ale.core.taskspec.VerifySpec.network`)."""
    round_verifier_network_mode: NetworkMode = NetworkMode.OPEN
    """The network mode of ``round_verifier``, that of the verifier of validation rounds
    (:attr:`~ale.core.taskspec.TaskSpec.validation_spec`)."""
    planned_saves: int = Field(default=0, ge=0)
    """How many validations still to come save this solver, each the one booted
    from the previous save, whatever its retention."""
    duration_sec: float | None = Field(default=None, gt=0)
    """How long the episode may need the Sandbox, where it bounds that, as a verifier's time
    limit does. A Provider that holds a Sandbox only until a deadline starts it where that
    leaves it this long."""
    volumes: tuple[str, ...] = ()
    """The Task's volume candidates (:attr:`~ale.core.taskspec.TaskSpec.volume_candidates`).
    A leaf that mounts volumes makes each that is a directory in the image a volume of its
    own from the start, which holds what the image has there; booting from ``snapshot``, or
    with ``volumes_from``, a view of what the snapshot holds there instead, whose writes stay
    its own. :attr:`Sandbox.volumes` says which it mounted."""
    volumes_from: str | None = None
    """A snapshot whose volumes a Sandbox of another image mounts views of: a separate
    verifier of a solver that mounted some."""
    shown: ShownDirectory | None = Field(default=None, exclude_if=lambda value: value is None)
    """The stage a Task shows its agent: a leaf on the Host that mounts Host directories
    mounts it read-only at its target from the start, sparing a copy of its assets, and says
    so in :attr:`Sandbox.shown`; the episode copies it there otherwise."""
    separate_verifier: Resources | None = None
    """The resources of the separate verifier that may mount views of this solver's
    volumes, for which a leaf that would host both reserves room; nothing requires it."""
    resources: Resources
    network: NetworkPolicy
    gateway_url: str | None = Field(
        default=None, description="Reachable from inside; the only permitted egress"
    )
    env: dict[str, str] = Field(
        default_factory=dict, description="Non-secret variables; credentials stay host side"
    )
    proxy_url: str = ""
    """The Host's egress proxy, for a proxied policy (``allowlist`` or ``blocklist``); empty
    for any other."""
    proxy_token: str = Field(default="", repr=False)
    """The token of the episode's egress session, which the Sandbox's relay presents to the
    proxy: it reaches the hosts of the phase that runs, and nothing between phases."""

    sudo: bool = False
    """Whether the agent user may elevate. Declared by the task, recorded in provenance."""
    user: str = Field(default="user", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    """The agent account, named by the Task's ``[agent].user``."""
    desktop_resolution: DesktopResolution = DesktopResolution()
    """The size of the desktop, when the image has one; a Run setting, never the image's."""

    @property
    def image_kind(self) -> ImageKind:
        return self.prepared_image.kind

    @property
    def saves(self) -> int:
        """How many snapshots in a row its episode may save, as
        :func:`~ale.core.capability.solver_saves` counts them."""
        return solver_saves(
            self.planned_saves,
            snapshot_verified=self.snapshot_verifier is not None,
            kept_as_snapshot=self.retention == "snapshot",
        )

    @property
    def needs(self) -> SandboxNeeds:
        return SandboxNeeds(
            kind=self.image_kind,
            os=self.os,
            arch=Arch.of(self.os),
            network_mode=self.network.mode,
            # The elevation it gets, which an image builder's request grants beyond its
            # size.
            resources=self.resources.model_copy(update={"sudo": self.sudo}),
            snapshot=self.saves > 0 or self.snapshot is not None or self.volumes_from is not None,
            saves=self.saves,
            duration_sec=self.duration_sec,
            verifier=verifiers_needs(
                self.image_kind,
                self.os,
                (
                    (self.snapshot_verifier, self.verifier_network_mode),
                    (self.round_verifier, self.round_verifier_network_mode),
                ),
            ),
        )

    # --- validation ---

    @model_validator(mode="after")
    def _volumes_from_a_snapshot(self) -> Self:
        if self.volumes_from is not None and self.snapshot is not None:
            raise ValueError("a Sandbox boots from a snapshot or mounts views of one's volumes")
        return self


def verifiers_needs(
    kind: ImageKind,
    operating_system: OperatingSystem,
    verifiers: Sequence[tuple[Resources | None, NetworkMode]],
) -> SandboxNeeds | None:
    """What the verifiers that boot from a solver's snapshot need, a snapshot verifier's and
    a validation round's, each of the ``verifiers`` given with its network mode, the next
    nested in the one before, so that every check of one profile holds it to all of them."""
    distinct: list[tuple[Resources, NetworkMode]] = []
    for resources, mode in verifiers:
        if resources is not None and (resources, mode) not in distinct:
            distinct.append((resources, mode))
    needs = None
    for resources, mode in reversed(distinct):
        outer = snapshot_verifier_needs(kind, operating_system, resources, mode)
        needs = outer if needs is None else outer.model_copy(update={"verifier": needs})
    return needs


def snapshot_verifier_needs(
    kind: ImageKind,
    operating_system: OperatingSystem,
    resources: Resources,
    network_mode: NetworkMode,
) -> SandboxNeeds:
    """What a snapshot verifier of ``resources`` needs: its solver's image, the verifier's
    ``network_mode``, and a snapshot to boot from."""
    return SandboxNeeds(
        kind=kind,
        os=operating_system,
        arch=Arch.of(operating_system),
        network_mode=network_mode,
        resources=resources,
        snapshot=True,
    )


class ResolvedImage(BaseModel):
    """Immutable image identity observed by the Provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ImageKind
    prepared_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observed_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observed_ref: str = Field(min_length=1)


class GpuDevice(BaseModel):
    """One physical NVIDIA GPU observed inside the ready sandbox, or on the Host that attached
    it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    vendor: Literal["nvidia"] = "nvidia"
    model: str = Field(min_length=1)
    pci_address: str | None = None
    driver_version: str = Field(min_length=1)
    memory_total_mb: int | None = None
    """``memory.total`` in MiB, as the Host's ``nvidia-smi`` reported it when the Provider
    admitted the Sandbox; None where it was not read or not reported."""
    memory_reserved_mb: int | None = None
    """``memory.reserved``, what the driver keeps of it, read with ``memory_total_mb``."""
    memory_free_mb: int | None = None
    """``memory.free`` in MiB, read with ``memory_total_mb``."""


class GpuAllocation(BaseModel):
    """Provider allocation addresses and sandbox observations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_count: int = Field(ge=1)
    provider_addresses: tuple[str, ...]
    lease_keys: tuple[str, ...]
    observed_devices: tuple[GpuDevice, ...]
    runtime_identity: str | None = None
    hold: GpuHold = GpuHold.WHOLE
    """How the Sandbox holds its GPUs: whole, or shared with others under a limit or watched."""
    memory_mb: int | None = Field(default=None, ge=1)
    """The device memory reserved for a Sandbox that shares its GPUs, in MiB on each; None for
    one that holds them whole."""

    # --- validation ---

    @model_validator(mode="after")
    def _memory_only_when_shared(self) -> Self:
        if (self.memory_mb is None) != (self.hold is GpuHold.WHOLE):
            raise ValueError("a GPU allocation reserves memory exactly when it is shared")
        return self


class ResourceAllocation(BaseModel):
    """Effective resources attached to a ready sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpus: int = Field(ge=1)
    memory_mb: int = Field(ge=1)
    storage_mb: int | None = Field(default=None, ge=1)
    gpu: GpuAllocation | None = None
    sudo: bool
    network_mode: NetworkMode
    provider: str = Field(min_length=1)
    """The provider id of the Provider the Run had start it, which its configuration
    selected."""
    leaf: str = Field(min_length=1)
    """The provider id of the profile that served it: the Provider's own for a leaf, the
    Node's leaf under the Fleet, and Daytona's own, which names no machine of its."""
    runtime: str | None = None
    """The runtime that ran a container Sandbox, as Docker reports it: ``runc``,
    ``io.containerd.kata.v2`` for a Kata microVM, or ``runsc``, ``runsc-net-raw`` or
    ``runsc-nested`` under gVisor. None where the Provider has no choice."""
    node: str | None = None
    """The compute node, when known. Fleet records its Node and leaf as ``<node>/<leaf>``."""
    desktop_resolution: DesktopResolution | None = None
    """The desktop's size as the guest reported it when the Provider started and checked the
    desktop; None for a sandbox whose desktop it did not start."""


class RetainedSandbox(BaseModel):
    """Durable operator handle, to a Sandbox or its snapshot, returned only after sanitation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    """The Provider's name, such as ``docker``, which prefixes its handles; not its id."""
    handle: str
    episode_id: str
    roles: tuple[Literal["solver", "verifier"], ...]
    reason: str
    cleanup_command: str
    gpu_devices: tuple[str, ...] = ()


class SandboxFile(BaseModel):
    """One regular guest file observed by :meth:`Sandbox.list_files`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    size: int = Field(ge=0)


class Sandbox(ABC):
    """One isolated execution instance.

    ``agent_user`` and ``agent_home`` are the account :attr:`Identity.AGENT` maps to, as
    the guest reported it once the sandbox was ready, not as the request assumed it.
    """

    def __init__(
        self,
        *,
        sandbox_id: str,
        request: SandboxRequest,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        agent_user: str,
        agent_home: str,
        resource_lease: ResourceLease | None = None,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.request = request
        self.resolved_image = resolved_image
        self.allocation = allocation
        self.agent_user = agent_user
        self.agent_home = agent_home
        self._resource_lease = resource_lease
        self.state = SandboxState.CREATED
        #: Which of its request's volume candidates it mounted as volumes of their own, or,
        #: mounting views of a snapshot's, which it mounted views of.
        self.volumes: tuple[str, ...] = ()
        #: Whether it mounted its request's ``shown`` directory, read-only.
        self.shown = False
        #: The latest time at which the episode must begin to save it, where its Provider
        #: holds it only until a set time, as a scheduler's allocation of a wall time does;
        #: what saving it and writing it where another Sandbox can boot from it takes is left
        #: after it. None where the Provider holds it as long as it is asked to.
        self.deadline: datetime | None = None

    def release_resources(self) -> None:
        """Release Provider allocations after the underlying sandbox has stopped."""
        if self._resource_lease is not None:
            self._resource_lease.release()
            self._resource_lease = None

    @property
    def gateway_url(self) -> str | None:
        return self.request.gateway_url

    @property
    def handle(self) -> str | None:
        """What this Sandbox's Provider names it by in :meth:`Provider.reattach` and
        :meth:`Provider.discard`; None where the Provider cannot reattach to it."""
        return None

    @abstractmethod
    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        identity: Identity = Identity.FRAMEWORK,
        output_sink: ExecOutputSink | None = None,
    ) -> ExecResult: ...

    @abstractmethod
    async def write_file(
        self, path: str, data: bytes, *, identity: Identity = Identity.FRAMEWORK
    ) -> None: ...

    # File reads never follow links: a link the agent made can point at anything the
    # framework identity may read. They proceed while a command is running, so evidence
    # can be collected from a live agent.

    @abstractmethod
    async def read_file(self, path: str, *, offset: int = 0, length: int | None = None) -> bytes:
        """Read bytes of a regular file."""

    @abstractmethod
    async def list_files(self, path: str) -> tuple[SandboxFile, ...]:
        """The regular files at or below an absolute path, sorted; none when it is missing."""

    @abstractmethod
    async def file_digest(self, path: str, *, length: int) -> str:
        """``sha256:<hex>`` of a regular file's first ``length`` bytes, computed in place.

        Lets a caller check that a copy it already holds is still a prefix of the file
        without transferring the file again.
        """

    @abstractmethod
    async def open_egress(self) -> None:
        """Let the sandbox reach the network: for setup and the Harness's first installation,
        which are the Task's trusted preparation and ours, and for a phase whose policy is
        ``open``. Opening an open sandbox leaves it open.

        Holding preparation to the agent's limits bought nothing and cost a great deal, most
        visibly an agent that could not be installed into an image that had not pre-baked it.
        """

    @abstractmethod
    async def close_egress(self) -> None:
        """Seal the sandbox to its request's policy for a phase whose policy is not ``open``,
        the agent's or a verifier's: it reaches the Gateway and, under a proxied policy, the
        egress proxy through the relay with the request's ``proxy_token``. Sealing a sealed
        sandbox keeps it sealed and starts its relay again. Nothing on an ``open`` request.

        Abstract rather than a no-op default: a sandbox that silently stayed open through
        a sealed phase would make every isolation claim in a run's provenance false. A
        backend that cannot change policy at runtime raises here, and one that cannot
        apply a policy at all leaves its mode out of :meth:`Provider.profiles`.
        """

    @abstractmethod
    async def upload_dir(
        self, source: str, target: str, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        """Copy a Host directory into the sandbox with the fixed upload semantics.

        Host modes never travel: directories become 0755 and files 0644 (on Windows, a
        reset ACL), owned by the identity's account, and links arrive as links. Entry
        scripts run through an interpreter, so nothing depends on an executable bit that
        an object store would have dropped anyway. Every Provider streams the tree through
        the guest service, which applies these rules in one place.
        """

    @abstractmethod
    async def destroy(self) -> None:
        """Release every resource. Must be idempotent and safe during cancellation."""

    async def retain(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        raise ProviderCapabilityError(f"{type(self).__name__} does not support retention")

    async def snapshot(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        """Save the disk as a snapshot new Sandboxes boot from, stopping this one.

        Only the disk is kept: processes, the desktop session and memory are not. The
        Sandbox is left for :meth:`destroy`.
        """
        raise ProviderCapabilityError(f"{type(self).__name__} does not save snapshots")

    async def screenshot(self) -> bytes:
        """Capture the guest desktop. Only meaningful on GUI-capable sandboxes."""
        raise ProviderCapabilityError(f"{type(self).__name__} has no desktop to capture")

    async def inject_input(self, actions: Sequence[object]) -> int:
        """Dispatch desktop actions; returns how many were applied."""
        raise ProviderCapabilityError(f"{type(self).__name__} cannot inject input")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.destroy()


#: A check of what only a running Sandbox shows of its Host, which fails loudly.
SandboxCheck = Callable[[Sandbox], Awaitable[None]]


class Provider(ABC):
    """Supplies Sandboxes.

    A leaf starts and drives them directly; a scheduler places them on machines it
    manages, which start them with the leaves there. Both declare what they can start as
    capability profiles, which every check of a request compares through
    :func:`~ale.core.capability.unmet`.
    """

    name: str
    """What its handles, records and GPU locks are named by."""

    @property
    def id(self) -> str:
        """The provider id a Run's configuration and a Fleet's Nodes know it by."""
        return self.name

    @abstractmethod
    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        """Every combination it can start: a scheduler's are those of its leaves."""

    def refusals(self, needs: SandboxNeeds, *, user: str, role: SandboxRole) -> tuple[str, ...]:
        """Why it refuses a Sandbox its profiles would serve, by rules no profile states,
        such as the units it allocates in; ``user`` is the agent account.

        They hold wherever the leaf runs, so a scheduler asks them of the leaves it would
        start the Sandbox with.
        """
        return ()

    def refusals_with_verifier(
        self, needs: SandboxNeeds, *, user: str, role: SandboxRole
    ) -> tuple[str, ...]:
        """Its :meth:`refusals` of a Sandbox and, since the leaf that saves a solver's
        snapshot boots its snapshot verifier too, of that verifier, as the same account."""
        reasons = self.refusals(needs, user=user, role=role)
        if needs.verifier is None:
            return reasons
        return reasons + tuple(
            f"the verifier booted from its snapshot: {why}"
            for why in self.refusals(needs.verifier, user=user, role=SandboxRole.VERIFIER)
        )

    async def unserved(
        self, needs: SandboxNeeds, *, user: str, role: SandboxRole
    ) -> tuple[str, ...]:
        """Why it cannot serve a Sandbox, one reason each; none when it can.

        The reasons of its profile that misses the fewest needs, then its own refusals. A
        Run selects its Providers this way before anything starts, and :meth:`accepts` asks
        it as a leaf creates a Sandbox, so a request refused then is refused up front.
        """
        return closest(await self.profiles(), needs) + self.refusals_with_verifier(
            needs, user=user, role=role
        )

    async def accepts(self, request: SandboxRequest) -> None:
        """Raise unless it can serve ``request``."""
        reasons = await self.unserved(request.needs, user=request.user, role=request.role)
        if reasons:
            raise ProviderCapabilityError(
                f"{self.id} cannot serve this Sandbox: {'; '.join(reasons)}"
            )

    @abstractmethod
    async def preflight(self, needs: Sequence[SandboxNeeds] = ()) -> SandboxCheck | None:
        """Fail loudly if this provider cannot run here (missing daemon, no KVM), or cannot
        run here the Sandboxes whose ``needs`` a Run gives it; with none, any Sandbox its
        profiles declare, as a Node lends it.

        What only a running Sandbox of it shows of the Host, it returns as a check, which
        the Run's preflight runs in one Sandbox it starts there before any episode.
        """

    @abstractmethod
    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage: ...

    @abstractmethod
    async def create(self, request: SandboxRequest) -> Sandbox: ...

    async def list_retained(self) -> list[dict[str, object]]:
        """The sandboxes and snapshots this provider kept after their episodes."""
        return []

    async def destroy_retained(self, handle: str) -> None:
        """Destroy one retained sandbox or snapshot and everything it owns."""
        raise ProviderCapabilityError(f"{self.name} does not retain sandboxes")

    async def attach_retained(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
    ) -> Sandbox:
        """Reconnect to a running retained sandbox without taking ownership of it.

        ``image`` is what the sandbox was recorded running, which the Provider confirms.
        """
        raise ProviderCapabilityError(f"{self.name} does not retain sandboxes")

    async def reattach(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
    ) -> Sandbox:
        """Drive a running Sandbox this Provider created on this machine again, after the
        process that created it is gone, whatever its retention.

        ``handle`` is the Sandbox's :attr:`Sandbox.handle`; ``request``, ``allocation`` and
        ``image`` are what the creating process recorded, and the Provider confirms ``image``.
        The returned Sandbox owns it, as the original did.
        """
        raise ProviderCapabilityError(f"{self.name} cannot reattach to a Sandbox")

    async def discard(self, handle: str) -> None:
        """Destroy a Sandbox this Provider created on this machine by its handle, when no
        process drives it any more; nothing happens if it is already gone."""
        raise ProviderCapabilityError(f"{self.name} cannot discard a Sandbox by its handle")
