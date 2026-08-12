from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale_verify import (
    AggregateResult,
    CriterionResult,
    EvidenceReference,
    JudgeAttempt,
    JudgeInvocation,
    ScoredChoice,
    VerificationRecord,
)

HASH0 = "sha256:" + "0" * 64
HASH1 = "sha256:" + "1" * 64


def test_record_round_trip_and_reward_projection() -> None:
    evidence = EvidenceReference(
        kind="file",
        location="/home/user/output/result.json",
        sha256=HASH0,
        size_bytes=2,
    )
    attempt = JudgeAttempt(
        index=1,
        mode="initial",
        started_at="2026-07-31T00:00:00Z",
        finished_at="2026-07-31T00:00:01Z",
        outcome="completed",
        model="judge-model",
        reasoning_effort="medium",
        endpoint_identity="https://example.test/v1",
        prompt_hash=HASH0,
        rubric_hash=HASH1,
        usage={"input_tokens": 4},
    )
    invocation = JudgeInvocation(
        id="judge-1",
        kind="llm",
        criterion_name="correctness",
        status="completed",
        attempts=(attempt,),
    )
    record = VerificationRecord(
        status="completed",
        criteria=(
            CriterionResult(
                name="correctness",
                source="llm_judge",
                score=1,
                evidence=(evidence,),
                judge_invocation_id="judge-1",
            ),
        ),
        metrics={"files": 1},
        aggregates=(
            AggregateResult(
                name="overall",
                method="mean",
                inputs={"correctness": 1},
                weights={"correctness": 1},
                score=1,
            ),
        ),
        judge_invocations=(invocation,),
    )
    restored = VerificationRecord.from_json(record.to_json())
    assert restored == record
    assert restored.rewards == {"correctness": 1.0, "overall": 1.0}
    assert restored.metrics == {"files": 1.0}


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan"), float("inf")])
def test_scores_are_bounded(score: float) -> None:
    with pytest.raises(ValueError):
        ScoredChoice(score=score, description="choice")


def test_record_rejects_collisions_unknown_judge_links_and_bad_terminal_state() -> None:
    with pytest.raises(ValueError, match="collide"):
        VerificationRecord(
            criteria=(CriterionResult(name="same", source="check", score=1),),
            metrics={"same": 1},
        )
    with pytest.raises(ValueError, match="unknown invocation"):
        VerificationRecord(
            criteria=(
                CriterionResult(
                    name="judged",
                    source="llm_judge",
                    score=1,
                    judge_invocation_id="missing",
                ),
            )
        )
    with pytest.raises(ValueError, match="failed"):
        VerificationRecord(status="failed")


def test_serialized_shape_matches_published_schema_top_level() -> None:
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3] / "docs/specs/schemas/verification-record-v1.json"
        ).read_text()
    )
    assert set(VerificationRecord().to_dict()) == set(schema["required"])
