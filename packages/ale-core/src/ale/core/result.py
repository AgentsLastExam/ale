"""The authoritative terminal result for one episode."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ale.core.harness import NativeContinuation
from ale.core.sandbox import ResourceAllocation
from ale.core.verdict import Status

__all__ = [
    "FailureInfo",
    "PhaseTiming",
    "ResultRecord",
    "SandboxOutcome",
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
    outcome: Literal["succeeded", "failed", "timed_out", "cancelled"]

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
    """One validation round: what ``validation/`` gave the agent's state when one of its
    turns ended, which the agent received as its next turn began."""

    model_config = _STRICT

    index: int = Field(ge=1)
    status: Status
    """``completed`` when it scored, else what its failure is, as for an Episode."""
    reward: float | None = None
    failure: FailureInfo | None = None
    feedback: ValidationFeedback | None = None
    """The text the agent was shown below the reward; none when the verifier wrote none."""
    solver_allocation: ResourceAllocation | None = None
    """What the solver the round booted for the agent's next turn was given, its GPUs
    included; the RunLock records the first turn's."""
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
