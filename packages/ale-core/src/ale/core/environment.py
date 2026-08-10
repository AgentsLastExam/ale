"""Environments: how one task becomes one episode.

This is the extension point a domain reaches for when the standard flow is not enough —
a grading topology with two isolated containers, a staged protocol, a human gate.

Everything an environment needs arrives through :class:`EpisodeContext`. It never builds
its own infrastructure clients, so the guarantees the framework makes (sandboxes are
leased and reaped, model traffic is metered and recorded, artifacts are collected,
budgets are enforced) hold for every domain by construction rather than by review.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from ale.core.blob import BlobSink
from ale.core.harness import EffectiveAgentResources, HarnessSession
from ale.core.lock import LimitTermination, SandboxProvenance
from ale.core.result import PhaseTiming, ResultRecord
from ale.core.sandbox import (
    PreparedTaskImage,
    ResolvedImage,
    ResourceAllocation,
    Sandbox,
    SandboxRequest,
)
from ale.core.task import TaskSourceContext
from ale.core.taskspec import TaskSpec
from ale.core.trajectory import AtifTrajectory
from ale.core.verdict import Verdict

if TYPE_CHECKING:
    from ale.core.task import Task

__all__ = [
    "ArtifactSink",
    "Budget",
    "Environment",
    "EpisodeContext",
    "EventSink",
    "Phase",
    "ResultSink",
    "SandboxLease",
    "TrajectorySink",
]


class Phase(StrEnum):
    """Lifecycle stages, used for timeouts, traces and failure attribution."""

    PROVISION = "provision"
    SETUP = "setup"
    AGENT = "agent"
    VERIFY = "verify"
    TEARDOWN = "teardown"


class SandboxLease(Protocol):
    """Leases sandboxes on behalf of an environment.

    An environment may hold several at once (a grader and a solver, say); all of them
    are reaped when the episode ends, whatever the outcome.
    """

    async def acquire(self, request: SandboxRequest) -> Sandbox: ...

    async def release(self, sandbox: Sandbox) -> None: ...

    def mark_sanitized(
        self, sandbox: Sandbox, *, succeeded: bool, reason: str | None = None
    ) -> None: ...


class ArtifactSink(Protocol):
    """Collects files out of a sandbox into the episode's run directory."""

    enabled: bool

    async def collect(self, sandbox: Sandbox, source: str, name: str) -> Path: ...

    async def collect_file(self, sandbox: Sandbox, source: str, name: str) -> Path: ...

    async def restore(self, sandbox: Sandbox) -> None: ...

    def cleanup(self) -> None: ...

    def path(self, name: str) -> Path: ...


class EventSink(Protocol):
    def append(self, event: BaseModel | dict[str, Any], *, durable: bool = False) -> int: ...


class TrajectorySink(Protocol):
    def write_trajectory(self, trajectory: AtifTrajectory) -> None: ...


class ResultSink(Protocol):
    def write_result(self, result: ResultRecord) -> None: ...


@dataclass
class Budget:
    """Wall-clock and money ceilings for one episode.

    The gateway enforces token and cost limits by refusing calls; this object is the
    environment's read-only view of what is left.
    """

    deadline_sec: float
    max_cost_usd: float | None = None
    spent_usd: float = 0.0
    started_at: float = 0.0

    def remaining_sec(self, now: float) -> float:
        return max(0.0, self.deadline_sec - (now - self.started_at))

    @property
    def remaining_usd(self) -> float | None:
        if self.max_cost_usd is None:
            return None
        return max(0.0, self.max_cost_usd - self.spent_usd)


@dataclass
class EpisodeContext:
    """Everything an environment is allowed to touch."""

    episode_id: str
    spec: TaskSpec
    task_dir: Path
    """Host-side task folder: manifest, verify, oracle, setup scripts, files."""

    run_dir: Path
    """Host-side output directory for this episode."""

    sandboxes: SandboxLease
    artifacts: ArtifactSink
    budget: Budget
    session: HarnessSession
    """Gateway address plus this episode's bearer token — never a provider credential."""
    agent_resources: EffectiveAgentResources = field(default_factory=EffectiveAgentResources)
    task_source: TaskSourceContext = field(default_factory=TaskSourceContext)
    trajectory_id: str = ""
    transport: EventSink | None = None
    execution: EventSink | None = None
    blobs: BlobSink | None = None
    trajectory: TrajectorySink | None = None
    result: ResultSink | None = None
    current_phase: Phase | None = None
    prepared_image: PreparedTaskImage | None = None
    """Validated solver image selected before provisioning."""
    prepared_verifier_image: PreparedTaskImage | None = None
    """Prepared dedicated verifier image; None for shared verification."""

    home: str = ""
    """The agent's home directory, which is also where a run does its work.

    Derived from the account the image declared, not configured: a run that could choose
    its own scratch directory was a second answer to a question the image had already
    answered, and the two could disagree. Everything the agent touches lives under here,
    so nothing has to be handed to it afterwards.
    """
    """Framework scratch inside the sandbox, created before setup runs."""

    phases: list[PhaseTiming] = field(default_factory=list)
    """Filled in as each phase exits; copied into the terminal result."""

    proxy_url: str = ""
    """Egress proxy for allowlist tasks; empty when none was started."""

    sandbox_identity: SandboxProvenance | None = None
    """Observed at provisioning: which account the agent ran as, and whether it could
    elevate. Recorded rather than asserted, like the image digest beside it."""

    resolved_image: ResolvedImage | None = None
    resource_allocation: ResourceAllocation | None = None

    agent_version: str | None = None
    """What the agent turned out to be, once it was installed.

    Filled in during the episode rather than declared before it, for the same reason the
    image digest is: what a run asked for and what it got are different facts, and the one
    worth recording is the second. Read before the install it describes, this said
    "unknown" on every run.
    """
    """Resolved when the sandbox is provisioned — the tag alone proves nothing."""

    seed: int = 0
    extras: dict[str, object] = field(default_factory=dict)
    limit_termination: LimitTermination | None = None


class Environment(ABC):
    """Turns one task into one episode."""

    name: str

    @abstractmethod
    async def run(self, task: Task, ctx: EpisodeContext) -> Verdict:
        """Administer ``task`` and return its verdict.

        Implementations should let framework errors propagate: the engine maps them to
        the status taxonomy. Catch only what you can genuinely handle.
        """

    def required_phases(self) -> Sequence[Phase]:
        """Phases this environment actually uses; drives timeout accounting."""
        return (Phase.PROVISION, Phase.SETUP, Phase.AGENT, Phase.VERIFY, Phase.TEARDOWN)
