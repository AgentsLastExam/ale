from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale_verify import JudgeError, ScoredChoice, _agents
from test_agent_adapters import codex_config, completed


@pytest.fixture
def agent_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Create a working program.")
    log = tmp_path / "agent-judge.jsonl"
    monkeypatch.setenv("ALE_HOME", str(workspace))
    monkeypatch.setenv("ALE_STAGE_DIR", str(tmp_path))
    monkeypatch.setenv("ALE_TASK_INSTRUCTION_PATH", str(instruction))
    monkeypatch.setenv("ALE_AGENT_JUDGE_LOG_PATH", str(log))
    monkeypatch.setenv("JUDGE_KEY", "provider-secret")
    monkeypatch.setattr(_agents.os, "geteuid", lambda: 0)
    return {"workspace": workspace, "log": log}


def run():  # type: ignore[no-untyped-def]
    return _agents.run(
        invocation_id="judge-1",
        name="functional",
        prompt="Test it.",
        rubric={
            "no": ScoredChoice(0, "Fails."),
            "yes": ScoredChoice(1, "Works."),
        },
        evidence=(),
        reference=None,
        trajectory=None,
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
    monkeypatch.setattr(_agents.shutil, "which", lambda name: f"/usr/bin/{name}")

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        if "--version" in argv:
            return completed("codex 1.2.3\n")
        calls.append((argv, kwargs))
        return completed(next(outputs))

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    choice, _, invocation = run()
    assert choice == "yes"
    assert len(calls) == 2
    assert calls[1][0][-3:-1] == ["resume", "thread-1"]
    assert "unknown choice 'mostly'" in calls[1][1]["input"]
    assert '{"choice":"no|yes","reasoning":"..."}' in calls[1][1]["input"]
    assert invocation.attempts[1].mode == "schema_repair"


def test_three_repairs_are_the_limit_and_no_fresh_session_is_started(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path]
) -> None:
    calls = []
    monkeypatch.setattr(_agents.shutil, "which", lambda name: f"/usr/bin/{name}")

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
    monkeypatch.setattr(_agents.shutil, "which", lambda name: f"/usr/bin/{name}")

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
    _, reasoning, _ = run()
    assert reasoning == "[REDACTED] works"
    transcript = json.loads(agent_env["log"].read_text())["stdout"]
    assert len(transcript) > 2_000
    _, final = _agents._final_response("codex-cli", transcript)
    assert final is not None
    assert json.loads(final)["choice"] == "yes"
    assert "provider-secret" not in transcript
