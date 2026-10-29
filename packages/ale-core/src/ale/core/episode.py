"""The episode contract: what the episode protocol may touch while it runs one Task.

Everything the protocol needs arrives through :class:`EpisodeContext`. It never builds
its own infrastructure clients, so the guarantees the framework makes (sandboxes are
leased and reaped, model traffic is metered and recorded, artifacts are collected,
budgets are enforced) hold by construction rather than by review.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from ale.core.blob import BlobSink
from ale.core.config import LoggingPolicy, VerificationConfig
from ale.core.harness import AgentRun, EffectiveAgentResources, HarnessSession
from ale.core.lock import AleVerifyProvenance, LimitTermination, ResumedFrom, SandboxProvenance
from ale.core.result import PhaseTiming, ResultRecord, SandboxOutcome
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
from ale.core.verdict import Rewards

__all__ = [
    "ArtifactSink",
    "Budget",
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
    """Leases sandboxes on behalf of the episode protocol.

    An episode may lease several (a solver and a separate verifier, say); all of them
    are reaped when the episode ends, whatever the outcome.
    """

    async def acquire(self, request: SandboxRequest) -> Sandbox: ...

    async def adopt(
        self, request: SandboxRequest, attach: Callable[[SandboxRequest], Awaitable[Sandbox]]
    ) -> Sandbox:
        """Lease a retained Sandbox that ``attach`` reconnects for ``request``."""
        ...

    async def release(self, sandbox: Sandbox) -> None: ...

    def mark_sanitized(
        self, sandbox: Sandbox, *, succeeded: bool, reason: str | None = None
    ) -> None: ...


class ArtifactSink(Protocol):
    """Collects files out of a sandbox into the episode's run directory."""

    enabled: bool

    async def collect(self, sandbox: Sandbox, source: str, name: str) -> None: ...

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
    """The wall-clock ceiling for one episode.

    The gateway enforces token and model-call limits by refusing calls; this object is
    the protocol's read-only view of the time left.
    """

    deadline_sec: float
    started_at: float = 0.0

    def remaining_sec(self, now: float) -> float:
        return max(0.0, self.deadline_sec - (now - self.started_at))


@dataclass
class EpisodeContext:
    """Everything the episode protocol is allowed to touch."""

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
    limit_refusal: Callable[[], LimitTermination | None] | None = None
    """Reports the Gateway limit that refused one of this episode's model calls, if any."""
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

    phases: list[PhaseTiming] = field(default_factory=list)
    """Filled in as each phase exits; copied into the terminal result."""

    proxy_url: str = ""
    """Egress proxy for allowlist tasks; empty when none was started."""
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    """Effective task plus harness allowlist enforced by the egress proxy."""

    sandbox_identity: SandboxProvenance | None = None
    """Observed at provisioning: which account the agent ran as, and whether it could
    elevate. Recorded rather than asserted, like the image digest beside it."""

    resolved_image: ResolvedImage | None = None
    """Resolved when the Sandbox is provisioned; an image tag alone proves nothing."""
    resource_allocation: ResourceAllocation | None = None
    resumed_from: ResumedFrom | None = None
    """The earlier episode this one continues, set when its retained solver is adopted."""

    agent_version: str | None = None
    """What the agent turned out to be, once it was installed.

    Filled in during the episode rather than declared before it, for the same reason the
    image digest is: what a run asked for and what it got are different facts, and the one
    worth recording is the second. Read before the install it describes, this said
    "unknown" on every run.
    """

    seed: int = 0
    verified_rewards: Rewards | None = None
    """Raw rewards emitted by successful verification."""
    metrics: dict[str, float] = field(default_factory=dict)
    phase_callback: Callable[[Phase], None] | None = None
    failure_phase: Phase | None = None
    phase_timeout_sec: float | None = None
    phase_deadline: float | None = None
    """Monotonic time by which the current phase must finish, when it is bounded."""

    logging_policy: LoggingPolicy = field(default_factory=LoggingPolicy)
    verification_config: VerificationConfig = field(default_factory=VerificationConfig)
    verification_command_env: dict[str, str] = field(default_factory=dict)
    verification_secrets: tuple[str, ...] = ()
    judge_tokens: dict[str, str] = field(default_factory=dict)
    """Gateway tokens of this episode's Judge sessions, by Judge kind."""
    judge_invocations: tuple[object, ...] = ()
    ale_verify_provenance: AleVerifyProvenance | None = None
    verifier_resolved_image: ResolvedImage | None = None
    verifier_resource_allocation: ResourceAllocation | None = None

    agent_started: bool = False
    agent_run: AgentRun | None = None
    trajectory_written: bool = False
    solver_evidence_captured: bool = False
    harness_execution_counter: int = 0
    sandbox_outcomes: tuple[SandboxOutcome, ...] = ()
    limit_termination: LimitTermination | None = None
