"""The authoritative terminal result for one episode."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orbit.core.harness import NativeContinuation
from orbit.core.sandbox import ResourceAllocation
from orbit.core.verdict import Status

__all__ = [
    "EpisodeProgress",
    "FailureInfo",
    "PhaseTiming",
    "ResultRecord",
    "SandboxOutcome",
    "Segment",
    "ValidationFeedback",
    "ValidationRound",
]

_STRICT = ConfigDict(frozen=True, extra="forbid")


class FailureInfo(BaseModel):
    model_config = _STRICT

    error_type: str
    message: str
    phase: str | None = None
    execution_id: str | None = None


class PhaseTiming(BaseModel):
    model_config = _STRICT

    phase: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    outcome: Literal["succeeded", "failed", "timed_out", "cancelled", "moved"]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("phase finished_at precedes started_at")
        return self


class SandboxOutcome(BaseModel):
    model_config = _STRICT

    roles: tuple[Literal["solver", "verifier"], ...]
    requested: Literal["destroy", "keep", "snapshot"]
    outcome: Literal["destroyed", "retained", "snapshotted", "retention-failed", "not-destroyed"]
    """``snapshotted``: the Sandbox was destroyed after its disk was saved as ``handle``.
    ``not-destroyed``: it outlived its episode, for ``reason``: a Sandbox, or a validation
    round's snapshot ``handle``, which ``cleanup_command`` removes; no Run continues from
    it."""
    provider: str
    """The Provider's name, such as ``docker``, which prefixes its handles; not its id."""
    handle: str | None = None
    reason: str | None = None
    cleanup_command: str | None = None
    gpu_devices: tuple[str, ...] = ()


class ValidationFeedback(BaseModel):
    """The feedback text a validation round's verifier wrote, kept as a file of the episode."""

    model_config = _STRICT

    path: str = Field(min_length=1)
    """Relative to the episode directory."""
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class ValidationRound(BaseModel):
    """One validation round: what the stage that validates, ``validation/`` or else
    ``tests/``, gave the agent's state when one of its turns ended, which the agent received
    as its next turn began."""

    model_config = _STRICT

    index: int = Field(ge=1)
    status: Status
    """``completed`` when it scored, else what its failure is, as for an Episode."""
    reward: float | None = None
    failure: FailureInfo | None = None
    feedback: ValidationFeedback | None = None
    """The text the agent was shown below the reward; none when the verifier wrote none."""
    turn_ended_by: FailureInfo | None = None
    """What ended the turn before the round where the agent did not: the turn's time, or a
    Gateway limit, each of which ends that turn only."""
    started_at: datetime
    finished_at: datetime

    # --- validation ---

    @model_validator(mode="after")
    def _scored_exactly_when_completed(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        if self.status is Status.COMPLETED:
            if self.reward is None or self.failure is not None:
                raise ValueError("a scored validation round has a reward and no failure")
        elif self.failure is None or self.reward is not None or self.feedback is not None:
            raise ValueError("an unscored validation round has a failure and no score")
        return self


class Segment(BaseModel):
    """One solver Sandbox the agent worked in: the first, or one booted from a snapshot of the
    one before, for the turn after a validation ``round``, or ``moved`` there because the one
    before was to be lost before the agent's time was up."""

    model_config = _STRICT

    index: int = Field(ge=1)
    began: Literal["provisioned", "round", "moved"]
    round: int | None = Field(default=None, ge=1)
    """The validation round whose next turn began in it."""
    sandbox_id: str
    allocation: ResourceAllocation
    """What it was given, its GPUs and, where a scheduler placed it, its node included."""
    booted_from: str | None = None
    """The handle of the snapshot it booted from, which outlives it until the next save."""
    started_at: datetime
    """When the agent's work could begin in it: once it booted, the first once setup ran."""
    ended_at: datetime | None = None
    """When the agent's work in it ended: its save began, or the agent's last turn ended."""
    agent_sec: float = Field(default=0.0, ge=0)
    """How much of the agent's time it spent in it."""

    # --- validation ---

    @model_validator(mode="after")
    def _round_exactly_when_began_after_one(self) -> Self:
        if (self.round is not None) != (self.began == "round"):
            raise ValueError("a segment names its round exactly when it began after one")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at precedes started_at")
        return self


class EpisodeProgress(BaseModel):
    """How far an episode got, rewritten as it goes so that it outlives the process that runs
    the episode: the agent's time and rounds, its native session and the solver Sandboxes it
    worked in, the last of which booted from the snapshot it names."""

    model_config = _STRICT

    schema_version: Literal[1] = 1
    episode_id: str
    phase: Literal["agent", "moving", "validating", "verifying"]
    agent_sec: float = Field(ge=0)
    """The agent's time across all its turns."""
    agent_used_sec: float = Field(ge=0)
    """What of it was spent, the run going on included."""
    rounds: int = Field(ge=0)
    """The validation rounds the agent received."""
    continuation: NativeContinuation | None = None
    """The native session the agent goes on in."""
    segments: tuple[Segment, ...] = ()
    updated_at: datetime


class ResultRecord(BaseModel):
    model_config = _STRICT

    schema_version: Literal[2] = 2
    episode_id: str
    status: Status
    rewards: dict[str, float] | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    failure: FailureInfo | None = None
    started_at: datetime
    finished_at: datetime
    phases: tuple[PhaseTiming, ...] = ()
    sandboxes: tuple[SandboxOutcome, ...] = ()
    continuation: NativeContinuation | None = None
    """The agent's native session as the episode left it, which a later episode may continue
    from the retained solver."""
    validation_rounds: tuple[ValidationRound, ...] = ()
    """Each validation round in order; ``rewards`` are ``tests/``'s alone."""
    segments: tuple[Segment, ...] = ()
    """Each solver Sandbox the agent worked in, in order; none where no agent ran."""

    @field_validator("rewards")
    @classmethod
    def _valid_rewards(cls, rewards: dict[str, float] | None) -> dict[str, float] | None:
        if rewards is None:
            return None
        if not rewards:
            raise ValueError("rewards must be non-empty")
        if any(not key.strip() for key in rewards):
            raise ValueError("reward names must be non-empty")
        if any(not math.isfinite(value) for value in rewards.values()):
            raise ValueError("reward values must be finite")
        return rewards

    @field_validator("metrics")
    @classmethod
    def _finite_metrics(cls, metrics: dict[str, float]) -> dict[str, float]:
        if any(not math.isfinite(value) for value in metrics.values()):
            raise ValueError("metric values must be finite")
        return metrics

    @model_validator(mode="after")
    def _terminal_consistency(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        if self.status is Status.COMPLETED:
            if self.rewards is None or self.failure is not None:
                raise ValueError("completed result requires rewards and no failure")
        elif self.failure is None:
            raise ValueError("failed result requires failure")
        return self
