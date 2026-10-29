from __future__ import annotations

import json

import pytest

import ale_verify
from ale_verify import EvidenceError, JudgeError, _io, _llm, judge, read_result, write


@pytest.fixture
def verify_env(monkeypatch, tmp_path):
    verifier, log, config = (
        tmp_path / name for name in ("verifier", "llm-judge.jsonl", "config.json")
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
        "ALE_LLM_JUDGE_LOG_PATH": log,
        "ALE_VERIFY_CONFIG_PATH": config,
        "JUDGE_KEY": "provider-secret",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(ale_verify, "_VERIFIER_DIR", verifier)
    return verifier, log


def saved(verifier):
    """What the engine reads back from a verifier directory."""
    return read_result(
        (verifier / "reward.json").read_bytes(), (verifier / "verification.json").read_bytes()
    )


def test_verifier_directory_is_harbors():
    assert str(ale_verify._VERIFIER_DIR) == "/logs/verifier"


def test_plain_nested_result_and_repeated_write(verify_env):
    verifier, log = verify_env
    value = {
        "reward": 0.74,
        "dimensions": {"correctness": 0.8},
        "reasoning": ["example", None],
        "measurements": {"count": 200},
    }
    write(value)
    assert json.loads((verifier / "reward.json").read_text()) == {"reward": 0.74}
    assert saved(verifier) == value
    write({"reward": 1})
    assert json.loads((verifier / "reward.json").read_text()) == {"reward": 1.0}
    assert saved(verifier) == {"reward": 1}
    assert not log.exists()
    assert not list(verifier.glob("*.tmp"))


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
    verifier, _ = verify_env
    value = {"reward": raw_score, "details": {"raw": raw_score, "count": 123}}
    normalized = {"reward": expected, "details": value["details"]}
    assert read_result(json.dumps({"reward": raw_score}), json.dumps(value)) == normalized
    write(value)
    assert json.loads((verifier / "verification.json").read_text()) == normalized
    assert json.loads((verifier / "reward.json").read_text()) == {"reward": expected}
    assert saved(verifier) == normalized
    assert value["reward"] == raw_score  # Submission does not change task calculations.


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"overall": 1},
        {"reward": True},
        {"reward": "1"},
        {"reward": -0.1},
        {"reward": -0.0001},
        {"reward": 1.0001},
        {"reward": 10**500},
        {"reward": float("nan")},
        {"reward": 1, "nested": {"x": float("inf")}},
        {"reward": 1, "x": (1, 2)},
        {"reward": 1, 2: "invalid key"},
    ],
)
def test_bad_result_never_replaces_previous_good_result(verify_env, value):
    verifier, _ = verify_env
    write({"reward": 0.5})
    with pytest.raises((TypeError, ValueError)):
        write(value)
    assert saved(verifier) == {"reward": 0.5}


def test_a_result_without_reward_says_so(verify_env):
    with pytest.raises(ValueError, match="must contain reward"):
        write({"overall": 1})


def test_direct_json_has_the_same_contract():
    assert read_result('{"reward":1,"accuracy":0.25}') == {"reward": 1.0, "accuracy": 0.25}
    assert read_result('{"reward":1}', '{"reward":1,"extra":{"any":[false,null,20]}}') == {
        "reward": 1.0,
        "extra": {"any": [False, None, 20]},
    }
    for reward, verification in [
        ('{"reward":0,"reward":1}', None),
        ('{"reward":NaN}', None),
        ('{"reward":1e309}', None),
        ('{"reward":1,"accuracy":"high"}', None),
        ('{"reward":1,"accuracy":true}', None),
        ('{"reward":1,"details":{"count":1}}', None),
        ('{"accuracy":1}', None),
        ("[1]", None),
        ('{"reward":1}', '{"details":"no reward"}'),
        ('{"reward":1}', '{"reward":0.5}'),
        ('{"reward":1}', '{"reward":true}'),
        ('{"reward":1}', '{"reward":1,"reward":1}'),
        ('{"reward":1}', "[]"),
    ]:
        with pytest.raises(ValueError):
            read_result(reward, verification)


def test_judge_returns_data_and_logs_invalid_and_valid_responses(monkeypatch, verify_env):
    verifier, log = verify_env
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
    assert not verifier.exists()
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert len([e for e in events if e["event"] == "response"]) == 3
    errors = [e["error"] for e in events if e["event"] == "error"]
    assert errors[0] in prompts[1]
    assert errors[1] in prompts[2]
    assert "grade" in errors[1]
    assert all("Output JSON Schema" in prompt for prompt in prompts)
    assert events[-1]["response"] == assessment
    write({"reward": assessment["grade"] * 0.5, "review": assessment})
    result = saved(verifier)
    assert result["reward"] == 0.5
    assert result["review"] == assessment


def test_optional_schema_and_calls_after_write(monkeypatch, verify_env):
    monkeypatch.setattr(_llm, "_request", lambda *args: {"output_text": '{"anything":[1,true]}'})
    write({"reward": 0})
    assert judge("llm", prompt="Inspect") == {"anything": [1, True]}
    assert judge("llm", prompt="Inspect again") == {"anything": [1, True]}


def test_each_judge_kind_keeps_its_own_events(monkeypatch, verify_env):
    from ale_verify import JudgeInvocation, _agents

    _, llm_log = verify_env
    agent_log = llm_log.parent / "agent-judge.jsonl"
    monkeypatch.setenv("ALE_AGENT_JUDGE_LOG_PATH", str(agent_log))
    config_path = llm_log.parent / "config.json"
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
    verifier, log = verify_env
    monkeypatch.setattr(_llm, "_request", lambda *args: pytest.fail("must not call model"))
    with pytest.raises(EvidenceError):
        judge("llm", prompt="Inspect", files=[str(log.parent / "missing")])
    assert not verifier.exists()
    assert json.loads(log.read_text().splitlines()[-1])["event"] == "failed"


def test_failed_judge_keeps_responses_and_no_result(monkeypatch, verify_env):
    verifier, log = verify_env
    monkeypatch.setattr(_llm, "_request", lambda *args: {"output_text": "invalid provider-secret"})
    with pytest.raises(JudgeError):
        judge("llm", prompt="Inspect")
    assert not verifier.exists()
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
