from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from ale.core.config import load_run_config
from ale.core.errors import (
    AgentError,
    AgentUnsupportedError,
    ConfigError,
    NativeContinuationError,
    SubscriptionProviderLimitError,
)
from ale.core.harness import (
    EffectiveAgentResources,
    HarnessSession,
    ResolvedMcpServer,
    ResolvedSkill,
    TrajectoryParseContext,
)
from ale.core.sandbox import ExecResult, SandboxFile
from ale.core.taskspec import OperatingSystem, StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.harnesses import _release
from ale.run.harnesses._npm import agent_path, is_windows, reported_version, run_posix
from ale.run.harnesses._release import (
    ReleaseAsset,
    ReleaseAssets,
    ensure_release_binary,
    release_identity,
)
from ale.run.harnesses.base import (
    CONTINUE_PROMPT,
    INVOCATIONS_PATH,
    LOG_ROOT,
    STDOUT_PATH,
    json_lines,
)
from ale.run.harnesses.builtin import NopHarness, OracleHarness, ScriptedPolicyHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.harnesses.codex_cli import CodexCliHarness
from ale.run.harnesses.computer_use import ComputerUseHarness
from ale.run.harnesses.grok_build import GrokBuildHarness
from ale.run.harnesses.openclaw_cli import OpenClawCliHarness
from ale.run.harnesses.openclaw_cli.adapter import _supported_node
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "harness",
    [
        ClaudeCodeHarness(),
        CodexCliHarness(),
        GrokBuildHarness(),
        OpenClawCliHarness(),
        NopHarness(),
        OracleHarness(),
        ScriptedPolicyHarness(),
        ComputerUseHarness(),
    ],
)
def test_shipped_harnesses_declare_both_guest_systems(harness) -> None:
    for operating_system in OperatingSystem:
        harness.validate_os(operating_system)


class FakeSandbox:
    def __init__(self, exit_code: int = 0, *, stdout: str = "", stderr: str = "") -> None:
        self.files: dict[str, bytes] = {}
        self.uploads: list[tuple[str, str]] = []
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.result = ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
        self.native_stdout = stdout.encode()
        self.native_stderr = stderr.encode()

    async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        self.commands.append([str(part) for part in argv])
        self.environments.append(kwargs.get("env") or {})
        command = " ".join(str(part) for part in argv)
        if list(argv) == ["sh", "-c", 'printf %s "$PATH"']:
            return ExecResult(exit_code=0, stdout="/usr/local/bin:/usr/bin:/bin")
        if "2>> " in command:
            root = kwargs["cwd"] + "/.ale-harness/"
            self.files[root + "stdout.log"] += self.native_stdout
            self.files[root + "stderr.log"] += self.native_stderr
            return self.result.model_copy(update={"stdout": "", "stderr": ""})
        if "mkdir -p" in command:
            for match in re.finditer(r": > ('[^']*'|\S+)", command):
                self.files[shlex.split(match.group(1))[0]] = b""
            return ExecResult(exit_code=0)
        return self.result

    async def write_file(self, path, data, **kwargs):  # type: ignore[no-untyped-def]
        self.files[str(path)] = data

    async def read_file(self, path, **kwargs):  # type: ignore[no-untyped-def]
        data = self.files[str(path)]
        offset = kwargs.get("offset", 0)
        length = kwargs.get("length")
        return data[offset:] if length is None else data[offset : offset + length]

    async def list_files(self, path):
        return tuple(
            SandboxFile(path=name, size=len(data))
            for name, data in sorted(self.files.items())
            if name.startswith(str(path) + "/")
        )

    async def upload_dir(self, source, target, **kwargs):  # type: ignore[no-untyped-def]
        self.uploads.append((source, str(target)))


def session(**updates: object) -> HarnessSession:
    values = {
        "episode_id": "episode",
        "gateway_url": "http://gateway",
        "token": "token",
        "model": "test-model",
        "sandbox_id": "sandbox",
        "resources_digest": "sha256:" + "a" * 64,
        "home": "/home/agent",
    }
    values.update(updates)
    return HarnessSession(**values)


@pytest.mark.parametrize(
    ("harness", "path", "native"),
    [
        (CodexCliHarness(), "transcript.jsonl", {"type": "turn.failed", "error": "proof"}),
        (GrokBuildHarness(), ".grok-ale/segment.jsonl", {"type": "error", "message": "proof"}),
        (
            ClaudeCodeHarness(),
            "transcript.jsonl",
            {"type": "result", "is_error": True, "errors": ["proof"]},
        ),
        (OpenClawCliHarness(), "result.json", {"payloads": [{"isError": True, "text": "proof"}]}),
    ],
)
async def test_windows_harness_uses_git_bash_native_paths_and_retains_failure(
    harness, path, native
) -> None:
    inherited_path = r"C:\Windows\System32;C:\Program Files\nodejs"
    sandbox = FakeSandbox(stdout=inherited_path)
    sandbox.native_stdout = json.dumps(native).encode()
    with pytest.raises(AgentError, match="proof"):
        await harness.launch("work", sandbox, session(home=r"C:\Users\Agent User"), timeout_sec=60)
    commands = list(zip(sandbox.commands, sandbox.environments, strict=True))
    shell, env = next(
        (argv, environment)
        for argv, environment in reversed(commands)
        if argv[0] == "C:/Program Files/Git/bin/bash.exe"
    )
    assert shell[1] == "-c"
    assert 'export PATH="/usr/bin:$PATH"' in shell[2]
    assert "'C:/Users/Agent User/" in shell[2]
    assert env["PATH"] == f"C:/Users/Agent User/.local;{inherited_path}"
    assert env["HOME"] == "C:/Users/Agent User"
    if isinstance(harness, GrokBuildHarness):
        assert "/bin/grok.exe'" in shell[2]


@pytest.mark.parametrize("exit_code", [0, 17])
@pytest.mark.parametrize(
    ("harness", "path", "native"),
    [
        (
            CodexCliHarness(),
            "transcript.jsonl",
            {"type": "turn.failed", "error": {"message": "capacity limit reached token"}},
        ),
        (
            GrokBuildHarness(),
            ".grok-ale/segment.jsonl",
            {"type": "error", "message": "capacity limit reached token"},
        ),
        (
            ClaudeCodeHarness(),
            "transcript.jsonl",
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "errors": ["capacity limit reached token"],
            },
        ),
        (
            OpenClawCliHarness(),
            "result.json",
            {"payloads": [{"isError": True, "text": "capacity limit reached token"}]},
        ),
    ],
    ids=["codex", "grok", "claude", "openclaw"],
)
async def test_native_failures_keep_the_cause_exit_code_and_stderr(
    harness, path, native, exit_code
) -> None:
    sandbox = FakeSandbox(exit_code, stderr="shell diagnostic token")
    sandbox.native_stdout = json.dumps(native).encode()
    sandbox.native_stderr = b"warning token\n" * 1000
    with pytest.raises(AgentError) as caught:
        await harness.launch("work", sandbox, session(), timeout_sec=60)
    detail = str(caught.value)
    assert "capacity limit reached" in detail
    assert f"exit code {exit_code}" in detail
    assert "stderr:" in detail
    assert "token" not in detail
    assert len(detail) < 3000


async def test_openclaw_declares_native_conversation_without_post_exit_copy() -> None:
    sandbox = FakeSandbox(17)
    sandbox.native_stdout = b'{"payloads": [{"isError": true, "text": "x"}]}'
    with pytest.raises(AgentError):
        await OpenClawCliHarness().launch("work", sandbox, session(), timeout_sec=60)
    assert OpenClawCliHarness().logs == (LOG_ROOT, ".openclaw-ale/agents/main/sessions")
    assert not any("cp --" in " ".join(argv) for argv in sandbox.commands)


@pytest.mark.parametrize(
    "harness", [CodexCliHarness(), GrokBuildHarness(), ClaudeCodeHarness(), OpenClawCliHarness()]
)
async def test_nonzero_process_failure_keeps_stdout_when_stderr_has_only_warning(harness) -> None:
    sandbox = FakeSandbox(9, stdout="native startup detail token", stderr="warning token")
    with pytest.raises(AgentError) as caught:
        await harness.launch("work", sandbox, session(), timeout_sec=60)
    assert "native startup detail" in str(caught.value)
    assert "warning" in str(caught.value)
    assert "exit code 9" in str(caught.value)
    assert "token" not in str(caught.value)


async def test_codex_recovered_transport_and_tool_errors_do_not_fail_success() -> None:
    sandbox = FakeSandbox()
    sandbox.native_stdout = b"\n".join(
        [
            b'{"type":"thread.started","thread_id":"thread-1"}',
            b'{"type":"error","message":"Reconnecting"}',
            b'{"type":"item.completed","item":{"type":"command_execution","exit_code":1}}',
            b'{"type":"item.completed","item":{"type":"error",'
            b'"message":"optional metadata unavailable"}}',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"Done"}}',
            b'{"type":"turn.completed"}',
        ]
    )
    result = await CodexCliHarness().launch("work", sandbox, session(), timeout_sec=60)
    assert result.exit_code == 0
    assert result.final_message == "Done"


async def test_nonzero_codex_does_not_treat_solver_output_as_failure_diagnostic() -> None:
    sandbox = FakeSandbox(1, stderr="native process failed")
    sandbox.native_stdout = (
        b'{"type":"item.completed","item":{"type":"agent_message","text":"PRIVATE TASK ANSWER"}}'
    )
    with pytest.raises(AgentError) as caught:
        await CodexCliHarness().launch("work", sandbox, session(), timeout_sec=60)
    assert "PRIVATE TASK ANSWER" not in str(caught.value)
    assert "native process failed" in str(caught.value)


async def test_subscription_failure_keeps_native_reason_before_long_stderr() -> None:
    sandbox = FakeSandbox(1)
    sandbox.native_stdout = b'{"type":"turn.failed","error":{"message":"rate limit reached token"}}'
    sandbox.native_stderr = b"unrelated warning\n" * 1000
    with pytest.raises(SubscriptionProviderLimitError) as caught:
        await CodexCliHarness().launch(
            "work", sandbox, session(authentication="subscription"), timeout_sec=60
        )
    assert "rate limit reached" in str(caught.value)
    assert "exit code 1" in str(caught.value)
    assert "token" not in str(caught.value)


async def test_oracle_nonzero_keeps_both_streams_and_redacts_credentials() -> None:
    result = await OracleHarness().launch(
        "",
        FakeSandbox(3, stdout="actual error token", stderr="warning token"),
        session(),
        timeout_sec=60,
    )
    assert result.exit_code == 3
    assert "actual error" in result.final_message
    assert "warning" in result.final_message
    assert "token" not in result.final_message


def resources(tmp_path: Path) -> EffectiveAgentResources:
    skill = tmp_path / "resource-proof"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# proof\n")
    return EffectiveAgentResources(
        skills=(
            ResolvedSkill(
                name="resource-proof",
                path=skill,
                source_layers=("task",),
                declared_sources=("skills/resource-proof",),
                digest="sha256:" + "b" * 64,
                reportable=True,
            ),
        ),
        mcp_servers=(
            ResolvedMcpServer(
                name="task-proof",
                server=StdioMcpServer(
                    name="task-proof",
                    transport="stdio",
                    command="python3",
                    args=("{home}/server.py",),
                    environment={"PROOF_HOME": "{home}"},
                ),
                source_layers=("task",),
                declared_sources=("mcp/task-proof.toml",),
                digest="sha256:" + "c" * 64,
                reportable=True,
            ),
        ),
        digest="sha256:" + "d" * 64,
    )


@pytest.mark.parametrize(
    ("name", "harness_type"),
    [
        ("grok-build", GrokBuildHarness),
        ("codex-cli", CodexCliHarness),
        ("openclaw-cli", OpenClawCliHarness),
    ],
)
def test_presets_are_complete_and_construct_strict_harnesses(name, harness_type) -> None:
    config = load_run_config(preset_path=PRESET_DIR / f"{name}.toml")
    harness = _harness(config)

    assert isinstance(harness, harness_type)
    assert config.gateway.dialect == "openai-responses"
    assert config.gateway.limits.max_total_tokens == "unlimited"


def test_codex_preset_uses_current_default_model() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "codex-cli.toml")

    assert config.agent.model == "gpt-5.6-luna"


def test_grok_backend_follows_gateway_dialect() -> None:
    config = load_run_config(
        preset_path=PRESET_DIR / "grok-build.toml",
        overrides=['gateway.dialect="openai-chat-completions"'],
    )
    harness = _harness(config)

    assert isinstance(harness, GrokBuildHarness)
    assert harness.gateway_dialect == "openai-chat-completions"


@pytest.mark.parametrize(
    ("preset", "dialect"),
    [
        ("claude-code", "openai-chat-completions"),
        ("codex-cli", "openai-chat-completions"),
        ("openclaw-cli", "anthropic"),
    ],
)
def test_harnesses_reject_unsupported_gateway_dialects(preset: str, dialect: str) -> None:
    config = load_run_config(
        preset_path=PRESET_DIR / f"{preset}.toml",
        overrides=[f'gateway.dialect="{dialect}"'],
    )

    with pytest.raises(ConfigError, match=r"requires gateway\.dialect"):
        _harness(config)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: GrokBuildHarness(settings={"max_truns": 2}),
        lambda: CodexCliHarness(settings={"reasoning": "high"}),
        lambda: OpenClawCliHarness(settings={"provider_name": "openai"}),
    ],
)
def test_unknown_harness_settings_fail(factory) -> None:
    with pytest.raises(ConfigError):
        factory()


@pytest.mark.parametrize(
    "value",
    [0, -1, False],
)
def test_native_limit_sentinels_are_strict(value) -> None:
    with pytest.raises(ConfigError):
        GrokBuildHarness(settings={"max_turns": value})
    with pytest.raises(ConfigError):
        OpenClawCliHarness(settings={"timeout_seconds": value})


async def test_resource_translation_uses_episode_local_native_config(tmp_path: Path) -> None:
    effective = resources(tmp_path)
    current = session()
    cases = [
        (GrokBuildHarness(), "/home/agent/.grok-ale/config.toml"),
        (CodexCliHarness(), "/home/agent/.codex-ale/config.toml"),
        (OpenClawCliHarness(), "/home/agent/.openclaw-ale/openclaw.json"),
    ]
    for harness, config_path in cases:
        sandbox = FakeSandbox()
        harness.validate_resources(effective)
        await harness.install_resources(sandbox, current, effective)  # type: ignore[arg-type]
        rendered = sandbox.files[config_path].decode()
        assert "task-proof" in rendered
        assert "resource-proof" in sandbox.uploads[0][1]
        assert "http://gateway/v1" in rendered
        assert "token" not in rendered


@pytest.mark.parametrize(
    ("home", "expected"),
    [
        ("/home/agent", ["chmod", "+x", "/home/agent/skill/run tool.sh"]),
        (
            r"C:\Users\Agent User",
            [
                "C:/Program Files/Git/bin/bash.exe",
                "-c",
                "export PATH=\"/usr/bin:$PATH\"; chmod +x 'C:/Users/Agent User/skill/run tool.sh'",
            ],
        ),
    ],
)
async def test_posix_utilities_run_directly_on_linux_and_through_git_bash_on_windows(
    home: str, expected: list[str]
) -> None:
    sandbox = FakeSandbox()
    await run_posix(sandbox, home, "chmod", "+x", agent_path(home) / "skill/run tool.sh")  # type: ignore[arg-type]
    assert sandbox.commands == [expected]


async def test_a_failed_posix_utility_raises_with_its_command_and_error() -> None:
    sandbox = FakeSandbox(exit_code=1, stderr="chmod: cannot access '/x': No such file\n")
    with pytest.raises(AgentError, match=r"^chmod \+x /x failed: chmod: cannot access"):
        await run_posix(sandbox, "/home/agent", "chmod", "+x", "/x")  # type: ignore[arg-type]
    with pytest.raises(AgentError, match=r"failed: exit 1$"):
        await run_posix(FakeSandbox(exit_code=1), "/home/agent", "chmod", "600", "/x")  # type: ignore[arg-type]


@pytest.mark.parametrize("home", ["/home/agent", r"C:\Users\Agent User"], ids=["linux", "windows"])
@pytest.mark.parametrize("name", sorted(path.stem for path in PRESET_DIR.glob("*.toml")))
async def test_every_cli_harness_stages_skill_executables_on_both_systems(
    tmp_path: Path, name: str, home: str
) -> None:
    config = load_run_config(preset_path=PRESET_DIR / f"{name}.toml")
    harness = _harness(config)
    if is_windows(home) and OperatingSystem.WINDOWS not in harness.supported_os:
        pytest.skip(f"{name} supports Linux only")
    effective = resources(tmp_path)
    skill = effective.skills[0].model_copy(update={"executable_files": ("run tool.sh",)})
    effective = effective.model_copy(update={"skills": (skill,), "mcp_servers": ()})
    sandbox = FakeSandbox()
    current = session(home=home, model=config.agent.model)
    await harness.install_resources(sandbox, current, effective)  # type: ignore[arg-type]
    chmod = ["chmod", "+x", f"{sandbox.uploads[0][1]}/run tool.sh"]
    if is_windows(home):
        assert not [argv for argv in sandbox.commands if argv[0] in ("mkdir", "chmod")]
        assert [
            "C:/Program Files/Git/bin/bash.exe",
            "-c",
            'export PATH="/usr/bin:$PATH"; ' + shlex.join(chmod),
        ] in sandbox.commands
    else:
        assert chmod in sandbox.commands


async def test_grok_chat_gateway_selects_native_chat_backend(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    harness = GrokBuildHarness(gateway_dialect="openai-chat-completions")

    await harness.install_resources(sandbox, session(), resources(tmp_path))  # type: ignore[arg-type]

    rendered = sandbox.files["/home/agent/.grok-ale/config.toml"].decode()
    assert 'api_backend = "chat_completions"' in rendered


@pytest.mark.parametrize(
    ("harness", "config_path"),
    [
        (CodexCliHarness(), "/home/agent/.codex-ale/config.toml"),
        (GrokBuildHarness(), "/home/agent/.grok-ale/config.toml"),
    ],
)
async def test_subscription_config_uses_the_same_gateway_provider(
    tmp_path: Path, harness: object, config_path: str
) -> None:
    sandbox = FakeSandbox()
    current = session(authentication="subscription")

    await harness.install_resources(sandbox, current, resources(tmp_path))  # type: ignore[attr-defined, arg-type]

    rendered = sandbox.files[config_path].decode()
    assert "http://gateway/v1" in rendered
    assert "ALE_GATEWAY_TOKEN" in rendered
    assert "task-proof" in rendered
    if isinstance(harness, CodexCliHarness):
        assert 'model = "test-model"' in rendered
        assert 'model_provider = "ale"' in rendered
    else:
        assert '[model."test-model"]' in rendered


async def test_codex_subscription_launch_drops_api_credentials() -> None:
    sandbox = FakeSandbox()
    sandbox.native_stdout = b'{"type":"thread.started","thread_id":"thread-1"}\n'

    await CodexCliHarness().launch(  # type: ignore[arg-type]
        "test",
        sandbox,
        session(authentication="subscription"),
        timeout_sec=60,
    )

    assert sandbox.environments[-1]["ALE_GATEWAY_TOKEN"] == "token"
    assert "unset OPENAI_API_KEY OPENAI_BASE_URL CODEX_ACCESS_TOKEN" in sandbox.commands[-1][2]


async def test_grok_subscription_launch_uses_gateway_and_requested_model() -> None:
    sandbox = FakeSandbox()
    sandbox.native_stdout = b'{"type":"end","sessionId":"session-1"}\n'

    prepared = await GrokBuildHarness().prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        sandbox,
        session(authentication="subscription"),
        native_session_id="session-1",
    )

    command = prepared.script
    assert "--model test-model" in command
    assert "unset XAI_API_KEY GROK_CLI_CHAT_PROXY_BASE_URL" in command
    assert prepared.env["ALE_GATEWAY_TOKEN"] == "token"
    assert prepared.env["GROK_WORKSPACE_DATA_COLLECTION_DISABLED"] == "1"


async def test_openclaw_declares_explicit_reasoning_support(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    harness = OpenClawCliHarness(settings={"thinking": "high"})

    await harness.install_resources(sandbox, session(), resources(tmp_path))  # type: ignore[arg-type]

    rendered = json.loads(sandbox.files["/home/agent/.openclaw-ale/openclaw.json"])
    assert rendered["models"]["providers"]["openai"]["models"][0]["reasoning"] is True


async def test_openclaw_runs_without_its_respawning_launcher() -> None:
    prepared = await OpenClawCliHarness().prepare_command(
        "/home/agent/.ale-harness/prompt.md", FakeSandbox(), session(), native_session_id=None
    )

    assert prepared.env["OPENCLAW_NO_RESPAWN"] == "1"


def native_path(tmp_path: Path, relative: str) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def context(tmp_path: Path, harness: str) -> TrajectoryParseContext:
    return TrajectoryParseContext(
        episode_id="episode",
        trajectory_id=f"{harness}-trajectory",
        instruction="Use the task MCP.",
        logs_dir=tmp_path,
        model="test-model",
        agent_version="1.0",
        blobs=BlobStore(tmp_path),
        session_id="session",
    )


def test_grok_parser_preserves_mcp_call_and_result(tmp_path: Path) -> None:
    native_path(tmp_path, ".grok-ale/sessions/session/chat_history.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "<user_query>\nUse the task MCP.\n</user_query>",
                            }
                        ],
                        "prompt_index": 0,
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "content": "working",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "name": "use_tool",
                                "arguments": json.dumps(
                                    {
                                        "tool_name": "task-proof__derive_fragment",
                                        "tool_input": {"nonce": "abc"},
                                    }
                                ),
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-1",
                        "content": '{"fragment":"xyz"}',
                    }
                ),
            ]
        )
    )
    trajectory = GrokBuildHarness().parse_trajectory(context(tmp_path, "grok"))
    call = trajectory.steps[1].tool_calls[0]

    assert trajectory.steps[0].message == "Use the task MCP."
    assert call.extra["ale"]["mcp"] == {
        "server": "task-proof",
        "tool": "derive_fragment",
    }
    assert trajectory.steps[1].observation.results[0].content == '{"fragment":"xyz"}'


def test_codex_parser_preserves_mcp_call_and_result(tmp_path: Path) -> None:
    native_path(tmp_path, STDOUT_PATH).write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "call-1",
                            "type": "mcp_tool_call",
                            "server": "task-proof",
                            "tool": "derive_fragment",
                            "arguments": {"nonce": "abc"},
                            "result": {"content": [{"type": "text", "text": "xyz"}]},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 10, "output_tokens": 3},
                    }
                ),
            ]
        )
    )
    trajectory = CodexCliHarness().parse_trajectory(context(tmp_path, "codex"))

    assert trajectory.steps[1].tool_calls[0].extra["ale"]["mcp"]["server"] == "task-proof"
    assert trajectory.steps[1].observation.results[0].content[0].text == "xyz"
    assert trajectory.final_metrics.total_prompt_tokens == 10


async def test_codex_traces_its_run_where_its_evidence_is_mirrored() -> None:
    harness = CodexCliHarness()
    prepared = await harness.prepare_command(
        "/home/agent/.ale-harness/prompt.md", FakeSandbox(), session(), native_session_id=None
    )

    assert harness.logs == (LOG_ROOT, ".codex-ale/sessions", ".codex-ale/rollout-traces")
    assert prepared.env["CODEX_ROLLOUT_TRACE_ROOT"] == "/home/agent/.codex-ale/rollout-traces"


async def test_codex_does_not_export_a_post_exit_session_copy() -> None:
    sandbox = FakeSandbox()
    sandbox.native_stdout = b'{"type":"thread.started","thread_id":"thread-1"}\n'

    await CodexCliHarness().launch(  # type: ignore[arg-type]
        "test",
        sandbox,
        session(),
        timeout_sec=60,
    )

    assert not any("cp " in " ".join(argv) for argv in sandbox.commands)


def test_codex_parser_uses_complete_native_function_call_evidence(tmp_path: Path) -> None:
    native_path(tmp_path, STDOUT_PATH).write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "sparse-shell",
                            "type": "command_execution",
                            "command": "echo duplicate",
                            "aggregated_output": "duplicate",
                            "exit_code": 0,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "mcp-transcript",
                            "type": "mcp_tool_call",
                            "server": "task-proof",
                            "tool": "derive_fragment",
                            "arguments": {"nonce": "abc"},
                            "result": {"content": [{"type": "text", "text": "xyz"}]},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "message-1", "type": "agent_message", "text": "done"},
                    }
                ),
            ]
        )
    )
    native_path(tmp_path, ".codex-ale/sessions/rollout-session.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "session"},
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "<environment_context/>"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Use the task MCP.\n"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "update_plan",
                            "arguments": '{"plan":[]}',
                            "call_id": "call-plan",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-plan",
                            "output": "Plan updated",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "namespace": "mcp__task_proof",
                            "name": "derive_fragment",
                            "arguments": '{"nonce":"abc"}',
                            "call_id": "call-mcp",
                        },
                    }
                ),
                # A model's own call leaves the end record a code-mode script's call does.
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "mcp_tool_call_end",
                            "call_id": "call-mcp",
                            "invocation": {
                                "server": "task-proof",
                                "tool": "derive_fragment",
                                "arguments": {"nonce": "abc"},
                            },
                            "result": {"Ok": {"content": [{"type": "text", "text": "xyz"}]}},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-mcp",
                            "output": "native fallback",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "web_search_call",
                            "id": "web-1",
                            "status": "completed",
                            "action": {"type": "search", "query": "example.com"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "tool_search_call",
                            "id": "search-1",
                            "call_id": "call-search",
                            "status": "completed",
                            "arguments": {"query": "available tools"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "tool_search_output",
                            "call_id": "call-search",
                            "status": "completed",
                            "tools": [],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "id": "patch-1",
                            "call_id": "call-patch",
                            "name": "apply_patch",
                            "input": "*** Begin Patch\n*** End Patch\n",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "patch_apply_end",
                            "call_id": "call-patch",
                            "stdout": "Success",
                            "stderr": "",
                            "success": True,
                            "changes": {},
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "call-patch",
                            "output": "Success",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    }
                ),
            ]
        )
    )
    trajectory = CodexCliHarness().parse_trajectory(context(tmp_path, "codex"))
    calls = [call for step in trajectory.steps for call in step.tool_calls or ()]

    assert [call.function_name for call in calls] == [
        "update_plan",
        "mcp__task-proof__derive_fragment",
        "web.run",
        "tool_search_tool",
        "apply_patch",
    ]
    assert calls[1].extra["ale"]["mcp"] == {
        "server": "task-proof",
        "tool": "derive_fragment",
    }
    assert trajectory.steps[1].observation.results[0].content == "Plan updated"
    assert trajectory.steps[2].observation.results[0].content[0].text == "xyz"
    assert '"status": "completed"' in trajectory.steps[3].observation.results[0].content
    assert '"tools": []' in trajectory.steps[4].observation.results[0].content
    assert trajectory.steps[5].observation.results[0].content == "Success"
    assert trajectory.steps[-1].message == "done"


def test_codex_function_output_images_become_atif_images(tmp_path: Path) -> None:
    png = base64.b64encode(b"\x89PNG fake").decode()
    rollout = [
        {"type": "session_meta", "payload": {"id": "session"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Use the task MCP."}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "view_image",
                "arguments": '{"path":"shot.png"}',
                "call_id": "call-image",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-image",
                "output": [
                    {"type": "input_text", "text": "the screen"},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{png}"},
                    {"type": "input_image", "image_url": "https://example.test/remote.png"},
                ],
            },
        },
    ]
    native_path(tmp_path, ".codex-ale/sessions/rollout-x-session.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in rollout)
    )
    trajectory = CodexCliHarness().parse_trajectory(context(tmp_path, "codex"))
    content = trajectory.steps[1].observation.results[0].content

    assert [part.type for part in content] == ["text", "image", "text"]
    assert (tmp_path / content[1].source.path).read_bytes() == b"\x89PNG fake"
    assert "remote.png" in content[2].text


def test_codex_children_stay_raw_evidence_without_the_root_rollout(tmp_path: Path) -> None:
    native_path(tmp_path, STDOUT_PATH).write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "session"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "root-shell",
                            "type": "command_execution",
                            "command": "echo root",
                            "aggregated_output": "root",
                            "exit_code": 0,
                        },
                    }
                ),
            ]
        )
    )
    native_path(tmp_path, ".codex-ale/sessions/rollout-child.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "child", "parent_thread_id": "session"},
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": '{"cmd":"echo child"}',
                            "call_id": "child-call",
                        },
                    }
                ),
            ]
        )
    )
    trajectory = CodexCliHarness().parse_trajectory(context(tmp_path, "codex"))
    calls = [call for step in trajectory.steps for call in step.tool_calls or ()]

    assert [call.tool_call_id for call in calls] == ["root-shell"]
    assert trajectory.extra["ale"]["parse_issues"] == [
        {"reason": "native_session_unavailable", "session_id": "session"},
        {
            "reason": "unconverted_native_files",
            "count": 1,
            "sources": [".codex-ale/sessions/rollout-child.jsonl"],
        },
    ]


#: Where Codex keeps one day's rollouts, each named `rollout-<timestamp>-<thread id>.jsonl`.
CODEX_DAY = ".codex-ale/sessions/2026/10/26"
OPENCLAW_SESSIONS = ".openclaw-ale/agents/main/sessions"


def subagent_refs(trajectory: AtifTrajectory) -> dict[str, list[str]]:
    """Each call's referenced subagent trajectory IDs."""
    return {
        result.source_call_id or "": [ref.trajectory_id or "" for ref in refs]
        for step in trajectory.steps
        for result in (step.observation.results if step.observation else ())
        if (refs := result.subagent_trajectory_ref)
    }


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def codex_message(role: str, text: str) -> dict[str, object]:
    part = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": [{"type": part, "text": text}]},
    }


def codex_subagent_run(tmp_path: Path) -> TrajectoryParseContext:
    """Thread ``session``, whose code-mode script ``spawn`` started thread ``child``.

    The script then sent the child a second input.
    """
    script = (
        "const child = await tools.multi_agent_v1__spawn_agent({message: 'Wait.'});\n"
        "await tools.multi_agent_v1__send_input({target: child.agent_id, message: 'Reply.'});"
    )
    write_jsonl(
        tmp_path / f"{CODEX_DAY}/rollout-2026-10-26T00-00-00-session.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "session"}},
            codex_message("user", "Use the task MCP."),
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "spawn",
                    "name": "exec",
                    "input": script,
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "spawn",
                    "output": '{"agent_id": "child"}',
                },
            },
        ],
    )
    write_jsonl(
        tmp_path / f"{CODEX_DAY}/rollout-2026-10-26T00-00-01-child.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "child", "parent_thread_id": "session"}},
            codex_message("user", "<environment_context/>"),
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Wait."}},
            codex_message("assistant", "Waiting."),
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Reply."}},
            codex_message("assistant", "DONE"),
        ],
    )
    return context(tmp_path, "codex")


def openclaw_subagent_run(tmp_path: Path) -> TrajectoryParseContext:
    """Session ``session``, whose ``sessions_spawn`` call ``spawn`` started session ``child``."""
    sessions = tmp_path / OPENCLAW_SESSIONS
    write_jsonl(
        sessions / "session.jsonl",
        [
            {"type": "message", "message": {"role": "user", "content": "Use the task MCP."}},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "spawn",
                            "name": "sessions_spawn",
                            "arguments": {"task": "Write the marker."},
                        }
                    ],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "spawn",
                    "toolName": "sessions_spawn",
                    "content": [{"type": "text", "text": "accepted"}],
                    "details": {"childSessionKey": "agent:main:subagent:run-1"},
                },
            },
        ],
    )
    write_jsonl(
        sessions / "child.jsonl",
        [
            {
                "type": "message",
                "message": {"role": "user", "content": "[Subagent Task] Write the marker."},
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "write",
                            "name": "write",
                            "arguments": {"path": "marker.txt", "content": "MARKER"},
                        }
                    ],
                    "usage": {"input": 10, "output": 1, "cost": {"total": 0.01}},
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "write",
                    "content": [{"type": "text", "text": "Wrote 6 bytes."}],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Wrote the marker."}],
                    "usage": {"input": 20, "output": 1},
                },
            },
        ],
    )
    (sessions / "sessions.json").write_text(
        json.dumps(
            {
                "agent:main:explicit:session": {"sessionId": "session"},
                "agent:main:subagent:run-1": {
                    "sessionId": "child",
                    "spawnedBy": "agent:main:explicit:session",
                },
            }
        )
    )
    return context(tmp_path, "openclaw")


#: A thread whose model ran `exec` scripts says when no rollout trace records their calls.
UNTRACED = {"reason": "rollout_trace_unavailable", "session_id": "session"}
SUBAGENT_RUNS = [
    pytest.param(CodexCliHarness(), codex_subagent_run, [UNTRACED], id="codex"),
    pytest.param(OpenClawCliHarness(), openclaw_subagent_run, [], id="openclaw"),
]


@pytest.mark.parametrize(("harness", "run", "untraced"), SUBAGENT_RUNS)
def test_subagents_are_embedded_and_referenced_from_their_spawn(
    tmp_path: Path, harness, run, untraced: list[dict[str, str]]
) -> None:
    trajectory = harness.parse_trajectory(run(tmp_path))
    (subagent,) = trajectory.subagent_trajectories

    assert subagent.trajectory_id == f"{trajectory.trajectory_id}-subagent-child"
    assert subagent.session_id == "child"
    assert subagent_refs(trajectory) == {"spawn": [subagent.trajectory_id]}
    assert subagent.agent == trajectory.agent
    # Converted, the child is no longer unused evidence.
    issues = (trajectory.extra or {}).get("ale", {}).get("parse_issues", [])
    assert issues == untraced


def test_codex_subagent_follows_every_input_its_parent_sent(tmp_path: Path) -> None:
    trajectory = CodexCliHarness().parse_trajectory(codex_subagent_run(tmp_path))
    (subagent,) = trajectory.subagent_trajectories

    # Spawned, then messaged again; injected context stays out.
    assert [(step.source, step.message) for step in subagent.steps] == [
        ("user", "Wait."),
        ("agent", "Waiting."),
        ("user", "Reply."),
        ("agent", "DONE"),
    ]


def test_openclaw_subagent_keeps_its_delegated_task_and_tool_calls(tmp_path: Path) -> None:
    trajectory = OpenClawCliHarness().parse_trajectory(openclaw_subagent_run(tmp_path))
    (subagent,) = trajectory.subagent_trajectories
    first, write, final = subagent.steps

    assert (first.source, first.message) == ("user", "[Subagent Task] Write the marker.")
    assert [call.function_name for call in write.tool_calls] == ["write"]
    (result,) = write.observation.results
    assert result.content[0].text == "Wrote 6 bytes."
    assert final.message == "Wrote the marker."
    assert subagent.final_metrics.total_prompt_tokens == 30
    # A native usage record carries a price; ALE reports tokens only.
    assert "cost" not in trajectory.model_dump_json()


@pytest.mark.parametrize(("harness", "run", "untraced"), SUBAGENT_RUNS)
def test_a_child_no_spawn_result_names_stays_reported(
    tmp_path: Path, harness, run, untraced: list[dict[str, str]]
) -> None:
    parse = run(tmp_path)
    if isinstance(harness, CodexCliHarness):
        orphan = f"{CODEX_DAY}/rollout-2026-10-26T00-50-00-orphan.jsonl"
        write_jsonl(
            tmp_path / orphan,
            [
                {
                    "type": "session_meta",
                    "payload": {"id": "orphan", "parent_thread_id": "session"},
                },
                {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
            ],
        )
    else:
        orphan = f"{OPENCLAW_SESSIONS}/orphan.jsonl"
        index = tmp_path / OPENCLAW_SESSIONS / "sessions.json"
        entries = json.loads(index.read_text())
        entries["agent:main:subagent:orphan"] = {
            "sessionId": "orphan",
            "spawnedBy": "agent:main:explicit:session",
        }
        index.write_text(json.dumps(entries))
        write_jsonl(
            tmp_path / orphan,
            [{"type": "message", "message": {"role": "user", "content": "hi"}}],
        )

    trajectory = harness.parse_trajectory(parse)

    assert [item.session_id for item in trajectory.subagent_trajectories] == ["child"]
    assert trajectory.extra["ale"]["parse_issues"] == [
        {"reason": "unlinked_subagent", "source": orphan},
        {"reason": "unconverted_native_files", "count": 1, "sources": [orphan]},
        *untraced,
    ]


@pytest.mark.parametrize(("harness", "run", "untraced"), SUBAGENT_RUNS)
def test_a_child_claimed_twice_is_converted_once(
    tmp_path: Path, harness, run, untraced: list[dict[str, str]]
) -> None:
    parse = run(tmp_path)
    if isinstance(harness, CodexCliHarness):
        rollout = next((tmp_path / CODEX_DAY).glob("*-child.jsonl"))
        duplicate = f"{CODEX_DAY}/rollout-2026-10-26T00-50-00-child.jsonl"
        shutil.copy(rollout, tmp_path / duplicate)
        expected = [
            {"reason": "unlinked_subagent", "source": duplicate},
            {"reason": "unconverted_native_files", "count": 1, "sources": [duplicate]},
        ]
    else:
        index = tmp_path / OPENCLAW_SESSIONS / "sessions.json"
        entries = json.loads(index.read_text())
        entries["agent:main:subagent:alias"] = entries["agent:main:subagent:run-1"]
        index.write_text(json.dumps(entries))
        expected = []

    trajectory = harness.parse_trajectory(parse)

    assert [item.session_id for item in trajectory.subagent_trajectories] == ["child"]
    issues = (trajectory.extra or {}).get("ale", {}).get("parse_issues", [])
    assert issues == expected + untraced


def test_codex_nested_spawn_agent_calls_nest_their_trajectories(tmp_path: Path) -> None:
    """A thread's own children embed in its trajectory, whichever call form spawned them."""
    sessions = tmp_path / ".codex-ale/sessions"

    def thread(name: str, parent: str | None, first: str, spawns: str | None) -> None:
        records: list[dict[str, object]] = [
            {"type": "session_meta", "payload": {"id": name, "parent_thread_id": parent}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": first}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": first}],
                },
            },
        ]
        if spawns is not None:
            records += [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": json.dumps({"message": f"Help {name}"}),
                        "call_id": f"spawn-{spawns}",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": f"spawn-{spawns}",
                        "output": json.dumps({"agent_id": spawns, "nickname": spawns}),
                    },
                },
            ]
        write_jsonl(sessions / f"rollout-x-{name}.jsonl", records)

    thread("session", None, "Use the task MCP.", "child")
    thread("child", "session", "Help session", "grandchild")
    thread("grandchild", "child", "Help child", None)

    trajectory = CodexCliHarness().parse_trajectory(context(tmp_path, "codex"))
    (child,) = trajectory.subagent_trajectories
    (grandchild,) = child.subagent_trajectories

    assert subagent_refs(trajectory) == {"spawn-child": ["codex-trajectory-subagent-child"]}
    assert subagent_refs(child) == {
        "spawn-grandchild": ["codex-trajectory-subagent-child-subagent-grandchild"]
    }
    assert child.steps[1].tool_calls[0].function_name == "multi_agent.spawn_agent"
    assert [step.message for step in grandchild.steps] == ["Help child"]
    assert "parse_issues" not in (trajectory.extra or {}).get("ale", {})


#: Thread ``session``'s rollout trace bundle, named `trace-<trace id>-<thread id>`.
CODEX_TRACE = ".codex-ale/rollout-traces/trace-1-session"


def codex_code_mode_run(tmp_path: Path) -> TrajectoryParseContext:
    """Model calls ``exec-<n>``, each running a script that made call ``script-<n>``.

    The scripts ran a command, called the task MCP and applied a patch. The rollout keeps
    only the ``exec`` calls; the trace records each script's call, with what it sent, what
    its MCP server reported and what it got back in its bundle's payload files.
    """
    scripts = {
        "exec-1": "await tools.exec_command({cmd: 'cat nonce.txt'});",
        "exec-2": "await tools.mcp__task_proof__derive_fragment({nonce: 'N1'});",
        "exec-3": "await tools.apply_patch('*** Begin Patch\\n*** End Patch');",
    }
    write_jsonl(
        tmp_path / f"{CODEX_DAY}/rollout-2026-10-26T00-00-00-session.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "session"}},
            codex_message("user", "Use the task MCP."),
            *(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": call,
                        "name": "exec",
                        "input": script,
                    },
                }
                for call, script in scripts.items()
            ),
        ],
    )
    bundle = tmp_path / CODEX_TRACE
    payloads = {
        1: {
            "tool_name": "exec_command",
            "payload": {"type": "function", "arguments": '{"cmd": "cat nonce.txt"}'},
        },
        2: {"type": "code_mode_response", "value": {"exit_code": 0, "output": "N1\n"}},
        3: {
            "tool_name": "derive_fragment",
            "tool_namespace": "mcp__task_proof",
            "payload": {"type": "function", "arguments": '{"nonce": "N1"}'},
        },
        4: {"invocation": {"server": "task-proof"}},
        5: {"type": "code_mode_response", "value": {"content": [{"type": "text", "text": "F1"}]}},
        6: {
            "tool_name": "apply_patch",
            "payload": {"type": "custom", "input": "*** Begin Patch\n*** End Patch"},
        },
        7: {"type": "code_mode_response", "value": {}},
    }
    (bundle / "payloads").mkdir(parents=True)
    for number, payload in payloads.items():
        (bundle / f"payloads/{number}.json").write_text(json.dumps(payload))

    def traced(kind: str, **fields: object) -> dict[str, object]:
        return {"thread_id": "session", "payload": {"type": kind, **fields}}

    def cell(number: int) -> dict[str, object]:
        return traced(
            "code_cell_started", runtime_cell_id=str(number), model_visible_call_id=f"exec-{number}"
        )

    def started(number: int, payload: int) -> dict[str, object]:
        return traced(
            "tool_call_started",
            tool_call_id=f"script-{number}",
            requester={"type": "code_cell", "runtime_cell_id": str(number)},
            invocation_payload={"path": f"payloads/{payload}.json"},
        )

    def ended(number: int, payload: int) -> dict[str, object]:
        return traced(
            "tool_call_ended",
            tool_call_id=f"script-{number}",
            status="completed",
            result_payload={"path": f"payloads/{payload}.json"},
        )

    write_jsonl(
        bundle / "trace.jsonl",
        [
            cell(1),
            started(1, 1),
            ended(1, 2),
            cell(2),
            started(2, 3),
            traced(
                "tool_call_runtime_ended",
                tool_call_id="script-2",
                runtime_payload={"path": "payloads/4.json"},
            ),
            ended(2, 5),
            cell(3),
            started(3, 6),
            ended(3, 7),
        ],
    )
    return context(tmp_path, "codex")


def script_steps(trajectory: AtifTrajectory) -> list[tuple[str, str, str]]:
    """Each script call with the model call before it and the call whose script made it."""
    steps = trajectory.steps
    return [
        (
            steps[index - 1].tool_calls[0].tool_call_id,
            call.function_name,
            call.extra["ale"].get("caller"),
        )
        for index, step in enumerate(steps)
        if step.llm_call_count == 0
        for call in step.tool_calls
    ]


def test_codex_code_mode_script_calls_are_steps_no_inference_chose(tmp_path: Path) -> None:
    trajectory = CodexCliHarness().parse_trajectory(codex_code_mode_run(tmp_path))
    calls = {
        call.tool_call_id: (call, step.observation.results[0])
        for step in trajectory.steps
        if step.llm_call_count == 0
        for call in step.tool_calls
    }

    # Each follows the model's `exec` call whose script made it, and names it.
    assert script_steps(trajectory) == [
        ("exec-1", "exec_command", "exec-1"),
        ("exec-2", "mcp__task-proof__derive_fragment", "exec-2"),
        ("exec-3", "apply_patch", "exec-3"),
    ]
    command, result = calls["script-1"]
    assert command.arguments == {"cmd": "cat nonce.txt"}
    assert json.loads(result.content) == {"exit_code": 0, "output": "N1\n"}
    mcp, result = calls["script-2"]
    assert mcp.arguments == {"nonce": "N1"}
    # The trace's namespace sanitizes the server name; Codex's record of the call keeps it.
    assert mcp.extra["ale"]["mcp"] == {"server": "task-proof", "tool": "derive_fragment"}
    assert result.content[0].text == "F1"
    assert result.extra is None
    patch, result = calls["script-3"]
    assert patch.arguments == {"input": "*** Begin Patch\n*** End Patch"}
    # What the script got back, which for a patch is nothing.
    assert result.content == "{}"
    # Every call a model inference chose is an `exec` script.
    assert {
        call.function_name
        for step in trajectory.steps
        if step.llm_call_count is None
        for call in step.tool_calls or ()
    } == {"exec"}
    assert "parse_issues" not in (trajectory.extra or {}).get("ale", {})


def test_codex_script_call_after_a_later_model_call_follows_that_call(tmp_path: Path) -> None:
    parse = codex_code_mode_run(tmp_path)
    log = tmp_path / CODEX_TRACE / "trace.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    # The first script yielded, so its command ran only after the model's next `exec`.
    first = [event for event in events if event["payload"].get("tool_call_id") == "script-1"]
    rest = [event for event in events if event not in first]
    later = next(
        index
        for index, event in enumerate(rest)
        if event["payload"].get("model_visible_call_id") == "exec-2"
    )
    write_jsonl(log, rest[: later + 1] + first + rest[later + 1 :])

    trajectory = CodexCliHarness().parse_trajectory(parse)

    assert script_steps(trajectory)[0] == ("exec-2", "exec_command", "exec-1")
    assert "parse_issues" not in (trajectory.extra or {}).get("ale", {})


def test_codex_code_mode_failed_script_calls_are_errors(tmp_path: Path) -> None:
    parse = codex_code_mode_run(tmp_path)
    payloads = tmp_path / CODEX_TRACE / "payloads"
    # The MCP tool reported an error; the patch never ran.
    reply = {"content": [{"type": "text", "text": "unknown nonce"}], "isError": True}
    (payloads / "5.json").write_text(json.dumps({"type": "code_mode_response", "value": reply}))
    (payloads / "7.json").write_text(json.dumps({"type": "error", "error": "patch rejected"}))

    trajectory = CodexCliHarness().parse_trajectory(parse)
    results = {
        result.source_call_id: result
        for step in trajectory.steps
        if step.llm_call_count == 0
        for result in step.observation.results
    }

    mcp = results["script-2"]
    assert (mcp.content[0].text, mcp.extra) == ("unknown nonce", {"ale": {"is_error": True}})
    patch = results["script-3"]
    assert (patch.content, patch.extra) == ("patch rejected", {"ale": {"is_error": True}})


def test_codex_code_mode_without_its_trace_says_so(tmp_path: Path) -> None:
    parse = codex_code_mode_run(tmp_path)
    shutil.rmtree(tmp_path / CODEX_TRACE)

    trajectory = CodexCliHarness().parse_trajectory(parse)

    assert script_steps(trajectory) == []
    assert trajectory.extra["ale"]["parse_issues"] == [UNTRACED]


def test_codex_trace_reads_only_its_own_payload_files(tmp_path: Path) -> None:
    parse = codex_code_mode_run(tmp_path)
    outside = json.dumps({"type": "code_mode_response", "value": "outside"})
    (tmp_path / "outside.json").write_text(outside)
    log = tmp_path / CODEX_TRACE / "trace.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    for event in events:
        if event["payload"].get("tool_call_id") == "script-1":
            for ref in event["payload"].values():
                if isinstance(ref, dict) and "path" in ref:
                    ref["path"] = "../../../outside.json"
    write_jsonl(log, events)

    trajectory = CodexCliHarness().parse_trajectory(parse)
    (step,) = [
        step
        for step in trajectory.steps
        if step.llm_call_count == 0 and step.tool_calls[0].tool_call_id == "script-1"
    ]

    assert (step.tool_calls[0].arguments, step.observation) == ({}, None)
    # Neither the arguments nor the result were read.
    source = f"{CODEX_TRACE}/../../../outside.json"
    unavailable = {"reason": "trace_payload_unavailable", "source": source}
    assert trajectory.extra["ale"]["parse_issues"] == [unavailable, unavailable]


def test_openclaw_nested_spawns_nest_their_trajectories(tmp_path: Path) -> None:
    sessions = tmp_path / OPENCLAW_SESSIONS

    def session(name: str, first: str, spawns: str | None) -> None:
        records: list[dict[str, object]] = [
            {"type": "message", "message": {"role": "user", "content": first}}
        ]
        if spawns is not None:
            records += [
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "id": f"spawn-{spawns}", "name": "sessions_spawn"}
                        ],
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": f"spawn-{spawns}",
                        "toolName": "sessions_spawn",
                        "content": [{"type": "text", "text": "accepted"}],
                        "details": {"status": "accepted", "childSessionKey": f"key-{spawns}"},
                    },
                },
            ]
        write_jsonl(sessions / f"{name}.jsonl", records)

    session("session", "Use the task MCP.", "child")
    session("child", "Help session", "grandchild")
    session("grandchild", "Help child", None)
    (sessions / "sessions.json").write_text(
        json.dumps(
            {
                "key-session": {"sessionId": "session"},
                "key-child": {"sessionId": "child", "spawnedBy": "key-session"},
                "key-grandchild": {"sessionId": "grandchild", "spawnedBy": "key-child"},
            }
        )
    )

    trajectory = OpenClawCliHarness().parse_trajectory(context(tmp_path, "openclaw"))
    (child,) = trajectory.subagent_trajectories
    (grandchild,) = child.subagent_trajectories

    assert subagent_refs(trajectory) == {"spawn-child": ["openclaw-trajectory-subagent-child"]}
    assert subagent_refs(child) == {
        "spawn-grandchild": ["openclaw-trajectory-subagent-child-subagent-grandchild"]
    }
    assert [step.message for step in grandchild.steps] == ["Help child"]
    assert "parse_issues" not in (trajectory.extra or {}).get("ale", {})


@pytest.mark.parametrize("name", ["grok", "openclaw"])
def test_turns_missing_from_the_native_session_come_from_stdout_with_their_failures(
    tmp_path: Path, name: str
) -> None:
    """A resume that failed before writing to the session still appears, with its error."""
    if name == "grok":
        harness = GrokBuildHarness()
        session = native_path(tmp_path, ".grok-ale/sessions/session/chat_history.jsonl")
        native = [
            {
                "type": "user",
                "prompt_index": 0,
                "content": [{"type": "text", "text": "<user_query>\nfirst\n</user_query>"}],
            },
            {"type": "assistant", "content": "one"},
        ]
        stdout = [
            json.dumps({"type": "error", "message": "late failure", "sessionId": "session"}),
            json.dumps({"type": "error", "message": "could not resume", "sessionId": "session"}),
        ]
    else:
        harness = OpenClawCliHarness()
        session = native_path(tmp_path, ".openclaw-ale/agents/main/sessions/session.jsonl")
        native = [
            {"type": "message", "message": {"role": "user", "content": "first"}},
            {
                "type": "message",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "one"}]},
            },
        ]
        meta = {"agentMeta": {"sessionId": "session"}}
        stdout = [
            json.dumps(
                {
                    "payloads": [{"text": "one"}, {"text": "late failure", "isError": True}],
                    "meta": meta,
                }
            ),
            json.dumps({"status": "error", "error": "could not resume", "meta": meta}),
        ]
    session.write_text("".join(json.dumps(event) + "\n" for event in native))
    first = stdout[0] + "\n"
    native_path(tmp_path, STDOUT_PATH).write_text(first + stdout[1] + "\n")
    native_path(tmp_path, INVOCATIONS_PATH).write_text(
        json.dumps({"prompt": "first", "session_id": None, "stdout_offset": 0})
        + "\n"
        + json.dumps({"prompt": "second", "session_id": "session", "stdout_offset": len(first)})
        + "\n"
    )
    trajectory = harness.parse_trajectory(context(tmp_path, name))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "first"),
        ("agent", "one"),
        ("system", "late failure"),
        ("user", "second"),
        ("system", "could not resume"),
    ]
    assert {"reason": "prompt_not_in_native_session", "invocation": 2} in trajectory.extra["ale"][
        "parse_issues"
    ]


def test_openclaw_parser_preserves_mcp_call_and_result(tmp_path: Path) -> None:
    native_path(tmp_path, ".openclaw-ale/agents/main/sessions/session.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "user",
                            "content": "Use the task MCP.",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "toolCall",
                                    "id": "call-1",
                                    "name": "task-proof__derive_fragment",
                                    "arguments": {"nonce": "abc"},
                                }
                            ],
                            "usage": {
                                "input": 10,
                                "cacheRead": 30,
                                "cacheWrite": 5,
                                "output": 2,
                                "cost": {"total": 0.25},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "toolResult",
                            "toolCallId": "call-1",
                            "content": [{"type": "text", "text": "xyz"}],
                        },
                    }
                ),
            ]
        )
    )
    trajectory = OpenClawCliHarness().parse_trajectory(context(tmp_path, "openclaw"))

    assert trajectory.steps[0].message == "Use the task MCP."
    assert trajectory.steps[1].tool_calls[0].extra["ale"]["mcp"]["tool"] == "derive_fragment"
    assert trajectory.steps[1].observation.results[0].content[0].text == "xyz"
    # Native input excludes the cache; ATIF prompt tokens include reads and writes.
    metrics = trajectory.steps[1].metrics
    assert (metrics.prompt_tokens, metrics.cached_tokens, metrics.completion_tokens) == (45, 30, 2)
    assert trajectory.final_metrics.total_prompt_tokens == 45
    assert trajectory.final_metrics.total_cached_tokens == 30
    assert "cost" not in trajectory.model_dump_json()


def test_openclaw_reports_other_transcripts_but_not_what_it_keeps_beside_them(
    tmp_path: Path,
) -> None:
    sessions = ".openclaw-ale/agents/main/sessions"
    native_path(tmp_path, f"{sessions}/session.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            }
        )
        + "\n"
    )
    # What a live OpenClaw run leaves beside its transcripts.
    for name in (
        "sessions.json",
        "session.trajectory.jsonl",
        "session.trajectory-path.json",
        "session.jsonl.lock",
        "skills-prompts/sha256/50/prompt.txt",
    ):
        native_path(tmp_path, f"{sessions}/{name}").write_text("{}")
    native_path(tmp_path, f"{sessions}/subagent.jsonl").write_text('{"type":"message"}\n')

    trajectory = OpenClawCliHarness().parse_trajectory(context(tmp_path, "openclaw"))

    unconverted = [
        issue
        for issue in trajectory.extra["ale"]["parse_issues"]
        if issue["reason"] == "unconverted_native_files"
    ]
    assert unconverted == [
        {
            "reason": "unconverted_native_files",
            "count": 1,
            "sources": [f"{sessions}/subagent.jsonl"],
        }
    ]


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("v22.22.2", False),
        ("v22.22.3", True),
        ("v23.0.0", False),
        ("v24.15.0", True),
        ("v25.9.0", True),
    ],
)
def test_openclaw_node_runtime_gate(version: str, supported: bool) -> None:
    assert _supported_node(version) is supported


@pytest.mark.parametrize(
    "harness", [ClaudeCodeHarness(), CodexCliHarness(), GrokBuildHarness(), OpenClawCliHarness()]
)
async def test_instruction_file_carries_full_utf8_prompt(harness) -> None:
    class CompletingSandbox(FakeSandbox):
        async def exec(self, argv, **kwargs):
            if "2>> " in " ".join(str(part) for part in argv):
                root = kwargs["cwd"] + "/.ale-harness/"
                requested = json.loads(self.files[root + "invocations.jsonl"].splitlines()[-1])[
                    "session_id"
                ]
                if harness.name == "claude-code":
                    event = {"type": "result", "session_id": requested, "result": "done"}
                elif harness.name == "codex-cli":
                    event = {"type": "thread.started", "thread_id": "thread-1"}
                elif harness.name == "grok-build":
                    event = {"type": "end", "sessionId": requested}
                else:
                    event = {
                        "meta": {"agentMeta": {"sessionId": requested}},
                        "payloads": [{"text": "done"}],
                    }
                self.native_stdout = json.dumps(event).encode() + b"\n"
                self.native_stderr = b"a separate warning\n"
            return await super().exec(argv, **kwargs)

    instruction = "多行任务\nPreserve 'quotes', $HOME, `substitution` and \u2603 exactly."
    sandbox = CompletingSandbox()
    await harness.launch(instruction, sandbox, session(), timeout_sec=60)
    prompt = "/home/agent/.ale-harness/prompt.md"
    assert sandbox.files[prompt] == instruction.encode("utf-8")
    record = json.loads(sandbox.files["/home/agent/.ale-harness/invocations.jsonl"])
    assert record["prompt"] == instruction
    assert all(instruction not in " ".join(argv) for argv in sandbox.commands)
    assert all(instruction not in env.values() for env in sandbox.environments)
    assert sandbox.files["/home/agent/.ale-harness/stderr.log"] == b"a separate warning\n"
    assert b"warning" not in sandbox.files["/home/agent/.ale-harness/stdout.log"]
    command = sandbox.commands[-1]
    # A non-login shell on Linux, like Git Bash on Windows.
    assert command[:2] == ["sh", "-c"]
    if isinstance(harness, GrokBuildHarness):
        assert f"--prompt-file {prompt}" in command[2]
    elif isinstance(harness, OpenClawCliHarness):
        assert f"--message-file {prompt}" in command[2]
    else:
        assert command[2].split("; }")[0].endswith(f"< {prompt}")


async def test_empty_resume_cannot_reuse_previous_success() -> None:
    harness = CodexCliHarness()
    sandbox = FakeSandbox()
    sandbox.native_stdout = b'{"type":"thread.started","thread_id":"thread-1"}\n'
    first = await harness.launch("first", sandbox, session(), timeout_sec=60)
    assert first.continuation is not None
    sandbox.native_stdout = b""
    sandbox.files["/home/agent/.codex-ale/sessions/rollout-x-thread-1.jsonl"] = b""
    with pytest.raises(NativeContinuationError, match="session ID"):
        await harness.resume("second", first.continuation, sandbox, session(), timeout_sec=60)
    assert sandbox.files["/home/agent/.ale-harness/prompt.md"] == b"second"
    records = [
        json.loads(line)
        for line in sandbox.files["/home/agent/.ale-harness/invocations.jsonl"].splitlines()
    ]
    assert records[1]["stdout_offset"] > 0


def test_invocation_boundaries_preserve_resumed_events_after_partial_json(tmp_path: Path) -> None:
    from ale.run.harnesses.base import invocations, stdout_events

    partial = b'{"type":"unfinished"'
    resumed = b'{"type":"thread.started","thread_id":"resumed"}\n'
    native_path(tmp_path, STDOUT_PATH).write_bytes(partial + resumed + b'{"type":')
    native_path(tmp_path, f"{LOG_ROOT}/invocations.jsonl").write_text(
        json.dumps({"prompt": "first", "stdout_offset": 0})
        + "\n"
        + json.dumps({"prompt": "second", "stdout_offset": len(partial)})
        + "\n"
    )
    recorded = invocations(context(tmp_path, "codex"))
    assert [item.prompt for item in recorded] == ["first", "second"]
    turns, issues = stdout_events(recorded)
    assert turns == [[], [{"type": "thread.started", "thread_id": "resumed"}]]
    # Each cut-off line is reported against its own invocation.
    assert [issue["invocation"] for issue in issues] == [1, 2]


def test_json_lines_reports_non_objects_and_partial_lines() -> None:
    events, issues = json_lines('{"a": 1}\n[1]\n{"b"')
    assert events == [{"a": 1}]
    assert [issue["line"] for issue in issues] == [2, 3]


NATIVE_ID = "0b7c5f1e-6f39-4f0a-9d47-6a0f0c8f3a11"
SESSION_FILES = {
    "claude-code": f".claude-config/projects/project/{NATIVE_ID}.jsonl",
    "codex-cli": f".codex-ale/sessions/2026/rollout-2026-{NATIVE_ID}.jsonl",
    "grok-build": f".grok-ale/sessions/{NATIVE_ID}/updates.jsonl",
    "openclaw-cli": f".openclaw-ale/agents/main/sessions/{NATIVE_ID}.jsonl",
}


@pytest.mark.parametrize(
    ("harness", "stdout"),
    [
        (ClaudeCodeHarness(), {"type": "result", "session_id": NATIVE_ID, "result": "done"}),
        (CodexCliHarness(), {"type": "thread.started", "thread_id": NATIVE_ID}),
        (GrokBuildHarness(), {"type": "end", "sessionId": NATIVE_ID}),
        (OpenClawCliHarness(), {"meta": {"agentMeta": {"sessionId": NATIVE_ID}}, "payloads": []}),
    ],
    ids=["claude", "codex", "grok", "openclaw"],
)
async def test_resume_sends_new_input_or_an_explicit_continue_and_records_each_prompt(
    harness, stdout, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = FakeSandbox()
    sandbox.native_stdout = json.dumps(stdout).encode() + b"\n"
    if harness.name != "codex-cli":
        # The CLI would confirm the ID ALE generated; the fake reports a fixed one.
        monkeypatch.setattr("uuid.uuid4", lambda: NATIVE_ID)
    first = await harness.launch("first", sandbox, session(), timeout_sec=60)
    assert first.continuation is not None
    sandbox.files[f"/home/agent/{SESSION_FILES[harness.name]}"] = b"{}\n"
    await harness.resume("second", first.continuation, sandbox, session(), timeout_sec=60)
    await harness.resume(None, first.continuation, sandbox, session(), timeout_sec=60)
    records = [
        json.loads(line)
        for line in sandbox.files["/home/agent/.ale-harness/invocations.jsonl"].splitlines()
    ]
    assert [record["prompt"] for record in records] == ["first", "second", CONTINUE_PROMPT]
    assert [record["session_id"] for record in records[1:]] == [NATIVE_ID, NATIVE_ID]
    assert sandbox.files["/home/agent/.ale-harness/prompt.md"] == CONTINUE_PROMPT.encode()


@pytest.mark.parametrize(
    "harness", [ClaudeCodeHarness(), CodexCliHarness(), GrokBuildHarness(), OpenClawCliHarness()]
)
def test_session_files_match_one_exact_native_id(harness) -> None:
    from pathlib import PurePosixPath

    path = PurePosixPath(SESSION_FILES[harness.name])
    assert harness.is_session_file(path, NATIVE_ID)
    assert not harness.is_session_file(path, NATIVE_ID[1:])
    assert not harness.is_session_file(path, NATIVE_ID[:-1])
    assert all(declared != LOG_ROOT or harness.logs[0] == LOG_ROOT for declared in harness.logs)
    assert any(path.is_relative_to(declared) for declared in harness.logs[1:])


async def test_resume_requires_the_exact_native_session_in_the_sandbox() -> None:
    harness = CodexCliHarness()
    sandbox = FakeSandbox()
    sandbox.native_stdout = json.dumps({"type": "thread.started", "thread_id": NATIVE_ID}).encode()
    first = await harness.launch("first", sandbox, session(), timeout_sec=60)
    assert first.continuation is not None
    sandbox.files[f"/home/agent/.codex-ale/sessions/rollout-{NATIVE_ID}.jsonl.bak"] = b""
    with pytest.raises(NativeContinuationError, match="absent"):
        await harness.resume("second", first.continuation, sandbox, session(), timeout_sec=60)


INSTALLED_BYTES = b"#!/bin/sh\necho 'fixture 2.0.0'\n"
INSTALLED_SHA256 = hashlib.sha256(INSTALLED_BYTES).hexdigest()


class ReleasedHarness(ClaudeCodeHarness):
    """Claude Code's adapter, distributed as a pinned release binary instead of npm."""

    release_assets: ClassVar[ReleaseAssets] = {
        (OperatingSystem.LINUX, "x86_64"): ReleaseAsset(
            "https://example.test/fixture-linux-x86_64", INSTALLED_SHA256
        ),
        (OperatingSystem.LINUX, "aarch64"): ReleaseAsset(
            "https://example.test/fixture-linux-aarch64", "b" * 64
        ),
    }


def test_release_identity_pins_the_whole_table_in_any_order() -> None:
    reordered = dict(reversed(list(ReleasedHarness.release_assets.items())))
    assert release_identity(reordered) == ReleasedHarness().integrity()
    assert ReleasedHarness().integrity().startswith("sha256:")
    changed = {
        **ReleasedHarness.release_assets,
        (OperatingSystem.LINUX, "aarch64"): ReleaseAsset(
            "https://example.test/fixture-linux-aarch64", "c" * 64
        ),
    }
    assert release_identity(changed) != ReleasedHarness().integrity()


def test_release_assets_must_be_https_and_hex_and_only_the_pinned_version_runs() -> None:
    with pytest.raises(ValueError, match="https"):
        ReleaseAsset("http://example.test/fixture", INSTALLED_SHA256)
    with pytest.raises(ValueError, match="hex"):
        ReleaseAsset("https://example.test/fixture", INSTALLED_SHA256.upper())
    assert ReleasedHarness(cli_version=ReleasedHarness.default_version).version()
    with pytest.raises(ConfigError, match="pins release digests"):
        ReleasedHarness(cli_version="0.0.1")


class ReleaseSandbox:
    """Answers the installer's commands; records what it ran."""

    def __init__(self, machine: str, *, reports: str = "fixture 2.0.0", banner: str = "") -> None:
        self.request = SimpleNamespace(os=OperatingSystem.LINUX)
        self.machine = machine
        self.reports = reports
        self.banner = banner
        self.commands: list[list[str]] = []

    async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        argv = [str(part) for part in argv]
        self.commands.append(argv)
        if argv[-1] == 'printf %s "$HOME"':
            return ExecResult(exit_code=0, stdout="/home/user")
        if argv[-1] == "uname -m":
            return ExecResult(exit_code=0, stdout=f"{self.machine}\n")
        if argv[-1] == "--version":
            return ExecResult(exit_code=0, stdout=self.reports, stderr=self.banner)
        return ExecResult(exit_code=0)


@pytest.mark.parametrize(("machine", "url"), [("x86_64", "x86_64"), ("arm64", "aarch64")])
async def test_release_install_picks_the_asset_for_the_sandbox_platform(
    machine: str, url: str
) -> None:
    sandbox = ReleaseSandbox(machine)
    assets = ReleasedHarness.release_assets
    version = await ensure_release_binary(
        sandbox,  # type: ignore[arg-type]
        binary="fixture",
        version="2.0.0",
        assets=assets,
    )
    assert version == "2.0.0"
    (install,) = [command for command in sandbox.commands if command[:2] == ["python3", "-c"]]
    assert install[3:] == [
        f"https://example.test/fixture-linux-{url}",
        assets[(OperatingSystem.LINUX, url)].sha256,
        "/home/user/.local/bin/fixture",
        "",
        "",
    ]


async def test_release_install_fails_closed_on_an_unknown_platform_or_wrong_version() -> None:
    with pytest.raises(AgentUnsupportedError, match="no official release for linux/riscv64"):
        await ensure_release_binary(
            ReleaseSandbox("riscv64"),  # type: ignore[arg-type]
            binary="fixture",
            version="2.0.0",
            assets=ReleasedHarness.release_assets,
        )
    # The program's own version is read before a library banner on stderr, as OpenHands
    # CLI prints "OpenHands SDK v1.21.0" there.
    assert (
        await ensure_release_binary(
            ReleaseSandbox("x86_64", banner="Fixture SDK v9.9.9\n"),  # type: ignore[arg-type]
            binary="fixture",
            version="2.0.0",
            assets=ReleasedHarness.release_assets,
        )
        == "2.0.0"
    )
    with pytest.raises(AgentError, match=r"does not name 2\.0\.0: 'fixture 1\.9\.0'"):
        await ensure_release_binary(
            ReleaseSandbox("x86_64", reports="fixture 1.9.0"),  # type: ignore[arg-type]
            binary="fixture",
            version="2.0.0",
            assets=ReleasedHarness.release_assets,
        )


@pytest.mark.parametrize(
    ("output", "version", "named"),
    [
        ("2.1.227 (Claude Code)\n", "2.1.227", True),
        ("2.1.227 (Claude Code)\n", "2.1.22", False),
        ("Muse Code 1.4.13 (1.4.13-R6996.1)\n", "1.4.13-R6996.1", True),
        ("OpenHands CLI 1.16.0\nOpenHands SDK v1.21.0\n", "1.21.1", False),
        ("forge v2.14.0\n", "2.14.0", True),
        ("", "2.14.0", False),
    ],
)
def test_the_version_output_must_name_the_pinned_version_as_a_token(
    output: str, version: str, named: bool
) -> None:
    assert (reported_version(output, version) == version) is named


def _install(
    source: str, digest: str, target: Path, member: str = "", member_digest: str = ""
) -> subprocess.CompletedProcess[str]:
    """The in-Sandbox installer, run here by this interpreter."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _release._INSTALL,
            source,
            digest,
            str(target),
            member,
            member_digest,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_installer_verifies_reuses_and_replaces_links(tmp_path: Path) -> None:
    source = tmp_path / "download"
    source.write_bytes(INSTALLED_BYTES)
    target = tmp_path / "home/.local/bin/fixture"

    refused = _install(source.as_uri(), "a" * 64, target)
    assert refused.returncode != 0 and "not the pinned" in refused.stderr
    assert not target.exists() and list(target.parent.iterdir()) == []

    assert _install(source.as_uri(), INSTALLED_SHA256, target).returncode == 0
    assert target.read_bytes() == INSTALLED_BYTES and os.access(target, os.X_OK)
    # A matching file is reused without fetching anything.
    assert _install("file:///nonexistent", INSTALLED_SHA256, target).returncode == 0

    protected = tmp_path / "protected"
    protected.write_bytes(b"system")
    target.unlink()
    target.symlink_to(protected)
    assert _install(source.as_uri(), INSTALLED_SHA256, target).returncode == 0
    assert not target.is_symlink() and target.read_bytes() == INSTALLED_BYTES
    assert protected.read_bytes() == b"system"


@pytest.mark.parametrize("kind", ["tar", "zip"])
def test_release_installer_takes_only_the_pinned_member_of_an_archive(
    tmp_path: Path, kind: str
) -> None:
    archive = tmp_path / f"release.{kind}"
    if kind == "tar":
        with tarfile.open(archive, "w:gz") as bundle:
            for name, data in (("cli", INSTALLED_BYTES), ("../escape", b"outside")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                bundle.addfile(info, io.BytesIO(data))
    else:
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("cli", INSTALLED_BYTES)
            bundle.writestr("../escape", b"outside")
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    target = tmp_path / "home/.local/bin/fixture"

    wrong = _install(archive.as_uri(), archive_digest, target, "cli", "a" * 64)
    assert wrong.returncode != 0 and "cli in" in wrong.stderr and not target.exists()
    missing = _install(archive.as_uri(), archive_digest, target, "absent", INSTALLED_SHA256)
    assert missing.returncode != 0 and not target.exists()

    assert (
        _install(archive.as_uri(), archive_digest, target, "cli", INSTALLED_SHA256).returncode == 0
    )
    assert target.read_bytes() == INSTALLED_BYTES and os.access(target, os.X_OK)
    assert sorted(path.name for path in target.parent.iterdir()) == ["fixture"]
    assert not (tmp_path / "home/.local/escape").exists() and not (tmp_path / "escape").exists()
    # The installed member is what a later install compares, not the archive.
    assert (
        _install("file:///nonexistent", archive_digest, target, "cli", INSTALLED_SHA256).returncode
        == 0
    )


def test_an_archive_member_is_pinned_with_its_own_digest() -> None:
    with pytest.raises(ValueError, match="archive member"):
        ReleaseAsset("https://example.test/a.tar.gz", INSTALLED_SHA256, member="cli")
    with pytest.raises(ValueError, match="archive member"):
        ReleaseAsset("https://example.test/a.tar.gz", INSTALLED_SHA256, member_sha256="b" * 64)
    plain = {(OperatingSystem.LINUX, "x86_64"): ReleaseAsset("https://example.test/a", "a" * 64)}
    packed = {
        (OperatingSystem.LINUX, "x86_64"): ReleaseAsset(
            "https://example.test/a", "a" * 64, member="cli", member_sha256="b" * 64
        )
    }
    assert release_identity(plain) != release_identity(packed)
