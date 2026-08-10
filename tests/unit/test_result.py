"""Terminal result contracts with unaggregated named rewards."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from ale.core.result import FailureInfo, PhaseTiming, ResultRecord, SandboxOutcome
from ale.core.verdict import Status, Verdict

pytestmark = pytest.mark.unit


def completed(**overrides: object) -> ResultRecord:
    values: dict[str, object] = {
        "episode_id": "episode-1",
        "status": Status.COMPLETED,
        "rewards": {"correctness": 1.0, "format": 0.5},
        "started_at": "2026-07-29T12:00:00Z",
        "finished_at": "2026-07-29T12:01:00Z",
    }
    return ResultRecord(**(values | overrides))  # type: ignore[arg-type]


def test_completed_result_keeps_all_rewards_without_primary() -> None:
    result = completed()
    assert result.rewards == {"correctness": 1.0, "format": 0.5}
    assert "primary" not in ResultRecord.model_fields
    verdict = Verdict.completed({"correctness": 1.0})
    assert not hasattr(verdict, "primary_reward")


@pytest.mark.parametrize(
    "rewards",
    [
        {},
        {"": 1.0},
        {"score": math.nan},
        {"score": math.inf},
    ],
)
def test_rewards_are_nonempty_named_and_finite(rewards: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        completed(rewards=rewards)


def test_failure_and_rewards_are_mutually_exclusive() -> None:
    failure = FailureInfo(error_type="TaskError", message="broken", phase="verify")
    with pytest.raises(ValidationError):
        completed(failure=failure)
    with pytest.raises(ValidationError):
        ResultRecord(
            episode_id="episode-1",
            status=Status.TASK_ERROR,
            rewards={"score": 0.0},
            failure=failure,
            started_at="2026-07-29T12:00:00Z",
            finished_at="2026-07-29T12:01:00Z",
        )


def test_phase_timing_is_consistent_with_terminal_time() -> None:
    phase = PhaseTiming(
        phase="setup",
        started_at="2026-07-29T12:00:02Z",
        finished_at="2026-07-29T12:00:03Z",
        duration_ms=1000,
        outcome="succeeded",
    )
    assert completed(phases=[phase]).phases == (phase,)
    with pytest.raises(ValidationError):
        completed(finished_at="2026-07-29T11:59:59Z")


def test_v1_result_migrates_deterministically_to_v2() -> None:
    legacy = completed().model_dump(mode="json", exclude={"schema_version", "sandboxes"})
    migrated = ResultRecord.model_validate(legacy)
    assert migrated.schema_version == 2
    assert migrated.sandboxes == ()
    assert ResultRecord.model_validate_json(migrated.model_dump_json()) == migrated


def test_v2_result_round_trips_retained_sandbox_handle() -> None:
    outcome = SandboxOutcome(
        roles=("solver", "verifier"),
        requested="keep",
        outcome="retained",
        provider="docker",
        handle="ale-demo",
        cleanup_command="ale sandbox destroy ale-demo",
    )
    result = completed(sandboxes=(outcome,))
    assert ResultRecord.model_validate_json(result.model_dump_json()).sandboxes == (outcome,)
