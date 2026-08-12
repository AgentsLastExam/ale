from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from ale.core.validation import (
    TaskValidationObservation,
    ValidationAttempt,
    ValidationEngine,
    ValidationNotice,
    ValidationObservation,
)

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "0" * 64


def completed(name: str) -> ValidationAttempt:
    return ValidationAttempt(
        episode_id=name,
        status="completed",
        rewards={"reward": 1.0},
        lock_path=f"{name}/run.lock.json",
        lock_digest=DIGEST,
        result_path=f"{name}/result.json",
    )


def test_attempt_consistency_and_finite_rewards() -> None:
    completed("oracle")
    ValidationAttempt(
        episode_id="failed",
        status="env_error",
        rewards=None,
        lock_path=None,
        lock_digest=None,
        result_path="failed/result.json",
    )
    with pytest.raises(ValidationError):
        ValidationAttempt(
            episode_id="bad",
            status="completed",
            rewards={"reward": float("nan")},
            lock_path="bad/run.lock.json",
            lock_digest=DIGEST,
            result_path="bad/result.json",
        )
    with pytest.raises(ValidationError):
        ValidationAttempt(
            episode_id="bad",
            status="completed",
            rewards={"reward": 1.0},
            lock_path=None,
            lock_digest=None,
            result_path="bad/result.json",
        )


def test_observation_matches_checked_in_json_schema() -> None:
    observation = ValidationObservation(
        run_id="validate-1",
        engine=ValidationEngine(version="0.1.0", commit="deadbeef"),
        tasks=(
            TaskValidationObservation(
                name="demo-task",
                variant="base",
                spec_hash=DIGEST,
                untouched=completed("untouched"),
                oracle=completed("oracle"),
                reward_names=("reward",),
                warnings=(ValidationNotice(code="partial_oracle", message="oracle reward is 0.5"),),
                passed=True,
            ),
        ),
    )
    schema_path = (
        Path(__file__).resolve().parents[2] / "docs/specs/schemas/validation-observation-v1.json"
    )
    schema = json.loads(schema_path.read_text())
    jsonschema.validate(observation.model_dump(mode="json"), schema)
    with pytest.raises(ValidationError):
        ValidationObservation.model_validate(
            observation.model_dump(mode="json") | {"unexpected": True}
        )
