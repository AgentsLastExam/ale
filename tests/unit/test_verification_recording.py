from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from test_runtime_contracts import make_lock

from ale.core.lock import AleVerifyProvenance, JudgeProvenance
from ale.run.provenance import judge_provenance
from ale_verify import (
    JudgeAttempt,
    JudgeInvocation,
)

pytestmark = pytest.mark.unit

HASH0 = "sha256:" + "0" * 64
HASH1 = "sha256:" + "1" * 64


def invocation() -> JudgeInvocation:
    return JudgeInvocation(
        id="judge-1",
        kind="llm",
        name="correctness",
        status="completed",
        attempts=(
            JudgeAttempt(
                index=1,
                mode="initial",
                started_at="2026-07-31T00:00:00Z",
                finished_at="2026-07-31T00:00:01Z",
                outcome="completed",
                model="judge-model",
                reasoning_effort="medium",
                endpoint_identity="https://example.test/v1",
                prompt_hash=HASH0,
                schema_hash=HASH1,
            ),
        ),
    )


def test_run_lock_separates_verify_runtime_and_direct_judges() -> None:
    judge = JudgeProvenance(
        invocation_id="judge-1",
        kind="llm",
        model="judge-model",
        reasoning_effort="medium",
        endpoint_identity="https://example.test/v1",
        prompt_hash=HASH0,
        schema_hash=HASH1,
        attempts=1,
    )
    lock = make_lock(
        ale_verify=AleVerifyProvenance(version="0.1.0", content_hash=HASH0),
        judges=(judge,),
    )
    dumped = lock.model_dump(mode="json")
    assert dumped["ale_verify"]["version"] == "0.1.0"
    assert "kits" not in dumped
    assert "dialect" not in dumped["judges"][0]
    assert "transport_call_ids" not in dumped["judges"][0]


def test_judge_provenance_is_derived_from_the_collected_record() -> None:
    record = (invocation(),)
    observed = judge_provenance(record)
    assert observed[0].invocation_id == "judge-1"
    assert observed[0].endpoint_identity == "https://example.test/v1"
    # The verifier saw the Gateway; the RunLock names the Judge's configured endpoint.
    real = judge_provenance(record, {"llm": "https://api.provider.test/v1"})
    assert real[0].endpoint_identity == "https://api.provider.test/v1"
    assert observed[0].attempts == 1
    assert "credential" not in json.dumps(observed[0].model_dump(mode="json"))


def test_a_lock_in_another_shape_is_rejected_not_reinterpreted() -> None:
    payload = make_lock().model_dump(mode="json")
    payload.pop("judges", None)
    payload["judge"] = {
        "model": "legacy-model",
        "prompt_hash": HASH0,
    }
    with pytest.raises(ValidationError):
        type(make_lock()).model_validate(payload)
