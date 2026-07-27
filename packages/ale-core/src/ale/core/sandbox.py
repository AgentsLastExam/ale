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
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field

from ale.core.errors import ProviderCapabilityError
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources

__all__ = [
    "Capabilities",
    "ExecResult",
    "GuestTransport",
    "Identity",
    "Provider",
    "Sandbox",
    "SandboxRequest",
    "SandboxState",
]


class SandboxState(StrEnum):
    CREATED = "created"
    READY = "ready"
    DESTROYED = "destroyed"


class ExecResult(BaseModel):
    """Outcome of one command run inside a sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Capabilities(BaseModel):
    """What a provider can actually deliver.

    Declared up front so a mismatch fails before anything is provisioned, rather than
    halfway through an episode.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    os: str = "linux"
    gui: bool = False
    gpus: int = 0
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
        if resources.gpus > self.gpus:
            problems.append(f"task needs {resources.gpus} gpu(s), provider offers {self.gpus}")
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
    image_ref: str
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

    sudo: bool = False
    """Whether the agent user may elevate. Declared by the task, recorded in provenance."""


class GuestTransport(Protocol):
    """How the host reaches the guest service.

    Implemented by a piped ``exec`` session for containers and by a socket for virtual
    machines; callers never see the difference.
    """

    async def request(self, op: str, params: dict[str, object]) -> dict[str, object]: ...

    async def close(self) -> None: ...


class Sandbox(ABC):
    """One isolated execution instance."""

    def __init__(self, *, sandbox_id: str, request: SandboxRequest) -> None:
        self.sandbox_id = sandbox_id
        self.request = request
        self.state = SandboxState.CREATED

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
    async def create(self, request: SandboxRequest) -> Sandbox: ...

    def accepts(self, request: SandboxRequest) -> None:
        """Raise unless this provider can serve ``request``."""
        self.capabilities().check(request.resources, request.network)
