"""Persisted evidence from ``ale validate``."""

from __future__ import annotations

import math
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ale.core.verdict import Status

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class ValidationEngine(BaseModel):
    model_config = _FROZEN

    version: str = Field(min_length=1)
    commit: str = Field(min_length=1)


class ValidationNotice(BaseModel):
    model_config = _FROZEN

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ValidationAttempt(BaseModel):
    model_config = _FROZEN

    episode_id: str = Field(min_length=1)
    status: Status
    rewards: dict[str, float] | None
    lock_path: str | None
    lock_digest: str | None = Field(default=None, pattern=_DIGEST)
    result_path: str = Field(min_length=1)

    @field_validator("rewards")
    @classmethod
    def _rewards(cls, rewards: dict[str, float] | None) -> dict[str, float] | None:
        if rewards is not None:
            if not rewards or any(not name.strip() for name in rewards):
                raise ValueError("rewards must have non-empty names")
            if any(not math.isfinite(value) for value in rewards.values()):
                raise ValueError("reward values must be finite")
        return rewards

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.lock_path is None) != (self.lock_digest is None):
            raise ValueError("lock_path and lock_digest must both be present or absent")
        if self.status is Status.COMPLETED:
            if self.rewards is None or self.lock_path is None:
                raise ValueError("completed validation attempts require rewards and RunLock")
        elif self.rewards is not None:
            raise ValueError("failed validation attempts must not carry rewards")
        return self


class TaskValidationObservation(BaseModel):
    model_config = _FROZEN

    name: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    spec_hash: str = Field(pattern=_DIGEST)
    untouched: ValidationAttempt
    oracle: ValidationAttempt
    reward_names: tuple[str, ...] = ()
    warnings: tuple[ValidationNotice, ...] = ()
    failures: tuple[ValidationNotice, ...] = ()
    passed: bool

    @field_validator("reward_names")
    @classmethod
    def _reward_names(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ValueError("reward_names must be non-empty and unique")
        return names


class ValidationObservation(BaseModel):
    model_config = _FROZEN

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    engine: ValidationEngine
    tasks: tuple[TaskValidationObservation, ...] = Field(min_length=1)
