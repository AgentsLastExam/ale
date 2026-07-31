from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale_verify import JudgeError, ScoredChoice, _agents


def rubric() -> dict[str, ScoredChoice]:
    return {
        "no": ScoredChoice(0, "Fails."),
        "yes": ScoredChoice(1, "Works."),
    }


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


def run(config: dict[str, str]):  # type: ignore[no-untyped-def]
    return _agents.run(
        invocation_id="judge-1",
        name="functional",
        prompt="Inspect and execute the program.",
        rubric=rubric(),
        evidence=(),
        reference=None,
        trajectory=None,
        config=config,
    )


def codex_config() -> dict[str, str]:
    return {
        "adapter": "codex-cli",
        "model": "gpt-5",
        "reasoning_effort": "high",
        "base_url": "https://api.openai.com",
        "api_key_env": "JUDGE_KEY",
    }


def claude_config() -> dict[str, str]:
    return {
        "adapter": "claude-code",
        "model": "claude-sonnet-4",
        "reasoning_effort": "high",
        "base_url": "https://api.anthropic.com",
        "api_key_env": "JUDGE_KEY",
    }


def completed(stdout: str, *, returncode: int = 0):  # type: ignore[no-untyped-def]
    return type("Completed", (), {"stdout": stdout, "stderr": "", "returncode": returncode})()


def test_codex_command_root_home_cwd_version_and_final_response(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path]
) -> None:
    calls = []
    monkeypatch.setattr(_agents.shutil, "which", lambda name: f"/usr/bin/{name}")

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs))
        if "--version" in argv:
            return completed("codex 1.2.3\n")
        return completed(
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"{\\"choice\\":\\"yes\\",\\"reasoning\\":\\"Observed working.\\"}"}}\n'
        )

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    choice, reasoning, invocation = run(codex_config())
    argv, kwargs = calls[-1]
    assert (choice, reasoning) == ("yes", "Observed working.")
    assert argv[:3] == ["/usr/bin/codex", "exec", "--json"]
    assert f'model_reasoning_effort="{codex_config()["reasoning_effort"]}"' in argv
    assert kwargs["cwd"] == agent_env["workspace"]
    assert kwargs["env"]["HOME"].startswith(str(agent_env["log"].parent))
    assert kwargs["env"]["OPENAI_BASE_URL"] == "https://api.openai.com"
    assert invocation.adapter_version == "codex 1.2.3"
    assert "provider-secret" not in agent_env["log"].read_text()


def test_claude_command_and_final_response(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path]
) -> None:
    calls = []
    monkeypatch.setattr(_agents.shutil, "which", lambda name: f"/usr/bin/{name}")

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs))
        if "--version" in argv:
            return completed("2.1.0\n")
        session_id = argv[argv.index("--session-id") + 1]
        return completed(
            json.dumps(
                {
                    "type": "result",
                    "session_id": session_id,
                    "result": '{"choice":"yes","reasoning":"Works."}',
                }
            )
            + "\n"
        )

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    _, _, invocation = run(claude_config())
    argv, kwargs = calls[-1]
    assert argv[0] == "/usr/bin/claude"
    assert "--output-format=stream-json" in argv
    assert "--session-id" in argv
    assert kwargs["env"]["CLAUDE_CONFIG_DIR"].endswith(".claude")
    assert kwargs["env"]["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    assert invocation.adapter == "claude-code"


def test_missing_cli_and_non_root_fail_before_launch(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path]
) -> None:
    monkeypatch.setattr(_agents.shutil, "which", lambda _name: None)
    with pytest.raises(JudgeError, match="not installed"):
        run(codex_config())
    monkeypatch.setattr(_agents.os, "geteuid", lambda: 1000)
    with pytest.raises(JudgeError, match="root"):
        run(codex_config())


def test_version_probe_failure_is_recorded_as_a_failed_invocation(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path]
) -> None:
    monkeypatch.setattr(_agents.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        _agents.subprocess,
        "run",
        lambda *_args, **_kwargs: completed("", returncode=1),
    )
    with pytest.raises(JudgeError) as caught:
        run(codex_config())
    assert caught.value.invocation.status == "failed"
    assert caught.value.invocation.attempts[0].outcome == "failed"


def test_stale_transcript_is_replaced(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, Path]
) -> None:
    agent_env["log"].write_text('{"stale":true}\n')
    monkeypatch.setattr(_agents.shutil, "which", lambda name: f"/usr/bin/{name}")

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        if "--version" in argv:
            return completed("codex 1\n")
        return completed(
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"{\\"choice\\":\\"yes\\",\\"reasoning\\":\\"Fresh.\\"}"}}\n'
        )

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    run(codex_config())
    assert '"stale"' not in agent_env["log"].read_text()
