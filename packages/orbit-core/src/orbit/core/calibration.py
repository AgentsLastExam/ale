"""Persisted evidence from ``orbit calibrate``."""

from __future__ import annotations

import math
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orbit.core.taskspec import Expectation
from orbit.core.verdict import Status

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class CalibrationEngine(BaseModel):
    model_config = _FROZEN

    version: str = Field(min_length=1)
    commit: str = Field(min_length=1)


class CalibrationNotice(BaseModel):
    model_config = _FROZEN

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class CalibrationAttempt(BaseModel):
    """The final episode of one case: how it ended, what it scored, and its RunLock."""

    model_config = _FROZEN

    episode_id: str = Field(min_length=1)
    status: Status
    rewards: dict[str, float] | None
    lock_path: str | None
    lock_digest: str | None = Field(default=None, pattern=_DIGEST)
    result_path: str = Field(min_length=1)

    # --- validation ---

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
                raise ValueError("completed calibration attempts require rewards and RunLock")
        elif self.rewards is not None:
            raise ValueError("failed calibration attempts must not carry rewards")
        return self


class CalibrationCheck(BaseModel):
    """What the stage that validates, ``validation/`` or else ``tests/``, gave a case's final
    state: what the case expects of it, each expected key's value, ``None`` where its
    Verification Result has no number by that name, and how it differs."""

    model_config = _FROZEN

    expect: dict[str, Expectation] = Field(min_length=1)
    observed: dict[str, float | None] = Field(default_factory=dict)
    failures: tuple[CalibrationNotice, ...] = ()
    passed: bool

    # --- validation ---

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not set(self.observed) <= set(self.expect):
            raise ValueError("observed values are recorded only for expected keys")
        if self.passed == bool(self.failures):
            raise ValueError("a check passes exactly when it has no failures")
        return self


class CaseCalibrationObservation(BaseModel):
    """One calibration case: what it expects, the episode that ran it, and what it observed.

    ``observed`` holds each expected key's value from a completed episode, ``None`` where
    the Verification Result has no number by that name. ``validation`` checks the same final
    state with the stage that validates, when the Task has a validation check.
    """

    model_config = _FROZEN

    name: str = Field(min_length=1)
    expect: dict[str, Expectation] = Field(min_length=1)
    attempt: CalibrationAttempt
    observed: dict[str, float | None] = Field(default_factory=dict)
    failures: tuple[CalibrationNotice, ...] = ()
    validation: CalibrationCheck | None = None
    passed: bool

    # --- validation ---

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not set(self.observed) <= set(self.expect):
            raise ValueError("observed values are recorded only for expected keys")
        checked = self.validation is None or self.validation.passed
        if self.passed != (not self.failures and checked):
            raise ValueError(
                "a case passes exactly when it has no failures, and its validation check none"
            )
        return self


class TaskCalibrationObservation(BaseModel):
    model_config = _FROZEN

    name: str = Field(min_length=1)
    spec_hash: str = Field(pattern=_DIGEST)
    cases: tuple[CaseCalibrationObservation, ...] = Field(min_length=1)
    passed: bool

    # --- validation ---

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        names = [case.name for case in self.cases]
        if len(names) != len(set(names)):
            raise ValueError("case names must be unique")
        if self.passed != all(case.passed for case in self.cases):
            raise ValueError("a Task passes exactly when every case passes")
        return self


class CalibrationObservation(BaseModel):
    model_config = _FROZEN

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    engine: CalibrationEngine
    tasks: tuple[TaskCalibrationObservation, ...] = Field(min_length=1)
