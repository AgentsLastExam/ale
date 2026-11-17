from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from ale.core.calibration import (
    CalibrationAttempt,
    CalibrationEngine,
    CalibrationNotice,
    CalibrationObservation,
    CaseCalibrationObservation,
    TaskCalibrationObservation,
)

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "0" * 64


def completed(name: str, reward: float = 1.0) -> CalibrationAttempt:
    return CalibrationAttempt(
        episode_id=name,
        status="completed",
        rewards={"reward": reward},
        lock_path=f"{name}/run.lock.json",
        lock_digest=DIGEST,
        result_path=f"{name}/result.json",
    )


def test_attempt_consistency_and_finite_rewards() -> None:
    completed("solution")
    CalibrationAttempt(
        episode_id="failed",
        status="env_error",
        rewards=None,
        lock_path=None,
        lock_digest=None,
        result_path="failed/result.json",
    )
    with pytest.raises(ValidationError):
        CalibrationAttempt(
            episode_id="bad",
            status="completed",
            rewards={"reward": float("nan")},
            lock_path="bad/run.lock.json",
            lock_digest=DIGEST,
            result_path="bad/result.json",
        )
    with pytest.raises(ValidationError):
        CalibrationAttempt(
            episode_id="bad",
            status="completed",
            rewards={"reward": 1.0},
            lock_path=None,
            lock_digest=None,
            result_path="bad/result.json",
        )


def test_a_case_passes_exactly_without_failures_and_a_task_when_every_case_does() -> None:
    notice = CalibrationNotice(code="unexpected_value", message="reward is 0.3")
    with pytest.raises(ValidationError, match="exactly when it has no failures"):
        CaseCalibrationObservation(
            name="partial",
            expect={"reward": (0.4, 0.6)},
            attempt=completed("partial", 0.3),
            observed={"reward": 0.3},
            failures=(notice,),
            passed=True,
        )
    with pytest.raises(ValidationError, match="only for expected keys"):
        CaseCalibrationObservation(
            name="partial",
            expect={"reward": (0.4, 0.6)},
            attempt=completed("partial", 0.5),
            observed={"reward": 0.5, "tflops": 640.0},
            passed=True,
        )
    failed = CaseCalibrationObservation(
        name="partial",
        expect={"reward": (0.4, 0.6)},
        attempt=completed("partial", 0.3),
        observed={"reward": 0.3},
        failures=(notice,),
        passed=False,
    )
    with pytest.raises(ValidationError, match="every case passes"):
        TaskCalibrationObservation(name="t", spec_hash=DIGEST, cases=(failed,), passed=True)


def test_observation_matches_schema_fixture() -> None:
    observation = CalibrationObservation(
        run_id="calibrate-1",
        engine=CalibrationEngine(version="0.1.0", commit="deadbeef"),
        tasks=(
            TaskCalibrationObservation(
                name="demo-task",
                spec_hash=DIGEST,
                cases=(
                    CaseCalibrationObservation(
                        name="start-state",
                        expect={"reward": 0},
                        attempt=completed("start-state", 0.0),
                        observed={"reward": 0.0},
                        passed=True,
                    ),
                    CaseCalibrationObservation(
                        name="solution",
                        expect={"reward": [0.0, 0.05], "tflops": [600, 680]},
                        attempt=completed("solution", 0.0),
                        observed={"reward": 0.0, "tflops": None},
                        failures=(
                            CalibrationNotice(
                                code="missing_value",
                                message="tflops in the Verification Result is missing",
                            ),
                        ),
                        passed=False,
                    ),
                ),
                passed=False,
            ),
        ),
    )
    schema_path = (
        Path(__file__).resolve().parents[1] / "fixtures/calibration-observation-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    dumped = observation.model_dump(mode="json")
    jsonschema.validate(dumped, schema)
    assert dumped["tasks"][0]["cases"][1]["expect"] == {
        "reward": [0.0, 0.05],
        "tflops": [600.0, 680.0],
    }
    assert CalibrationObservation.model_validate(dumped) == observation
    with pytest.raises(ValidationError):
        CalibrationObservation.model_validate(dumped | {"unexpected": True})
