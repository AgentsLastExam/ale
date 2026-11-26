from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orbit_verify import JudgeError, _agents, _llm, judge
from test_agent_adapters import codex_config, completed


@pytest.fixture
def agent_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Create a working program.")
    log = tmp_path / "agent-judge.jsonl"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"agent": codex_config()}))
    monkeypatch.setenv("ORBIT_HOME", str(workspace))
    monkeypatch.setenv("ORBIT_STAGE_DIR", str(tmp_path))
    monkeypatch.setenv("ORBIT_TASK_INSTRUCTION_PATH", str(instruction))
    monkeypatch.setenv("ORBIT_AGENT_JUDGE_LOG_PATH", str(log))
    monkeypatch.setenv("ORBIT_VERIFY_CONFIG_PATH", str(config))
    monkeypatch.setenv("JUDGE_KEY", "provider-secret")
    monkeypatch.setattr(_agents.os, "geteuid", lambda: 0)
    # Verification's budget allows three retries.
    monkeypatch.setattr(_llm, "_wait_to_retry", lambda failures, _after=None: failures <= 3)
    return {"workspace": workspace, "log": log, "instruction": instruction}


def run():  # type: ignore[no-untyped-def]
    return _agents.run(
        invocation_id="judge-1",
        name="functional",
        prompt="Test it.",
        output_schema={
            "type": "object",
            "properties": {"choice": {"enum": ["no", "yes"]}},
            "required": ["choice"],
        },
        evidence=(),
        config=codex_config(),
    )


def event(thread: str, verdict: dict[str, str]) -> str:
    return (
        json.dumps({"type": "thread.started", "thread_id": thread})
        + "\n"
        + json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(verdict)},
            }
        )
        + "\n"
    )


def test_invalid_verdict_repairs_in_the_same_session_with_concrete_error(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path]
) -> None:
    calls = []
    outputs = iter(
        [
            event("thread-1", {"choice": "mostly", "reasoning": "Maybe."}),
            event("thread-1", {"choice": "yes", "reasoning": "Fixed."}),
        ]
    )

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        if "--version" in argv:
            return completed("codex 1.2.3\n")
        calls.append((argv, kwargs))
        return completed(next(outputs))

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    result, invocation = run()
    assert result["choice"] == "yes"
    assert len(calls) == 2
    assert calls[1][0][-3:-1] == ["resume", "thread-1"]
    assert "choice" in calls[1][1]["input"]
    assert "Output JSON Schema" in calls[1][1]["input"]
    assert invocation.attempts[0].error in calls[1][1]["input"]
    assert invocation.attempts[1].mode == "schema_repair"


def test_timeout_after_schema_repair_keeps_latest_error_and_partial_native_evidence(
    monkeypatch: pytest.MonkeyPatch,
    agent_env: dict[str, Path],
) -> None:
    monkeypatch.setattr(_agents, "_installed_binary", lambda *_args: ("codex", "1.2.3"))
    calls = []

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        if len(calls) == 1:
            return completed(event("thread-1", {"choice": "invalid", "reasoning": "No."}))
        raise subprocess.TimeoutExpired(
            argv,
            600,
            output=b'{"type":"error","message":"provider capacity exhausted provider-secret"}',
            stderr=b"native warning provider-secret",
        )

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    with pytest.raises(JudgeError) as caught:
        run()
    invocation = caught.value.invocation
    assert invocation.attempts[-1].outcome == "timed_out"
    assert "timed out" in invocation.failure
    assert "provider capacity exhausted" in invocation.failure
    assert "unknown choice" not in invocation.failure
    assert "provider-secret" not in invocation.failure
    logs = [
        item
        for line in agent_env["log"].read_text().splitlines()
        if (item := json.loads(line))["event"] == "response"
    ]
    assert len(logs) == 5
    assert [attempt.mode for attempt in invocation.attempts] == [
        "initial",
        "schema_repair",
        "retry",
        "retry",
        "retry",
    ]
    assert "provider capacity exhausted" in logs[-1]["stdout"]
    assert "provider-secret" not in agent_env["log"].read_text()


def test_three_repairs_are_the_limit_and_no_fresh_session_is_started(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path]
) -> None:
    calls = []

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        if "--version" in argv:
            return completed("codex 1.2.3\n")
        calls.append(argv)
        return completed(event("thread-1", {"choice": "invalid", "reasoning": "No."}))

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    with pytest.raises(JudgeError) as caught:
        run()
    assert len(calls) == 4
    assert all("resume" in argv for argv in calls[1:])
    assert len(caught.value.invocation.attempts) == 4


def test_transcript_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path]
) -> None:

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        if "--version" in argv:
            return completed("codex 1.2.3\n")
        return completed(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "output": "x" * 2_100},
                }
            )
            + "\n"
            + event("thread-1", {"choice": "yes", "reasoning": "provider-secret works"})
        )

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    with pytest.raises(JudgeError, match="credential"):
        run()
    transcript = next(
        item["stdout"]
        for line in agent_env["log"].read_text().splitlines()
        if (item := json.loads(line))["event"] == "response"
    )
    assert len(transcript) > 2_000
    _, final = _agents._final_response("codex-cli", transcript)
    assert final is not None
    assert json.loads(final)["choice"] == "yes"
    assert "provider-secret" not in transcript


def test_agent_receives_selected_evidence_during_initial_and_repair_calls(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path]
) -> None:
    artifact = agent_env["workspace"] / "report.pdf"
    artifact.write_bytes(b"%PDF-1.7\n\xff\xfe\x00")
    # The verifier's own trajectory is no resource of the Judge's: it reads what it is given.
    trajectory = agent_env["workspace"] / "trajectory.json"
    trajectory.write_text('{"steps":[]}')
    monkeypatch.setenv("ORBIT_TRAJECTORY_PATH", str(trajectory))
    calls = []

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        if "--version" in argv:
            return completed("codex 1.2.3\n")
        calls.append((argv, kwargs))
        assert "Expected totals: accepted=17, rejected=4." in kwargs["input"]
        assert "Create a working program." in kwargs["input"]
        assert f"file: {artifact}" in kwargs["input"]
        assert str(trajectory) not in kwargs["input"]
        assert "ORBIT_TRAJECTORY_PATH" not in kwargs["env"]
        assert "%PDF" not in kwargs["input"]
        choice = "invalid" if len(calls) == 1 else "yes"
        return completed(event("thread-1", {"choice": choice, "reasoning": "Checked evidence."}))

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    assert (
        judge(
            "agent",
            "content",
            prompt="Compare the artifact against the expected totals.",
            files=[str(artifact)],
            reference="Expected totals: accepted=17, rejected=4.",
            output_schema={
                "type": "object",
                "properties": {"choice": {"enum": ["no", "yes"]}},
                "required": ["choice"],
            },
        )["choice"]
        == "yes"
    )
    assert len(calls) == 2
    assert calls[1][0][-3:-1] == ["resume", "thread-1"]
    events = [json.loads(line) for line in agent_env["log"].read_text().splitlines()]
    evidence = next(e for e in events if e["event"] == "evidence")
    assert [item["kind"] for item in evidence["files"]] == ["file", "reference"]


def test_an_agent_judge_whose_model_refuses_its_input_fails_at_once(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path]
) -> None:
    from orbit_verify import JudgeContextError

    monkeypatch.setattr(_agents.shutil, "which", lambda name: f"/usr/bin/{name}")
    runs = []

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        if "--version" in argv:
            return completed("codex 1.2.3\n")
        runs.append(argv)
        failed = {
            "type": "turn.failed",
            "error": {
                "message": "Codex ran out of room in the model's context window. Start a new "
                "thread or clear earlier history before retrying."
            },
        }
        return completed(
            json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n" + json.dumps(failed),
            returncode=1,
        )

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    with pytest.raises(JudgeContextError, match="larger than its model accepts") as caught:
        judge("agent", "content", prompt="Check the artifact.")
    assert len(runs) == 1
    assert [attempt.outcome for attempt in caught.value.invocation.attempts] == ["failed"]
    events = [json.loads(line) for line in agent_env["log"].read_text().splitlines()]
    assert events[-1]["event"] == "failed"


@pytest.mark.parametrize("unreadable", ["missing", "invalid_utf8"])
def test_configured_instruction_cannot_be_silently_omitted(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path], unreadable: str
) -> None:
    if unreadable == "missing":
        agent_env["instruction"].unlink()
    else:
        agent_env["instruction"].write_bytes(b"\xff\xfe")
    monkeypatch.setattr(
        _agents.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Agent must not start without its instruction"),
    )
    with pytest.raises(RuntimeError, match="evidence"):
        judge(
            "agent",
            "content",
            prompt="Check the artifact.",
            output_schema={
                "type": "object",
                "properties": {"choice": {"enum": ["no", "yes"]}},
                "required": ["choice"],
            },
        )
    events = [json.loads(line) for line in agent_env["log"].read_text().splitlines()]
    assert events[-1]["event"] == "failed"
