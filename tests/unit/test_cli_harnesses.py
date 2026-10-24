from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.config import load_run_config
from ale.core.errors import (
    AgentError,
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
from ale.core.sandbox import ExecResult
from ale.core.taskspec import OperatingSystem, StdioMcpServer
from ale.run.cli.tasks import PRESET_DIR, _harness
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
        if "/.ale-harness/stdout.log" in command:
            root = kwargs["cwd"] + "/.ale-harness/"
            self.files[root + "stdout.log"] += self.native_stdout
            self.files[root + "stderr.log"] += self.native_stderr
            return self.result.model_copy(update={"stdout": "", "stderr": ""})
        if "mkdir -p" in command:
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
            SimpleNamespace(path=name, size=len(data))
            for name, data in self.files.items()
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
    assert OpenClawCliHarness().native_logs[0].root == ".openclaw-ale/agents/*/sessions"
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
        "/home/agent/instruction.md",
        sandbox,
        session(authentication="subscription"),
        native_session_id="session-1",
    )

    command = prepared.argv[2]
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


def native_path(tmp_path: Path, relative: str) -> Path:
    path = tmp_path / "native" / relative
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
        native_logs_enabled=True,
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
    (tmp_path / "stdout.log").write_text(
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


def test_codex_declares_native_session_evidence() -> None:
    assert CodexCliHarness().native_logs[0].root == ".codex-ale/sessions"


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
    (tmp_path / "stdout.log").write_text(
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
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "call-patch",
                            "output": "Success",
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


def test_codex_parser_rejects_a_subagent_session_export(tmp_path: Path) -> None:
    (tmp_path / "stdout.log").write_text(
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
    native_path(tmp_path, ".codex-ale/sessions/rollout-session.jsonl").write_text(
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
        {
            "reason": "unconverted_native_files",
            "count": 1,
            "sources": ["native/.codex-ale/sessions/rollout-session.jsonl"],
        },
        {"reason": "native_session_unavailable", "session_id": "session"},
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
                            "usage": {"input": 10, "output": 2},
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
    assert trajectory.final_metrics.total_prompt_tokens == 10


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
            if "/.ale-harness/stdout.log" in " ".join(str(part) for part in argv):
                root = kwargs["cwd"] + "/.ale-harness/"
                requested = json.loads(self.files[root + "invocations.jsonl"].splitlines()[-1])[
                    "expected_session_id"
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
    assert sandbox.files["/home/agent/instruction.md"] == instruction.encode("utf-8")
    assert all(instruction not in " ".join(argv) for argv in sandbox.commands)
    assert all(instruction not in env.values() for env in sandbox.environments)
    assert sandbox.files["/home/agent/.ale-harness/stderr.log"] == b"a separate warning\n"
    assert b"warning" not in sandbox.files["/home/agent/.ale-harness/stdout.log"]
    command = " ".join(sandbox.commands[-1])
    if isinstance(harness, GrokBuildHarness):
        assert "--prompt-file /home/agent/instruction.md" in command
    elif isinstance(harness, OpenClawCliHarness):
        assert "--message-file /home/agent/instruction.md" in command
    else:
        assert "Read instruction.md in the current working directory" in command


async def test_empty_resume_cannot_reuse_previous_success() -> None:
    harness = CodexCliHarness()
    sandbox = FakeSandbox()
    sandbox.native_stdout = b'{"type":"thread.started","thread_id":"thread-1"}\n'
    first = await harness.launch("first", sandbox, session(), timeout_sec=60)
    assert first.continuation is not None
    sandbox.native_stdout = b""
    with pytest.raises(NativeContinuationError, match="session ID"):
        await harness.resume("second", first.continuation, sandbox, session(), timeout_sec=60)
    assert sandbox.files["/home/agent/instruction.md"] == b"second"
    records = [
        json.loads(line)
        for line in sandbox.files["/home/agent/.ale-harness/invocations.jsonl"].splitlines()
    ]
    assert records[1]["stdout_offset"] > 0


def test_invocation_boundaries_preserve_resumed_events_after_partial_json(tmp_path: Path) -> None:
    from dataclasses import replace

    from ale.run.harnesses.evidence import jsonl_events

    partial = b'{"type":"unfinished"'
    resumed = b'{"type":"thread.started","thread_id":"resumed"}\n'
    (tmp_path / "stdout.log").write_bytes(partial + resumed + b'{"type":')
    (tmp_path / "invocations.jsonl").write_text(
        json.dumps({"stdout_offset": 0}) + "\n" + json.dumps({"stdout_offset": len(partial)}) + "\n"
    )
    running = replace(context(tmp_path, "codex"), incomplete=True, incomplete_reason="running")
    events, issues = jsonl_events(running)
    assert events == [{"type": "thread.started", "thread_id": "resumed"}]
    assert len(issues) == 1  # The closed first invocation really was truncated.
    _, final_issues = jsonl_events(replace(running, incomplete_reason="agent_interrupted"))
    assert len(final_issues) == 2


def test_disabled_native_collection_reports_summary_scope_without_missing_file_error(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    (tmp_path / "stdout.log").write_text(json.dumps({"payloads": [{"text": "done"}]}))
    native_path(tmp_path, ".openclaw-ale/agents/main/sessions/session.jsonl").write_text(
        "bad native bytes"
    )
    trajectory = OpenClawCliHarness().parse_trajectory(
        replace(context(tmp_path, "openclaw"), native_logs_enabled=False)
    )
    assert trajectory.steps[-1].message == "done"
    assert trajectory.extra["ale"]["evidence_scope"] == "stdout_only"
    assert trajectory.extra["ale"]["limitations"]
    assert not trajectory.extra["ale"].get("parse_issues")
