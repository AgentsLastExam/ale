"""Strict Claude Code configuration and native argv translation."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from ale.core.errors import (
    AgentUnsupportedError,
    BudgetExceededError,
    ConfigError,
    HarnessLimitError,
    NativeContinuationError,
    TrajectoryReferenceError,
)
from ale.core.harness import HarnessSession, TrajectoryParseContext
from ale.core.sandbox import ExecResult
from ale.run.harnesses.builtin import NopHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit


def test_unknown_setting_is_not_silently_ignored() -> None:
    with pytest.raises(ConfigError, match="max_truns"):
        ClaudeCodeHarness(settings={"max_truns": 10})
    with pytest.raises(ConfigError, match="fallback_model"):
        ClaudeCodeHarness(settings={"fallback_model": "other"})


def test_default_and_unlimited_omit_native_limit_flags() -> None:
    default = ClaudeCodeHarness(settings={"max_turns": "default", "max_budget_usd": "default"})
    unlimited = ClaudeCodeHarness(
        settings={"max_turns": "unlimited", "max_budget_usd": "unlimited"}
    )
    assert "--max-turns" not in default._flags()
    assert "--max-budget-usd" not in default._flags()
    assert "--max-turns" not in unlimited._flags()
    assert "--max-budget-usd" not in unlimited._flags()


def test_concrete_settings_map_to_native_flags() -> None:
    harness = ClaudeCodeHarness(
        settings={
            "max_turns": 12,
            "max_budget_usd": 3.5,
            "permission_mode": "manual",
            "allowed_tools": ["Bash(git status:*)"],
            "disallowed_tools": ["WebFetch"],
            "append_system_prompt": "Use the task files.",
            "effort": "high",
        }
    )
    flags = harness._flags()
    assert "--max-turns 12" in flags
    assert "--max-budget-usd 3.5" in flags
    assert "--permission-mode default" in flags
    assert "--allowedTools 'Bash(git status:*)'" in flags
    assert "--disallowedTools WebFetch" in flags
    assert "--append-system-prompt 'Use the task files.'" in flags
    assert "--effort high" in flags


@pytest.mark.parametrize("value", [0, -1, False])
def test_invalid_limit_values_fail(value: object) -> None:
    with pytest.raises(ConfigError):
        ClaudeCodeHarness(settings={"max_turns": value})


def test_gateway_and_native_limit_exits_are_distinct() -> None:
    gateway = ClaudeCodeHarness()._classify(
        '{"type":"ale_limit_reached","ale":{"limit":"max_total_tokens","value":400000}}',
        1,
    )
    native = ClaudeCodeHarness(settings={"max_turns": 12})._classify(
        "Error: reached max turns",
        1,
    )

    assert isinstance(gateway, BudgetExceededError)
    assert gateway.layer == "gateway"
    assert gateway.limit == "max_total_tokens"
    assert gateway.value == 400000
    assert isinstance(native, HarnessLimitError)
    assert native.layer == "harness"
    assert native.limit == "max_turns"


class FakeSandbox:
    def __init__(self, sandbox_id: str = "sandbox") -> None:
        self.sandbox_id = sandbox_id
        self.commands: list[str] = []
        self.prompts: list[str] = []
        self.transcript = b""
        self.has_state = True
        self.environments: list[dict[str, str]] = []

    async def exec(self, argv, *, env=None, **kwargs):  # type: ignore[no-untyped-def]
        command = " ".join(str(part) for part in argv)
        self.commands.append(command)
        self.environments.append(env or {})
        if "find " in command:
            return ExecResult(exit_code=0 if self.has_state else 1)
        if "claude " not in command:
            return ExecResult(exit_code=0)
        prompt = next(value for key, value in (env or {}).items() if key.startswith("ALE_PROMPT_"))
        self.prompts.append(prompt)
        match = re.search(r"--(?:session-id|resume) ([^ ]+)", command)
        assert match
        session_id = match.group(1).strip("'")
        line = (
            json.dumps({"type": "result", "session_id": session_id, "result": prompt}).encode()
            + b"\n"
        )
        self.transcript = self.transcript + line if " >> " in command else line
        return ExecResult(exit_code=0)

    async def read_file(self, path):  # type: ignore[no-untyped-def]
        return self.transcript


def native_session(**updates: object) -> HarnessSession:
    values = {
        "episode_id": "episode",
        "gateway_url": "http://gateway",
        "token": "token",
        "model": "claude-opus-4-8",
        "sandbox_id": "sandbox",
        "resources_digest": "sha256:" + "a" * 64,
        "home": "/home/agent",
    }
    values.update(updates)
    return HarnessSession(**values)


@pytest.mark.asyncio
async def test_subscription_launch_uses_oauth_and_drops_api_credentials() -> None:
    harness = ClaudeCodeHarness()
    sandbox = FakeSandbox()
    current = native_session(
        authentication="subscription",
        subscription_credential=b"oauth-token",
        gateway_url="",
    )

    await harness.launch("test", sandbox, current, timeout_sec=10)  # type: ignore[arg-type]

    launch_env = sandbox.environments[-1]
    assert launch_env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert launch_env["CLAUDE_CODE_PROXY_RESOLVES_HOSTS"] == "1"
    assert launch_env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert launch_env["NODE_USE_ENV_PROXY"] == "1"
    assert "ANTHROPIC_BASE_URL" not in launch_env
    assert "ANTHROPIC_API_KEY" not in launch_env
    assert "ANTHROPIC_AUTH_TOKEN" not in launch_env
    for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        assert name in sandbox.commands[-1]
    assert "--bare" not in sandbox.commands[-1]


@pytest.mark.asyncio
async def test_subscription_relay_uses_episode_token() -> None:
    sandbox = FakeSandbox()
    current = native_session(
        authentication="subscription",
        subscription_credential=b"oauth-token",
    )

    await ClaudeCodeHarness().launch(  # type: ignore[arg-type]
        "test", sandbox, current, timeout_sec=10
    )

    launch_env = sandbox.environments[-1]
    assert launch_env["ANTHROPIC_AUTH_TOKEN"] == "token"
    assert launch_env["ANTHROPIC_BASE_URL"] == "http://gateway"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in launch_env
    assert "unset ANTHROPIC_API_KEY CLAUDE_CODE_USE_BEDROCK" in sandbox.commands[-1]


@pytest.mark.asyncio
async def test_native_resume_uses_exact_id_and_only_new_input() -> None:
    harness = ClaudeCodeHarness()
    sandbox = FakeSandbox()
    current = native_session()

    first = await harness.launch("first", sandbox, current, timeout_sec=10)  # type: ignore[arg-type]
    assert first.continuation is not None
    second = await harness.resume(
        "second",
        first.continuation,
        sandbox,  # type: ignore[arg-type]
        current,
        timeout_sec=10,
    )

    native_id = first.continuation.native_session_id
    assert any(f"--session-id {native_id}" in command for command in sandbox.commands)
    assert any(f"--resume {native_id}" in command for command in sandbox.commands)
    assert all("--continue" not in command for command in sandbox.commands)
    assert sandbox.prompts == ["first", "second"]
    assert second.continuation == first.continuation


@pytest.mark.asyncio
async def test_native_resume_rejects_drift_other_sandbox_and_missing_state() -> None:
    harness = ClaudeCodeHarness()
    sandbox = FakeSandbox()
    current = native_session()
    first = await harness.launch("first", sandbox, current, timeout_sec=10)  # type: ignore[arg-type]
    assert first.continuation is not None

    with pytest.raises(NativeContinuationError, match="original live sandbox"):
        await harness.resume(
            "next",
            first.continuation,
            sandbox,  # type: ignore[arg-type]
            native_session(sandbox_id="other"),
            timeout_sec=10,
        )
    with pytest.raises(NativeContinuationError, match="changed"):
        await ClaudeCodeHarness(settings={"effort": "high"}).resume(
            "next",
            first.continuation,
            sandbox,  # type: ignore[arg-type]
            current,
            timeout_sec=10,
        )
    sandbox.has_state = False
    with pytest.raises(NativeContinuationError, match="absent"):
        await harness.resume(
            "next",
            first.continuation,
            sandbox,  # type: ignore[arg-type]
            current,
            timeout_sec=10,
        )


@pytest.mark.asyncio
async def test_unsupported_native_resume_is_explicit() -> None:
    harness = ClaudeCodeHarness()
    first = await harness.launch(
        "first",
        FakeSandbox(),  # type: ignore[arg-type]
        native_session(),
        timeout_sec=10,
    )
    assert first.continuation is not None
    with pytest.raises(AgentUnsupportedError):
        await NopHarness().resume(
            "next",
            first.continuation,
            FakeSandbox(),  # type: ignore[arg-type]
            native_session(),
            timeout_sec=10,
        )


def parse_context(tmp_path: Path) -> TrajectoryParseContext:
    return TrajectoryParseContext(
        episode_id="episode",
        trajectory_id="trajectory",
        instruction="Do the task.",
        logs_dir=tmp_path,
        model="claude-opus-4-8",
        agent_version="2.1.220",
        blobs=BlobStore(tmp_path),
    )


def test_stream_json_parser_preserves_atif_tool_grouping_and_parse_issues(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "working"},
                                {
                                    "type": "tool_use",
                                    "id": "tool-bash-1",
                                    "name": "Bash",
                                    "input": {"command": "printf '%s' hello"},
                                },
                                {
                                    "type": "tool_use",
                                    "id": "tool-mcp-1",
                                    "name": "mcp__docs__search",
                                    "input": {"query": "ALE"},
                                },
                                {
                                    "type": "tool_use",
                                    "id": "tool-mcp-2",
                                    "name": "mcp__cua-desktop__click",
                                    "input": {"coordinate": [500, 500]},
                                },
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool-bash-1",
                                    "content": "hello",
                                },
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool-mcp-1",
                                    "content": '{"matches": 3}',
                                },
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool-mcp-2",
                                    "content": "clicked",
                                }
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "session_id": "session-1",
                        "result": "done",
                    }
                ),
                '{"type":"assistant"',
            ]
        )
        + "\n"
    )
    harness = ClaudeCodeHarness()

    first = harness.parse_trajectory(parse_context(tmp_path))
    second = harness.parse_trajectory(parse_context(tmp_path))

    assert first == second
    agent = first.steps[1]
    assert [call.function_name for call in agent.tool_calls or ()] == [
        "Bash",
        "mcp__docs__search",
        "mcp__cua-desktop__click",
    ]
    assert [result.source_call_id for result in agent.observation.results] == [
        "tool-bash-1",
        "tool-mcp-1",
        "tool-mcp-2",
    ]
    cua = agent.tool_calls[2]
    assert cua.extra["ale"]["mcp"] == {"server": "cua-desktop", "tool": "click"}
    assert cua.extra["ale"]["normalized_action"]["type"] == "click"
    assert first.session_id == "session-1"
    assert first.extra["ale"]["parse_issues"][0]["reason"] == "malformed_json"


def test_stream_json_parser_keeps_missing_result_absent(tmp_path: Path) -> None:
    (tmp_path / "transcript.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "missing-result",
                            "name": "mcp__docs__search",
                            "input": {"query": "ALE"},
                        }
                    ]
                },
            }
        )
        + "\n"
    )

    trajectory = ClaudeCodeHarness().parse_trajectory(parse_context(tmp_path))
    assert trajectory.steps[1].tool_calls[0].tool_call_id == "missing-result"
    assert trajectory.steps[1].observation is None


def test_stream_json_parser_rejects_orphan_results(tmp_path: Path) -> None:
    (tmp_path / "transcript.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "unknown-result",
                            "content": "orphan",
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    with pytest.raises(TrajectoryReferenceError, match="unknown calls"):
        ClaudeCodeHarness().parse_trajectory(parse_context(tmp_path))


def test_stream_json_parser_externalizes_document_and_binary_results(
    tmp_path: Path,
) -> None:
    document = base64.b64encode(b"%PDF-demo").decode()
    binary = base64.b64encode(b"\x00\x01\x02").decode()
    (tmp_path / "transcript.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "attachments",
                                    "name": "mcp__files__read",
                                    "input": {},
                                }
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "attachments",
                                    "content": [
                                        {
                                            "type": "document",
                                            "source": {
                                                "media_type": "application/pdf",
                                                "data": document,
                                            },
                                        },
                                        {
                                            "type": "resource",
                                            "resource": {
                                                "mimeType": "application/octet-stream",
                                                "blob": binary,
                                            },
                                        },
                                    ],
                                }
                            ]
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    parsed = ClaudeCodeHarness().parse_trajectory(parse_context(tmp_path))
    result = parsed.steps[1].observation.results[0]
    refs = result.extra["ale"]["attachments"]
    assert [ref["media_type"] for ref in refs] == [
        "application/pdf",
        "application/octet-stream",
    ]
    assert all((tmp_path / ref["path"]).is_file() for ref in refs)
    assert document not in parsed.model_dump_json()
    assert binary not in parsed.model_dump_json()
