import json
from dataclasses import asdict
from pathlib import Path

import jsonschema

from ale_verify import JudgeAttempt, JudgeInvocation, read_result


def test_judge_metadata_round_trips_independently_of_a_result():
    attempt = JudgeAttempt(
        index=1,
        mode="initial",
        started_at="2026-07-31T00:00:00Z",
        finished_at="2026-07-31T00:00:01Z",
        outcome="completed",
        model="judge-model",
        reasoning_effort="medium",
        endpoint_identity="https://example.test/v1",
        prompt_hash="sha256:" + "0" * 64,
        schema_hash="sha256:" + "1" * 64,
    )
    invocation = JudgeInvocation(
        id="judge-1", kind="llm", name="review", status="completed", attempts=(attempt,)
    )
    assert JudgeInvocation.from_dict(json.loads(json.dumps(asdict(invocation)))) == invocation


def test_result_matches_schema_fixture():
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "tests/fixtures/verification-result-v1.schema.json"
        ).read_text()
    )
    result = read_result('{"reward":0.5}', '{"reward":0.5,"why":{"anything":[1,true,null]}}')
    jsonschema.validate(result, schema)
