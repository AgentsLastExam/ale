"""Episode results.

A verdict answers two questions and nothing else: how did the episode end, and what
did it score. The status taxonomy is closed and shared by every domain — comparability
depends on `task_error` meaning the same thing everywhere.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["FailureInfo", "Rewards", "Status", "Verdict"]


class Status(StrEnum):
    """How an episode ended. Exactly one applies, and all of them are terminal."""

    COMPLETED = "completed"
    """The agent finished and verification produced a score."""

    AGENT_ERROR = "agent_error"
    """The agent failed in a way attributable to the agent."""

    ENV_ERROR = "env_error"
    """The sandbox side failed: provisioning, guest service, transport."""

    TASK_ERROR = "task_error"
    """The task's own machinery failed. Distinct from the agent scoring zero."""

    TIMEOUT = "timeout"
    """A phase exceeded its declared timeout."""

    BUDGET_EXCEEDED = "budget_exceeded"
    """A turn, token or cost ceiling stopped the episode."""

    CANCELLED = "cancelled"
    """An operator or a signal stopped the episode."""

    REFUSED = "refused"
    """The agent declined the task."""

    @property
    def is_scored(self) -> bool:
        """Whether this status may contribute to score aggregates."""
        return self is Status.COMPLETED


Rewards = dict[str, float]
"""Named scores. By convention the primary key is ``reward`` and lives in [0, 1]."""


class FailureInfo(BaseModel):
    """Why a non-completed episode ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    error_class: str = Field(description="Class name of the raised error, e.g. VerifierOutputError")
    message: str = Field(description="Operator-facing explanation, secrets already redacted")
    phase: str | None = Field(default=None, description="Lifecycle phase that failed")


class Verdict(BaseModel):
    """The result envelope for one episode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Status
    rewards: Rewards | None = Field(
        default=None, description="Present exactly when verification ran to completion"
    )
    primary: str | None = Field(
        default=None, description="Key of the headline score within `rewards`"
    )
    metrics: dict[str, float] = Field(
        default_factory=dict,
        description="Diagnostics (durations, turn counts). Never part of the score.",
    )
    failure: FailureInfo | None = None

    @property
    def primary_reward(self) -> float | None:
        """The headline score, or ``None`` when the episode produced no score."""
        if self.rewards is None:
            return None
        return self.rewards[self.primary or "reward"]

    @classmethod
    def completed(
        cls, rewards: Rewards, *, primary: str = "reward", metrics: dict[str, float] | None = None
    ) -> Verdict:
        """Build a successful verdict."""
        return cls(
            status=Status.COMPLETED,
            rewards=rewards,
            primary=primary,
            metrics=metrics or {},
        )

    @classmethod
    def failed(
        cls,
        status: Status,
        error: BaseException,
        *,
        phase: str | None = None,
        metrics: dict[str, float] | None = None,
    ) -> Verdict:
        """Build a verdict from a raised error."""
        if status is Status.COMPLETED:
            raise ValueError("use Verdict.completed for successful episodes")
        return cls(
            status=status,
            metrics=metrics or {},
            failure=FailureInfo(error_class=type(error).__name__, message=str(error), phase=phase),
        )

    # --- validation ---

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.rewards is not None:
            if not self.rewards:
                raise ValueError("rewards must be omitted rather than empty")
            primary = self.primary or "reward"
            if primary not in self.rewards:
                raise ValueError(f"primary key {primary!r} is not present in rewards")
        elif self.primary is not None:
            raise ValueError("primary was given without rewards")

        if self.status is Status.COMPLETED:
            if self.rewards is None:
                raise ValueError("a completed episode must carry rewards")
            if self.failure is not None:
                raise ValueError("a completed episode must not carry failure info")
        elif self.failure is None:
            raise ValueError(f"status {self.status} requires failure info")
        return self
