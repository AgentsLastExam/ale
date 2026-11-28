"""The episode contract: what the episode protocol may touch while it runs one Task.

Everything the protocol needs arrives through :class:`EpisodeContext`. It never builds
its own infrastructure clients, so the guarantees the framework makes (sandboxes are
leased and reaped, model traffic is metered and recorded, artifacts are collected,
budgets are enforced) hold by construction rather than by review.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orbit.core.blob import BlobSink
from orbit.core.config import LoggingPolicy, VerificationConfig
from orbit.core.harness import AgentRun, EffectiveAgentResources, HarnessSession, NativeContinuation
from orbit.core.lock import LimitTermination, OrbitVerifyProvenance, ResumedFrom, SandboxProvenance
from orbit.core.result import PhaseTiming, ResultRecord, SandboxOutcome, Segment, ValidationRound
from orbit.core.sandbox import (
    PreparedTaskImage,
    ResolvedImage,
    ResourceAllocation,
    RetainedSandbox,
    Sandbox,
    SandboxRequest,
)
from orbit.core.task import ResolvedVariables, Stage, TaskSourceContext
from orbit.core.taskspec import NetworkPolicy, TaskSpec
from orbit.core.trajectory import AtifTrajectory
from orbit.core.verdict import Rewards

__all__ = [
    "AgentTime",
    "ArtifactSink",
    "Budget",
    "EpisodeContext",
    "EpisodeContinuation",
    "EventSink",
    "GatewayAccounting",
    "ModelUsage",
    "Phase",
    "ResultSink",
    "SandboxLease",
    "TrajectorySink",
    "TurnCursor",
]

_STRICT = ConfigDict(frozen=True, extra="forbid")


class Phase(StrEnum):
    """Lifecycle stages, used for timeouts, traces and failure attribution."""

    PROVISION = "provision"
    SETUP = "setup"
    AGENT = "agent"
    VALIDATE = "validate"
    """A validation round's verifier, between two of the agent's turns."""
    VERIFY = "verify"
    TEARDOWN = "teardown"


class SandboxLease(Protocol):
    """Leases sandboxes on behalf of the episode protocol.

    An episode may lease several (a solver and a separate verifier, say); all of them
    are reaped when the episode ends, whatever the outcome. A solver saved as the snapshot
    its verifier boots from is held as that snapshot, which is reaped after every Sandbox
    booted from it.
    """

    async def acquire(self, request: SandboxRequest, *, validation: bool = False) -> Sandbox:
        """Lease a new Sandbox; one booted from a snapshot of this lease's comes from the
        Provider that saved it. A ``validation`` round's verifier is destroyed whatever the
        Run's retention."""
        ...

    async def adopt(
        self, request: SandboxRequest, attach: Callable[[SandboxRequest], Awaitable[Sandbox]]
    ) -> Sandbox:
        """Lease a retained Sandbox that ``attach`` reconnects for ``request``."""
        ...

    async def release(self, sandbox: Sandbox) -> None: ...

    async def release_snapshot(self, handle: str) -> None:
        """Release the snapshot ``handle`` a solver was saved as for a validation round or a
        move, which goes once no Sandbox booted from it runs."""
        ...

    def hand_off(self, handle: str) -> tuple[str, RetainedSandbox]:
        """Give the snapshot ``handle``, saved for a validation round or a move, to the
        episode's next process, which boots from it: this lease neither destroys nor records
        it. Return the id of the Provider that holds it, and the snapshot."""
        ...

    async def snapshot(self, sandbox: Sandbox, *, validation: bool = False) -> str:
        """Save a sanitized Sandbox's disk as a snapshot a verifier boots from, which stops
        and destroys the Sandbox, and return the snapshot's handle. Releasing the Sandbox
        then destroys the snapshot once no Sandbox booted from it runs, unless its retention
        keeps it; a ``validation`` round's, which the next turn's solver boots from too, it
        never keeps."""
        ...

    def mark_sanitized(
        self, sandbox: Sandbox, *, succeeded: bool, reason: str | None = None
    ) -> None: ...


class ArtifactSink(Protocol):
    """Collects files out of a sandbox into the episode's run directory."""

    enabled: bool
    retained: bool
    """Whether the Run keeps the copy, as ``artifacts.collect = "host"`` asks."""

    async def collect(
        self,
        sandbox: Sandbox,
        source: str,
        name: str,
        exclude: tuple[str, ...] = (),
        destination: str | None = None,
        *,
        restored: bool = True,
    ) -> None:
        """Copy ``source`` out, kept on the Host at ``destination`` when given; one not
        ``restored`` reaches no separate verifier, which mounts a view of it instead."""
        ...

    async def restore(self, sandbox: Sandbox) -> None: ...

    def cleanup(self) -> None: ...

    def path(self, name: str) -> Path: ...

    def for_validation(self, directory: Path) -> ArtifactSink:
        """A sink of its own, below ``directory``, for one validation round's separate
        verifier, whose copy goes once it is cleaned up."""
        ...


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


class ModelUsage(BaseModel):
    """A Gateway session's model calls the provider answered, and the tokens of all its
    calls."""

    model_config = _STRICT

    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class GatewayAccounting(BaseModel):
    """What the limits of the agent's Gateway session count, across the episode: its
    ``usage``, and the first limit that refused one of its calls."""

    model_config = _STRICT

    usage: ModelUsage = ModelUsage()
    refusal: LimitTermination | None = None


class AgentTime(BaseModel):
    """The agent's time: ``total`` across all its turns, ``None`` where unlimited, of which
    ``used`` is spent."""

    model_config = _STRICT

    total: float | None = Field(ge=0)
    used: float = Field(default=0.0, ge=0)


class TurnCursor(BaseModel):
    """Where the episode goes on in the solver booted from a hand-off's snapshot: in the turn
    a move cut, or kept from starting (``moved``); in the turn after validation round
    ``round``, which begins there (``round``); at validation round ``round``, whose verifier
    the earlier process would not have lasted (``validating``); or at the episode's
    verification, for the same reason (``verifying``). A turn's next run receives ``prompt``,
    or the instruction where none, in the native ``continuation``, or launches where none."""

    model_config = _STRICT

    began: Literal["moved", "round", "validating", "verifying"]
    prompt: str | None = None
    continuation: NativeContinuation | None = None
    ran: bool = False
    """Whether the run the move cut had begun, so that ``prompt`` says it was moved."""
    round: int | None = Field(default=None, ge=1)
    """The validation round whose result ``prompt`` gives."""
    segment: int | None = Field(default=None, ge=1)
    """The segment whose move ``prompt`` marks in the trajectory."""

    # --- validation ---

    @model_validator(mode="after")
    def _one_way_on(self) -> Self:
        if self.began == "round" and (
            self.round is None or self.ran or self.prompt is None or self.continuation is None
        ):
            raise ValueError("the turn after a round begins with its prompt, in its session")
        if self.ran and (self.continuation is None or self.segment is None):
            raise ValueError("a run a move cut goes on in its session, in a segment of its own")
        if self.began in ("validating", "verifying") and (
            self.prompt is not None
            or self.continuation is not None
            or self.ran
            or self.segment is not None
            or (self.round is None) != (self.began == "verifying")
        ):
            raise ValueError(
                "verification goes on without a turn: a validation round by its number alone"
            )
        return self


class EpisodeContinuation(BaseModel):
    """What a later process needs to continue an episode whose Provider could hold no solver
    past the process that ran it, as a cluster's job holds none past its wall time: the
    snapshot the agent's work goes on from, where it goes on, and the episode's records so
    far, which the final ``result.json``, ``lock.json`` and trajectory describe with the rest.

    It holds the earlier processes' episode tokens in ``redacted``, so it is kept where only
    the episode's next process reads it, and never in a record of the episode.
    """

    model_config = _STRICT

    schema_version: Literal[1] = 1
    episode_id: str = Field(min_length=1)
    trajectory_id: str = Field(min_length=1)
    started_at: datetime
    """When the episode's first process began it, which its result's span starts from."""

    snapshot: RetainedSandbox
    """The snapshot the solver goes on from, which ``provider`` holds."""
    provider: str = Field(min_length=1)
    """The id of the Provider that holds ``snapshot``."""
    image: PreparedTaskImage | None = None
    """The image the solver's requests name, the snapshot's booted solver's too."""
    planned_saves: int = Field(ge=0)
    """How many saves in a row the solver booted from ``snapshot`` plans, as the earlier
    process would have asked for it."""
    turn: TurnCursor
    agent_time: AgentTime
    session: NativeContinuation | None = None
    """The native session the agent's work goes on in, once one was reported."""

    agent_started: bool = False
    agent_version: str | None = None
    agent_run: AgentRun | None = None
    """The latest run that returned, which ``result.json``'s continuation names."""
    failure_phase: Phase | None = None
    prepared_image: PreparedTaskImage | None = None
    resolved_image: ResolvedImage | None = None
    resource_allocation: ResourceAllocation | None = None
    sandbox_identity: SandboxProvenance | None = None
    home: str = ""
    resumed_from: ResumedFrom | None = None
    """The earlier episode this one continues, where it began with ``--continue-from``."""
    prepared_validation_image: PreparedTaskImage | None = None
    validation_resolved_image: ResolvedImage | None = None
    validation_resource_allocation: ResourceAllocation | None = None
    orbit_verify_provenance: OrbitVerifyProvenance | None = None

    phases: tuple[PhaseTiming, ...] = ()
    segments: tuple[Segment, ...] = ()
    validation_rounds: tuple[ValidationRound, ...] = ()
    validation_prompts: dict[int, str] = Field(default_factory=dict)
    move_prompts: dict[int, str] = Field(default_factory=dict)
    validation_judge_invocations: tuple[dict[str, Any], ...] = ()
    """What the Judges of the rounds so far recorded, as ``orbit_verify`` writes each
    invocation."""
    harness_execution_counter: int = Field(default=0, ge=0)
    sandbox_outcomes: tuple[SandboxOutcome, ...] = ()
    """What became of the Sandboxes and snapshots the earlier processes released."""
    gateway: GatewayAccounting | None = None
    """The agent's Gateway session's accounting so far, which its limits go on counting."""
    redacted: tuple[str, ...] = Field(default=(), repr=False)
    """Secret: the earlier processes' episode tokens, which the episode's records keep out
    still, as they keep out the current ones; never a model provider's key."""


@dataclass
class EpisodeContext:
    """Everything the episode protocol is allowed to touch."""

    episode_id: str
    spec: TaskSpec
    task_dir: Path
    """Host-side task folder: configuration, environment, tests and calibration cases."""
    validation_stage: Stage
    """The stage that validates: ``validation/``, or ``tests/`` for a Task without one."""

    run_dir: Path
    """Host-side output directory for this episode."""

    sandboxes: SandboxLease
    artifacts: ArtifactSink
    budget: Budget
    session: HarnessSession
    """Gateway address plus this episode's bearer token — never a provider credential."""
    agent_resources: EffectiveAgentResources = field(default_factory=EffectiveAgentResources)
    task_source: TaskSourceContext = field(default_factory=TaskSourceContext)
    variables: ResolvedVariables = field(default_factory=ResolvedVariables)
    """The Task's variables as its commands receive them."""
    trajectory_id: str = ""
    transport: EventSink | None = None
    execution: EventSink | None = None
    blobs: BlobSink | None = None
    trajectory: TrajectorySink | None = None
    result: ResultSink | None = None
    current_phase: Phase | None = None
    limit_refusal: Callable[[], LimitTermination | None] | None = None
    """Reports the Gateway limit that refused one of this episode's model calls, if any."""
    model_provider_failure: Callable[[], str | None] | None = None
    """Describes how the model provider is failing the agent's calls, as the Gateway saw: from
    a call it failed until it answers one; ``None`` while it answers them."""
    open_agent_session: Callable[[bool], None] | None = None
    """Opens (``True``) or suspends (``False``) the agent's Gateway session. It is open from
    its creation through the agent's first turn, then only while a turn runs, and a policy
    Harness's while it rolls out: nothing run between the agent's turns or after its last,
    ``validation/`` and ``tests/`` included, reaches the model with the agent's token."""
    open_judge_sessions: Callable[[bool], None] | None = None
    """Opens or suspends the Judges' Gateway sessions, which are open only while a verifier
    runs, a validation round's, calibration's validation check or ``tests/``: a Judge's token
    that reaches the agent is refused in its later turns."""
    prepared_image: PreparedTaskImage | None = None
    """Validated solver image selected before provisioning."""
    prepared_verifier_image: PreparedTaskImage | None = None
    """Prepared dedicated verifier image; None for shared verification."""
    shown_stage: Path | None = None
    """The folder on the Host of the stage that validates, where the Task shows it to its
    agent (``[verifier.validation].visibility``)."""
    prepared_validation_image: PreparedTaskImage | None = None
    """The image of the verifier of validation rounds in a Sandbox of its own; None where
    they validate shared or not at all."""
    agent_stop: Exception | None = None
    """What ended the agent's run early. Its work is verified first, as in Harbor; this then
    decides the Episode's status."""
    stopped_rewards: Rewards | None = None
    """The score of an agent that ended early, carried past ``agent_stop``."""

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
    """Egress proxy for ``allowlist`` and ``blocklist`` Tasks; empty when none was started."""
    egress_token: str = ""
    """The token of this episode's egress session, which a Sandbox's relay presents to the
    egress proxy; empty when the episode has none."""
    open_egress_session: Callable[[NetworkPolicy], None] | None = None
    """Opens the egress session with a phase's proxied policy, the agent's while its turn
    runs or the verifier's while ``validation/`` or ``tests/`` runs, or suspends it, given
    one that is not proxied: the proxy refuses it between phases, so nothing the agent left
    running reaches a host outside the phase that declared it."""

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
    validation_judge_invocations: tuple[object, ...] = ()
    """What the Judges of every validation round recorded, in order."""
    validation_rounds: list[ValidationRound] = field(default_factory=list)
    """Each validation round so far; copied into the terminal result."""
    validation_prompts: dict[int, str] = field(default_factory=dict)
    """The prompt that gave the agent each round's result, by round, which marks where the
    round falls in its trajectory."""
    segments: list[Segment] = field(default_factory=list)
    """Each solver Sandbox the agent worked in so far; copied into the terminal result."""
    move_prompts: dict[int, str] = field(default_factory=dict)
    """The prompt that resumed the agent's turn a move cut, by the segment it resumed in,
    which marks where the move falls in its trajectory."""
    orbit_verify_provenance: OrbitVerifyProvenance | None = None
    verifier_resolved_image: ResolvedImage | None = None
    verifier_resource_allocation: ResourceAllocation | None = None
    validation_resolved_image: ResolvedImage | None = None
    """The image of the first validation round's verifier of its own."""
    validation_resource_allocation: ResourceAllocation | None = None

    agent_started: bool = False
    agent_run: AgentRun | None = None
    trajectory_written: bool = False
    solver_evidence_captured: bool = False
    harness_execution_counter: int = 0
    sandbox_outcomes: tuple[SandboxOutcome, ...] = ()
    limit_termination: LimitTermination | None = None
    continued: EpisodeContinuation | None = None
    """The record of the episode's earlier process, whose agent's work this process continues
    from the snapshot it handed off."""
    earlier_secrets: tuple[str, ...] = ()
    """The episode tokens of its earlier processes, which its records keep out too."""

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every value this episode keeps out of what it records."""
        egress = (self.egress_token,) if self.egress_token else ()
        return (
            self.session.token,
            *egress,
            *self.verification_secrets,
            *self.variables.secrets,
            *self.earlier_secrets,
        )
