from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale.core.config import load_run_config
from ale.core.errors import ConfigError
from ale.core.harness import (
    EffectiveAgentResources,
    HarnessSession,
    ResolvedMcpServer,
    ResolvedSkill,
    TrajectoryParseContext,
)
from ale.core.sandbox import ExecResult
from ale.core.taskspec import StdioMcpServer
from ale.run.cli.main import PRESET_DIR, _harness
from ale.run.harnesses.codex_cli import CodexCliHarness
from ale.run.harnesses.grok_build import GrokBuildHarness
from ale.run.harnesses.openclaw_cli import OpenClawCliHarness, _supported_node
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit


class FakeSandbox:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.uploads: list[tuple[str, str]] = []
        self.commands: list[list[str]] = []

    async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        self.commands.append([str(part) for part in argv])
        return ExecResult(exit_code=0)

    async def write_file(self, path, data, **kwargs):  # type: ignore[no-untyped-def]
        self.files[str(path)] = data

    async def upload_dir(self, source, target, **kwargs):  # type: ignore[no-untyped-def]
        self.uploads.append((source, str(target)))


def session() -> HarnessSession:
    return HarnessSession(
        episode_id="episode",
        gateway_url="http://gateway",
        token="token",
        model="test-model",
        sandbox_id="sandbox",
        resources_digest="sha256:" + "a" * 64,
        home="/home/agent",
    )


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
    (tmp_path / "session_chat_history.jsonl").write_text(
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
    (tmp_path / "transcript.jsonl").write_text(
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
    assert "session.jsonl" in CodexCliHarness.logs


def test_openclaw_parser_preserves_mcp_call_and_result(tmp_path: Path) -> None:
    (tmp_path / "transcript.jsonl").write_text(
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
