from __future__ import annotations

import json

import pytest

from ale_verify import EvidenceError, JudgeError, _io, _llm, judge, read_result, write


@pytest.fixture
def verify_env(monkeypatch, tmp_path):
    result, log, config = (
        tmp_path / name for name in ("result.json", "llm-judge.jsonl", "config.json")
    )
    config.write_text(
        json.dumps(
            {
                "llm": {
                    "model": "gpt-test",
                    "reasoning_effort": "medium",
                    "base_url": "https://example.test/v1/responses",
                    "api_key_env": "JUDGE_KEY",
                }
            }
        )
    )
    for key, value in {
        "ALE_VERDICT_PATH": result,
        "ALE_LLM_JUDGE_LOG_PATH": log,
        "ALE_VERIFY_CONFIG_PATH": config,
        "JUDGE_KEY": "provider-secret",
    }.items():
        monkeypatch.setenv(key, str(value))
    return result, log


def test_plain_nested_result_and_repeated_write(verify_env):
    result, log = verify_env
    value = {
        "overall": 0.74,
        "dimensions": {"correctness": 0.8},
        "reasoning": ["example", None],
        "measurements": {"count": 200},
    }
    write(value)
    assert read_result(result.read_bytes()) == value
    write({"overall": 1})
    assert read_result(result.read_bytes()) == {"overall": 1}
    assert not log.exists()
    assert not list(result.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    ("raw_score", "expected"),
    [
        (0, 0.0),
        (1, 1.0),
        (0.004, 0.0),
        (0.005, 0.01),
        (0.125, 0.13),
        (0.145, 0.15),
        (0.994, 0.99),
        (0.995, 1.0),
        (0.999, 1.0),
    ],
)
def test_result_precision_is_identical_for_sdk_and_direct_json(verify_env, raw_score, expected):
    result, _ = verify_env
    value = {"overall": raw_score, "details": {"raw": raw_score, "count": 123}}
    normalized = {"overall": expected, "details": value["details"]}
    assert read_result(json.dumps(value)) == normalized
    write(value)
    assert json.loads(result.read_text()) == normalized
    assert read_result(result.read_bytes()) == normalized
    assert value["overall"] == raw_score  # Submission does not change task calculations.


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"overall": True},
        {"overall": "1"},
        {"overall": -0.1},
        {"overall": -0.0001},
        {"overall": 1.0001},
        {"overall": 10**500},
        {"overall": float("nan")},
        {"overall": 1, "nested": {"x": float("inf")}},
        {"overall": 1, "x": (1, 2)},
        {"overall": 1, 2: "invalid key"},
    ],
)
def test_bad_result_never_replaces_previous_good_result(verify_env, value):
    result, _ = verify_env
    write({"overall": 0.5})
    with pytest.raises((TypeError, ValueError)):
        write(value)
    assert read_result(result.read_bytes()) == {"overall": 0.5}


def test_direct_json_has_the_same_contract():
    assert read_result('{"overall":1,"extra":{"any":[false,null,20]}}')["overall"] == 1
    with pytest.raises(ValueError, match="duplicate"):
        read_result('{"overall":0,"overall":1}')


def test_judge_returns_data_and_logs_invalid_and_valid_responses(monkeypatch, verify_env):
    result, log = verify_env
    responses = iter(["not json", '{"grade":2}', '{"grade":0.999,"reasoning":{"why":"good"}}'])
    prompts = []

    def request(protocol, config, prompt, secret, evidence):
        prompts.append(prompt)
        return {"output_text": next(responses)}

    monkeypatch.setattr(_llm, "_request", request)
    schema = {
        "type": "object",
        "properties": {"grade": {"type": "number", "minimum": 0, "maximum": 1}},
        "required": ["grade"],
    }
    assessment = judge("llm", prompt="Assess it", output_schema=schema)
    assert assessment == {"grade": 0.999, "reasoning": {"why": "good"}}
    assert not result.exists()
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert len([e for e in events if e["event"] == "response"]) == 3
    errors = [e["error"] for e in events if e["event"] == "error"]
    assert errors[0] in prompts[1]
    assert errors[1] in prompts[2]
    assert "grade" in errors[1]
    assert all("Output JSON Schema" in prompt for prompt in prompts)
    assert events[-1]["response"] == assessment
    write({"overall": assessment["grade"] * 0.5, "review": assessment})
    saved = read_result(result.read_bytes())
    assert saved["overall"] == 0.5
    assert saved["review"] == assessment


def test_optional_schema_and_calls_after_write(monkeypatch, verify_env):
    monkeypatch.setattr(_llm, "_request", lambda *args: {"output_text": '{"anything":[1,true]}'})
    write({"overall": 0})
    assert judge("llm", prompt="Inspect") == {"anything": [1, True]}
    assert judge("llm", prompt="Inspect again") == {"anything": [1, True]}


def test_each_judge_kind_keeps_its_own_events(monkeypatch, verify_env):
    from ale_verify import JudgeInvocation, _agents

    result, llm_log = verify_env
    agent_log = result.parent / "agent-judge.jsonl"
    monkeypatch.setenv("ALE_AGENT_JUDGE_LOG_PATH", str(agent_log))
    config_path = result.parent / "config.json"
    config = json.loads(config_path.read_text())
    config["agent"] = {**config["llm"], "adapter": "codex-cli", "version": "1.2.3"}
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(_llm, "_request", lambda *args: {"output_text": '{"ok":true}'})

    def agent(**kwargs):
        _agents._append_transcript(
            invocation_id=kwargs["invocation_id"],
            index=1,
            argv=["codex"],
            stdout="native provider-secret",
            stderr="",
            secrets=("provider-secret",),
        )
        return {"ok": True}, JudgeInvocation(
            id=kwargs["invocation_id"],
            kind="agent",
            name="agent review",
            status="completed",
            adapter="codex-cli",
            adapter_version="1.2.3",
        )

    monkeypatch.setattr(_agents, "run", agent)
    judge("llm", prompt="LLM review")
    assert not agent_log.exists()
    judge("agent", prompt="Agent review")
    judge("llm", prompt="LLM review again")

    for path, kind, count in [(llm_log, "llm", 2), (agent_log, "agent", 1)]:
        events = [json.loads(line) for line in path.read_text().splitlines()]
        completed = [e for e in events if e["event"] == "completed"]
        assert len(completed) == count
        assert {e["invocation"]["kind"] for e in completed} == {kind}
        ids = {e["id"] for e in completed}
        assert all(e["id"] in ids for e in events)
        assert ("native [REDACTED]" in path.read_text()) is (kind == "agent")
        assert "provider-secret" not in path.read_text()


def test_missing_evidence_raises_without_inventing_zero(monkeypatch, verify_env):
    result, log = verify_env
    monkeypatch.setattr(_llm, "_request", lambda *args: pytest.fail("must not call model"))
    with pytest.raises(EvidenceError):
        judge("llm", prompt="Inspect", files=[str(result.parent / "missing")])
    assert not result.exists()
    assert json.loads(log.read_text().splitlines()[-1])["event"] == "failed"


def test_failed_judge_keeps_responses_and_no_result(monkeypatch, verify_env):
    result, log = verify_env
    monkeypatch.setattr(_llm, "_request", lambda *args: {"output_text": "invalid provider-secret"})
    with pytest.raises(JudgeError):
        judge("llm", prompt="Inspect")
    assert not result.exists()
    assert "provider-secret" not in log.read_text()
    assert "invalid [REDACTED]" in log.read_text()
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert len([e for e in events if e["event"] == "response"]) == 4
    assert events[-1]["event"] == "failed"


def test_windows_skips_unsupported_directory_fsync(monkeypatch, tmp_path):
    monkeypatch.setattr(_io.os, "name", "nt")
    monkeypatch.setattr(_io.os, "open", lambda *args: pytest.fail("no directory open on Windows"))
    _io._fsync_directory(tmp_path)


def test_schema_references_and_defaults(monkeypatch, verify_env):
    monkeypatch.setattr(_llm, "_request", lambda *args: {"output_text": '{"value":0.5}'})
    schema = {
        "definitions": {"grade": {"type": "number", "minimum": 0, "maximum": 1}},
        "type": "object",
        "properties": {"value": {"$ref": "#/definitions/grade"}, "extra": {"default": 7}},
        "required": ["value"],
    }
    assert judge("llm", prompt="Assess", output_schema=schema) == {"value": 0.5}
    monkeypatch.setattr(_llm, "_request", lambda *args: pytest.fail("invalid schema made a call"))
    for invalid in ({"$ref": "ftp://example.test/schema"}, {"$schema": "draft-2020-12"}):
        with pytest.raises(ValueError):
            judge("llm", prompt="Assess", output_schema=invalid)


def test_initial_prompt_is_redacted(monkeypatch, verify_env):
    _, log = verify_env
    monkeypatch.setattr(_llm, "_request", lambda *args: {"output_text": '{"ok":true}'})
    judge("llm", prompt="Do not disclose provider-secret")
    assert "provider-secret" not in log.read_text()
    assert "[REDACTED]" in log.read_text()
