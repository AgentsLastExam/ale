"""Sandbox and provider contracts.

A sandbox is an isolated execution instance; a provider supplies them. Everything that
happens inside a sandbox — commands, files, screenshots, input — goes through the guest
service, so this interface is identical for containers and virtual machines and there
is no provider-specific path for callers to special-case.

Two capabilities are declared but unimplemented on purpose: ``reset`` and ``snapshot``.
Fresh-per-episode is correct for Phase 0, and pretending otherwise would invite callers
to depend on semantics we have not designed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ale.core.errors import ProviderCapabilityError
from ale.core.taskspec import ImageKind, NetworkMode, NetworkPolicy, Resources

__all__ = [
    "Capabilities",
    "ExecOutputSink",
    "ExecResult",
    "GpuAllocation",
    "GpuDevice",
    "GuestTransport",
    "Identity",
    "ImageKind",
    "ImageRef",
    "PreparedTaskImage",
    "Provider",
    "ResolvedImage",
    "ResourceAllocation",
    "RetainedSandbox",
    "Sandbox",
    "SandboxRequest",
    "SandboxRole",
    "SandboxState",
]


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
    source: Literal["solver-local", "verifier-local", "external-ref"]
    input_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    image_source_identity: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_ref: str = Field(min_length=1)
    prepared_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    base_materials: tuple[str, ...] = ()
    resolved_reference: str | None = None
    oci_identity: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    materializer_identity: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

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

        if self.kind is ImageKind.CONTAINER:
            if self.oci_identity is not None or self.materializer_identity is not None:
                raise ValueError("container images cannot declare VM materialization identities")
        elif local:
            if self.oci_identity is None or self.materializer_identity is None:
                raise ValueError("local VM images require OCI and materializer identities")
        elif self.oci_identity is not None or self.materializer_identity is not None:
            raise ValueError("referenced VM images cannot declare local materialization identities")
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


class Capabilities(BaseModel):
    """What a provider can actually deliver.

    Declared up front so a mismatch fails before anything is provisioned, rather than
    halfway through an episode.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    os: str = "linux"
    gui: bool = False
    network_modes: frozenset[NetworkMode] = frozenset({NetworkMode.OPEN})
    max_cpus: int | None = None
    max_memory_mb: int | None = None
    reset: bool = False
    snapshot: bool = False

    def check(self, resources: Resources, network: NetworkPolicy) -> None:
        """Raise :class:`ProviderCapabilityError` if this provider cannot serve a task.

        A desktop is not among the things checked. Whether one exists is a property of the
        image, not of the backend, and it is answered where it is needed — an agent that
        asks for a screenshot in a sandbox without one is told so by the screenshot.
        """
        problems: list[str] = []
        if network.mode not in self.network_modes:
            offered = ", ".join(sorted(self.network_modes))
            problems.append(f"network mode {network.mode} unsupported (offers: {offered})")
        if self.max_cpus is not None and resources.cpus > self.max_cpus:
            problems.append(f"task needs {resources.cpus} cpus, provider caps at {self.max_cpus}")
        if self.max_memory_mb is not None and resources.memory_mb > self.max_memory_mb:
            problems.append(
                f"task needs {resources.memory_mb} MB, provider caps at {self.max_memory_mb} MB"
            )
        if problems:
            raise ProviderCapabilityError("; ".join(problems))


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
    validation meets the same limits a real run will."""


class SandboxRequest(BaseModel):
    """What the engine asks a provider for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: str
    role: SandboxRole = SandboxRole.SOLVER
    retention: Literal["destroy", "keep"] = "destroy"
    prepared_image: PreparedTaskImage
    resources: Resources
    network: NetworkPolicy
    gateway_url: str | None = Field(
        default=None, description="Reachable from inside; the only permitted egress"
    )
    env: dict[str, str] = Field(
        default_factory=dict, description="Non-secret variables; credentials stay host side"
    )
    proxy_url: str = ""
    """Egress proxy for ``allowlist`` mode; empty when the task declared no hosts."""
    proxy_token: str = Field(default="", repr=False)
    """Episode credential used only for the authenticated allowlist proxy."""

    sudo: bool = False
    """Whether the agent user may elevate. Declared by the task, recorded in provenance."""

    @property
    def image_kind(self) -> ImageKind:
        return self.prepared_image.kind


class ResolvedImage(BaseModel):
    """Immutable image identity observed by the Provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ImageKind
    prepared_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observed_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observed_ref: str = Field(min_length=1)


class GpuDevice(BaseModel):
    """One physical NVIDIA GPU observed inside the ready sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    vendor: Literal["nvidia"] = "nvidia"
    model: str = Field(min_length=1)
    pci_address: str | None = None
    driver_version: str = Field(min_length=1)


class GpuAllocation(BaseModel):
    """Provider allocation addresses and sandbox observations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_count: int = Field(ge=1)
    provider_addresses: tuple[str, ...]
    lease_keys: tuple[str, ...]
    observed_devices: tuple[GpuDevice, ...]
    runtime_identity: str | None = None


class ResourceAllocation(BaseModel):
    """Effective resources attached to a ready sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpus: int = Field(ge=1)
    memory_mb: int = Field(ge=128)
    storage_mb: int | None = Field(default=None, ge=256)
    gpu: GpuAllocation | None = None
    sudo: bool
    network_mode: NetworkMode
    provider: str = Field(min_length=1)


class RetainedSandbox(BaseModel):
    """Durable operator handle returned only after successful sanitation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    handle: str
    episode_id: str
    roles: tuple[Literal["solver", "verifier"], ...]
    reason: str
    cleanup_command: str
    gpu_devices: tuple[str, ...] = ()


class GuestTransport(Protocol):
    """How the host reaches the guest service.

    Implemented by a piped ``exec`` session for containers and by a socket for virtual
    machines; callers never see the difference.
    """

    async def request(self, op: str, params: dict[str, object]) -> dict[str, object]: ...

    async def close(self) -> None: ...


class Sandbox(ABC):
    """One isolated execution instance."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        request: SandboxRequest,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        resource_lease: ResourceLease | None = None,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.request = request
        self.resolved_image = resolved_image
        self.allocation = allocation
        self._resource_lease = resource_lease
        self.state = SandboxState.CREATED

    def release_resources(self) -> None:
        """Release Provider allocations after the underlying sandbox has stopped."""
        if self._resource_lease is not None:
            self._resource_lease.release()
            self._resource_lease = None

    @property
    def gateway_url(self) -> str | None:
        return self.request.gateway_url

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
        self, path: PurePosixPath | str, data: bytes, *, identity: Identity = Identity.FRAMEWORK
    ) -> None: ...

    @abstractmethod
    async def read_file(self, path: PurePosixPath | str) -> bytes: ...

    async def open_egress(self) -> None:
        """Let the sandbox reach the network, for the framework's own phases.

        A task's network policy describes what binds **the agent** — that is the thing
        being measured, and the only thing whose reach is a result rather than a detail.
        Setup and verify are the task's own trusted code and the harness's preparation is
        ours; holding all three to the agent's limits bought nothing and cost a great deal,
        most visibly an agent that could not be installed into an image that had not
        pre-baked it.

        Default: nothing. A provider that cannot vary this must say so by overriding
        :meth:`close_egress`, because a sandbox that silently stays open during the agent
        phase would make every isolation claim in a run's provenance false.
        """
        return None

    async def close_egress(self) -> None:
        """Apply the task's declared policy, before the agent starts."""
        return None

    @abstractmethod
    async def upload_dir(
        self, source: str, target: PurePosixPath | str, *, identity: Identity = Identity.FRAMEWORK
    ) -> None: ...

    @abstractmethod
    async def download_dir(self, source: PurePosixPath | str, target: str) -> None: ...

    @abstractmethod
    async def destroy(self) -> None:
        """Release every resource. Must be idempotent and safe during cancellation."""

    async def retain(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        raise ProviderCapabilityError(f"{type(self).__name__} does not support retention")

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


class Provider(ABC):
    """Supplies sandboxes of one kind."""

    name: str

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    async def preflight(self) -> None:
        """Fail loudly if this provider cannot run here (missing daemon, no KVM)."""

    @abstractmethod
    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage: ...

    @abstractmethod
    async def create(self, request: SandboxRequest) -> Sandbox: ...

    def accepts(self, request: SandboxRequest) -> None:
        """Raise unless this provider can serve ``request``."""
        self.capabilities().check(request.resources, request.network)
