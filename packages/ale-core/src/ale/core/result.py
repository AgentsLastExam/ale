"""The authoritative terminal result for one episode."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ale.core.verdict import Status

__all__ = ["FailureInfo", "PhaseTiming", "ResultRecord", "SandboxOutcome"]

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
    requested: Literal["destroy", "keep"]
    outcome: Literal["destroyed", "retained", "retention-failed"]
    provider: str
    handle: str | None = None
    reason: str | None = None
    cleanup_command: str | None = None
    gpu_devices: tuple[str, ...] = ()


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

    @model_validator(mode="before")
    @classmethod
    def _migrate_v1(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("schema_version", 1) == 1:
            return {**value, "schema_version": 2, "sandboxes": value.get("sandboxes", ())}
        return value

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
        elif self.failure is None or self.rewards is not None:
            raise ValueError("failed result requires failure and no rewards")
        return self
