from __future__ import annotations

import json
import subprocess
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
    monkeypatch.setattr(_agents.time, "sleep", lambda _delay: None)
    return {"workspace": workspace, "log": log}


def run(config: dict[str, str]):  # type: ignore[no-untyped-def]
    return _agents.run(
        invocation_id="judge-1",
        name="functional",
        prompt="Inspect and execute the program.",
        rubric=rubric(),
        evidence=(),
        config=config,
    )


def codex_config() -> dict[str, str]:
    return {
        "adapter": "codex-cli",
        "version": "1.2.3",
        "model": "gpt-5",
        "reasoning_effort": "high",
        "base_url": "https://api.openai.com",
        "api_key_env": "JUDGE_KEY",
    }


def claude_config() -> dict[str, str]:
    return {
        "adapter": "claude-code",
        "version": "2.1.0",
        "model": "claude-sonnet-4",
        "reasoning_effort": "high",
        "base_url": "https://api.anthropic.com",
        "api_key_env": "JUDGE_KEY",
    }


def completed(stdout: str, *, returncode: int = 0, stderr: str = ""):  # type: ignore[no-untyped-def]
    return type("Completed", (), {"stdout": stdout, "stderr": stderr, "returncode": returncode})()


@pytest.mark.parametrize("config", [codex_config(), claude_config()], ids=["codex", "claude"])
@pytest.mark.parametrize("exit_code", [0, 19])
def test_native_failure_exhausts_internal_retries_and_keeps_cause_despite_stderr_noise(
    monkeypatch: pytest.MonkeyPatch,
    agent_env: dict[str, Path],
    config: dict[str, str],
    exit_code: int,
) -> None:
    monkeypatch.setattr(_agents, "_ensure_binary", lambda *_args: ("agent", config["version"]))
    calls = []

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        native = (
            {"type": "turn.failed", "error": {"message": "capacity exhausted provider-secret"}}
            if config["adapter"] == "codex-cli"
            else {
                "type": "result",
                "is_error": True,
                "subtype": "error_during_execution",
                "errors": ["capacity exhausted provider-secret"],
                "result": '{"choice":"yes","reasoning":"stale verdict"}',
            }
        )
        return completed(
            json.dumps(native), returncode=exit_code, stderr="warning provider-secret\n" * 1000
        )

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    with pytest.raises(JudgeError) as caught:
        run(config)
    assert len(calls) == 4
    invocation = caught.value.invocation
    assert invocation.status == "failed"
    assert [attempt.mode for attempt in invocation.attempts] == ["initial", *["retry"] * 3]
    assert invocation.attempts[0].outcome == "failed"
    assert f"exit code {exit_code}" in invocation.failure
    assert "capacity exhausted" in invocation.failure
    assert "stderr:" in invocation.failure
    assert "provider-secret" not in invocation.failure
    assert "provider-secret" not in agent_env["log"].read_text()
    assert len(invocation.failure) < 2200


@pytest.mark.parametrize("config", [codex_config(), claude_config()], ids=["codex", "claude"])
@pytest.mark.parametrize("failure", ["native_error", "nonzero", "timeout"])
def test_service_failure_retries_then_repairs_a_verdict_within_one_budget(
    monkeypatch: pytest.MonkeyPatch,
    agent_env: dict[str, Path],
    config: dict[str, str],
    failure: str,
) -> None:
    monkeypatch.setattr(_agents, "_ensure_binary", lambda *_args: ("agent", config["version"]))
    calls = []

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs))
        if len(calls) == 1:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 600)
            if failure == "nonzero":
                return completed("", returncode=1, stderr="Service unavailable")
            native = (
                {"type": "turn.failed", "error": {"message": "capacity exhausted"}}
                if config["adapter"] == "codex-cli"
                else {"type": "result", "is_error": True, "errors": ["capacity exhausted"]}
            )
            return completed(json.dumps(native))
        verdict = json.dumps(
            {"choice": "invalid" if len(calls) == 2 else "yes", "reasoning": "Checked."}
        )
        if config["adapter"] == "codex-cli":
            return completed(
                '{"type":"thread.started","thread_id":"thread-2"}\n'
                + json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": verdict}}
                )
            )
        selector = "--session-id" if len(calls) == 2 else "--resume"
        return completed(
            json.dumps(
                {
                    "type": "result",
                    "session_id": argv[argv.index(selector) + 1],
                    "result": verdict,
                }
            )
        )

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    choice, _, invocation = run(config)
    assert choice == "yes"
    assert len(calls) == 3
    assert [attempt.mode for attempt in invocation.attempts] == [
        "initial",
        "retry",
        "schema_repair",
    ]
    assert calls[0][1]["input"] == calls[1][1]["input"]
    assert "unknown choice 'invalid'" in calls[2][1]["input"]
    resume_flag = "resume" if config["adapter"] == "codex-cli" else "--resume"
    assert resume_flag not in calls[1][0]
    assert resume_flag in calls[2][0]


def test_recovered_codex_error_does_not_reject_a_valid_judge_verdict(
    monkeypatch: pytest.MonkeyPatch,
    agent_env: dict[str, Path],
) -> None:
    monkeypatch.setattr(_agents, "_ensure_binary", lambda *_args: ("codex", "1.2.3"))
    monkeypatch.setattr(
        _agents.subprocess,
        "run",
        lambda *_args, **_kwargs: completed(
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"error","message":"Reconnecting"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"choice\\":\\"yes\\",\\"reasoning\\":\\"Verified\\"}"}}\n'
            '{"type":"turn.completed"}\n'
        ),
    )
    choice, _, invocation = run(codex_config())
    assert choice == "yes"
    assert invocation.status == "completed"


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
    assert 'model_provider="ale_verify"' in argv
    assert 'model_providers.ale_verify.base_url="https://api.openai.com/v1"' in argv
    assert "model_providers.ale_verify.supports_websockets=false" in argv
    assert kwargs["cwd"] == agent_env["workspace"]
    assert kwargs["env"]["HOME"].startswith(str(agent_env["log"].parent))
    assert Path(kwargs["env"]["CODEX_HOME"]).is_dir()
    assert kwargs["env"]["OPENAI_BASE_URL"] == "https://api.openai.com"
    assert invocation.adapter_version == "1.2.3"
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
    with pytest.raises(JudgeError, match="unavailable"):
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
            return completed("codex 1.2.3\n")
        return completed(
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"{\\"choice\\":\\"yes\\",\\"reasoning\\":\\"Fresh.\\"}"}}\n'
        )

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    run(codex_config())
    assert '"stale"' not in agent_env["log"].read_text()


def test_missing_or_different_cli_is_installed_at_the_exact_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        _agents.shutil,
        "which",
        lambda name: "/usr/bin/npm" if name == "npm" else "/usr/bin/codex",
    )
    calls = []

    def execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        if argv == ["/usr/bin/codex", "--version"]:
            return completed("codex 9.9.9\n")
        if argv[1] == "install":
            root = Path(argv[argv.index("--prefix") + 1])
            installed = root / "node_modules/.bin/codex"
            installed.parent.mkdir(parents=True)
            installed.write_text("")
            return completed("")
        return completed("codex 1.2.3\n")

    monkeypatch.setattr(_agents.subprocess, "run", execute)
    binary, version = _agents._ensure_binary("codex-cli", "1.2.3", tmp_path)
    assert version == "1.2.3"
    assert binary.endswith("agent-tools/codex-cli-1.2.3/node_modules/.bin/codex")
    assert any("@openai/codex@1.2.3" in argv for argv in calls)


def test_invocation_local_mcp_supports_stdio_and_http_without_solver_inheritance(
    tmp_path: Path,
) -> None:
    home = tmp_path / "agent"
    home.mkdir()
    servers = [
        {
            "name": "local",
            "transport": "stdio",
            "command": "python3",
            "args": ["server.py"],
            "cwd": "/workspace",
        },
        {"name": "remote", "transport": "streamable-http", "url": "https://mcp.test"},
    ]
    claude = _agents._write_mcp_config("claude-code", home, servers)
    assert claude is not None
    payload = json.loads(claude.read_text())
    assert set(payload["mcpServers"]) == {"local", "remote"}

    assert _agents._write_mcp_config("codex-cli", home, servers) is None
    codex = (home / ".codex/config.toml").read_text()
    assert "[mcp_servers.local]" in codex
    assert 'url = "https://mcp.test"' in codex

    with pytest.raises(JudgeError, match="unsupported transport"):
        _agents._write_mcp_config(
            "codex-cli",
            home,
            [{"name": "legacy", "transport": "sse", "url": "https://mcp.test"}],
        )
