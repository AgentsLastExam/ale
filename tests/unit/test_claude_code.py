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
from ale.core.sandbox import ExecResult, SandboxFile
from ale.run.harnesses.base import STDOUT_PATH
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
    default = ClaudeCodeHarness(settings={"max_turns": "default"})
    unlimited = ClaudeCodeHarness(settings={"max_turns": "unlimited"})
    assert "--max-turns" not in default._flags(native_session())
    assert "--max-turns" not in unlimited._flags(native_session())


def test_concrete_settings_map_to_native_flags() -> None:
    harness = ClaudeCodeHarness(
        settings={
            "max_turns": 12,
            "permission_mode": "manual",
            "allowed_tools": ["Bash(git status:*)"],
            "disallowed_tools": ["WebFetch"],
            "append_system_prompt": "Use the task files.",
            "effort": "high",
        }
    )
    flags = harness._flags(native_session(provider_web_tools=True))
    assert "--max-turns 12" in flags
    assert "--permission-mode default" in flags
    assert "--allowedTools 'Bash(git status:*)'" in flags
    assert "--disallowedTools WebFetch --append-system-prompt" in flags
    assert "--append-system-prompt 'Use the task files.'" in flags
    assert "--effort high" in flags


def test_web_search_is_disallowed_unless_the_task_network_is_open() -> None:
    """WebSearch runs Anthropic's server tool, outside the Sandbox's network policy."""
    assert "--disallowedTools WebSearch" in ClaudeCodeHarness()._flags(native_session())
    assert "--disallowedTools" not in ClaudeCodeHarness()._flags(
        native_session(provider_web_tools=True)
    )
    harness = ClaudeCodeHarness(settings={"disallowed_tools": ["WebSearch", "WebFetch"]})
    assert harness._flags(native_session()).split().count("WebSearch") == 1


@pytest.mark.parametrize("value", [0, -1, False])
def test_invalid_limit_values_fail(value: object) -> None:
    with pytest.raises(ConfigError):
        ClaudeCodeHarness(settings={"max_turns": value})


def test_native_limits_are_classified_and_gateway_refusals_left_to_the_gateway() -> None:
    gateway = ClaudeCodeHarness().classify_failure(
        '{"type":"ale_limit_reached","ale":{"limit":"max_total_tokens","value":400000}}'
    )
    native = ClaudeCodeHarness(settings={"max_turns": 12}).classify_failure(
        "Error: reached max turns"
    )

    assert not isinstance(gateway, BudgetExceededError)
    assert isinstance(native, HarnessLimitError)
    assert native.layer == "harness"
    assert native.limit == "max_turns"
    structured = ClaudeCodeHarness(settings={"max_turns": 12}).classify_failure("error_max_turns")
    assert isinstance(structured, HarnessLimitError)


class FakeSandbox:
    """Claude as far as the base can see: it echoes its prompt and keeps its session file."""

    def __init__(self, sandbox_id: str = "sandbox") -> None:
        self.sandbox_id = sandbox_id
        self.commands: list[str] = []
        self.prompts: list[str] = []
        self.files: dict[str, bytes] = {}
        self.has_state = True
        self.environments: list[dict[str, str]] = []

    async def exec(self, argv, *, env=None, **kwargs):  # type: ignore[no-untyped-def]
        command = " ".join(str(part) for part in argv)
        self.commands.append(command)
        self.environments.append(env or {})
        if argv[-1] == 'printf %s "$PATH"':
            return ExecResult(exit_code=0, stdout="/usr/bin:/bin")
        if "2>> " not in command:
            for path in re.findall(r": > (\S+)", command):
                self.files[path] = b""
            return ExecResult(exit_code=0)
        prompt = self.files["/home/agent/.ale-harness/prompt.md"].decode()
        self.prompts.append(prompt)
        match = re.search(r"--(?:session-id|resume) ([^ ]+)", command)
        assert match
        session_id = match.group(1).strip("'")
        self.files[f"/home/agent/.claude-config/projects/home/{session_id}.jsonl"] = b"{}\n"
        line = (
            json.dumps({"type": "result", "session_id": session_id, "result": prompt}).encode()
            + b"\n"
        )
        self.files["/home/agent/.ale-harness/stdout.log"] += line
        return ExecResult(exit_code=0)

    async def read_file(self, path, *, offset=0, length=None):
        data = self.files[str(path)]
        return data[offset:] if length is None else data[offset : offset + length]

    async def write_file(self, path, data, **kwargs):
        self.files[str(path)] = data

    async def list_files(self, path):
        return tuple(
            SandboxFile(path=name, size=len(data))
            for name, data in sorted(self.files.items())
            if name.startswith(str(path) + "/") and (self.has_state or "/projects/" not in name)
        )


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
async def test_subscription_relay_uses_episode_token() -> None:
    sandbox = FakeSandbox()
    current = native_session(authentication="subscription")

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


def stdout_log(tmp_path: Path) -> Path:
    path = tmp_path / STDOUT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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
    transcript = stdout_log(tmp_path)
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
    stdout_log(tmp_path).write_text(
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
    stdout_log(tmp_path).write_text(
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
    stdout_log(tmp_path).write_text(
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


def test_native_root_and_linked_child_are_collected_without_duplicate_stream_messages(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    native_root = tmp_path / ".claude-config/projects/project"
    native_root.mkdir(parents=True)
    root_events = [
        {
            "type": "assistant",
            "sessionId": "root",
            "message": {
                "id": "root-message",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "delegate",
                        "name": "Task",
                        "input": {"prompt": "child task"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "sessionId": "root",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "delegate", "content": "child done"}
                ],
            },
        },
    ]
    serialized = "".join(json.dumps(event) + "\n" for event in root_events)
    (native_root / "root.jsonl").write_text(serialized)
    stdout_log(tmp_path).write_text(serialized)
    (native_root / "unrelated.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": "ignore me"}})
    )
    children = native_root / "root/subagents"
    children.mkdir(parents=True)
    (children / "agent-child.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "child-session",
                "isSidechain": True,
                "agentId": "child",
                "message": {
                    "id": "child-message",
                    "content": [{"type": "text", "text": "child done"}],
                },
            }
        )
        + "\n"
    )
    (children / "agent-child.meta.json").write_text(json.dumps({"toolUseId": "delegate"}))
    trajectory = ClaudeCodeHarness().parse_trajectory(
        replace(parse_context(tmp_path), session_id="root")
    )
    calls = [call for step in trajectory.steps for call in step.tool_calls or []]
    assert [call.tool_call_id for call in calls] == ["delegate"]
    assert len(trajectory.subagent_trajectories) == 1
    assert trajectory.subagent_trajectories[0].steps[-1].message == "child done"
    assert "ignore me" not in trajectory.model_dump_json()


@pytest.mark.parametrize("native", [True, False])
def test_stdout_subagent_events_belong_to_the_subagent(tmp_path: Path, native: bool) -> None:
    """Stream-json marks a subagent's messages with ``parent_tool_use_id`` only."""
    from dataclasses import replace

    delegate = {
        "id": "root-message",
        "content": [
            {"type": "tool_use", "id": "delegate", "name": "Task", "input": {"prompt": "child"}}
        ],
    }
    delegated = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "delegate", "content": "child done"}],
    }
    child_call = {
        "id": "child-message",
        "content": [{"type": "tool_use", "id": "child-call", "name": "Bash", "input": {}}],
    }
    child_result = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "child-call", "content": "a b"}],
    }
    if native:
        project = tmp_path / ".claude-config/projects/project"
        (project / "root/subagents").mkdir(parents=True)
        (project / "root.jsonl").write_text(
            "".join(
                json.dumps({"type": kind, "sessionId": "root", "message": message}) + "\n"
                for kind, message in (
                    ("user", {"role": "user", "content": "Do the task."}),
                    ("assistant", delegate),
                    ("user", delegated),
                )
            )
        )
        (project / "root/subagents/agent-child.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "type": kind,
                        "sessionId": "root",
                        "isSidechain": True,
                        "agentId": "child",
                        "message": message,
                    }
                )
                + "\n"
                for kind, message in (("assistant", child_call), ("user", child_result))
            )
        )
        (project / "root/subagents/agent-child.meta.json").write_text(
            json.dumps({"toolUseId": "delegate"})
        )
    stdout_log(tmp_path).write_text(
        "".join(
            json.dumps({"session_id": "root", "parent_tool_use_id": parent, **event}) + "\n"
            for parent, event in (
                (None, {"type": "system", "subtype": "init"}),
                (None, {"type": "assistant", "message": delegate}),
                ("delegate", {"type": "assistant", "message": child_call}),
                ("delegate", {"type": "user", "message": child_result}),
                (None, {"type": "user", "message": delegated}),
                (None, {"type": "result", "result": "done"}),
            )
        )
    )
    trajectory = ClaudeCodeHarness().parse_trajectory(
        replace(parse_context(tmp_path), session_id="root")
    )

    def calls(document) -> list[str]:  # type: ignore[no-untyped-def]
        return [call.tool_call_id for step in document.steps for call in step.tool_calls or []]

    assert calls(trajectory) == ["delegate"]
    assert trajectory.steps[0].message == "Do the task."
    assert [calls(child) for child in trajectory.subagent_trajectories] == [["child-call"]]


@pytest.mark.parametrize("native", [True, False])
def test_tool_result_is_what_the_model_saw(tmp_path: Path, native: bool) -> None:
    """Claude Code's client-side result copy never replaces the model-visible result."""
    from dataclasses import replace

    call = {
        "id": "call-message",
        "content": [
            {
                "type": "tool_use",
                "id": "mcp-call",
                "name": "mcp__task-proof__derive_fragment",
                "input": {"nonce": "N"},
            }
        ],
    }
    visible = '{"fragment":"F"}'
    result = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "mcp-call", "content": visible}],
    }
    if native:
        project = tmp_path / ".claude-config/projects/project"
        project.mkdir(parents=True)
        (project / "root.jsonl").write_text(
            "".join(
                json.dumps({"sessionId": "root", **event}) + "\n"
                for event in (
                    {"type": "user", "message": {"role": "user", "content": "Do the task."}},
                    {"type": "assistant", "message": call},
                    {
                        "type": "user",
                        "message": result,
                        "toolUseResult": visible,
                        "mcpMeta": {"structuredContent": {"fragment": "F"}},
                    },
                )
            )
        )
    stdout_log(tmp_path).write_text(
        "".join(
            json.dumps({"session_id": "root", "parent_tool_use_id": None, **event}) + "\n"
            for event in (
                {"type": "assistant", "message": call},
                {
                    "type": "user",
                    "message": result,
                    "tool_use_result": {
                        "content": visible,
                        "structuredContent": {"fragment": "F"},
                    },
                },
                {"type": "result", "result": "done"},
            )
        )
    )
    trajectory = ClaudeCodeHarness().parse_trajectory(
        replace(parse_context(tmp_path), session_id="root")
    )

    [observation] = [step.observation for step in trajectory.steps if step.observation]
    assert [item.content for item in observation.results] == [visible]


#: The native log of the one subagent of session ``parent-1``, without its suffix.
CHILD_LOG = ".claude-config/projects/project/parent-1/subagents/agent-child-1"


def delegated_run(tmp_path: Path) -> TrajectoryParseContext:
    """Session ``parent-1``, whose ``Agent`` call ``spawn-1`` ran one subagent.

    Only the subagent's ``.meta.json`` names that call, and stdout stops at its last call.
    """
    from dataclasses import replace

    spawn = {
        "id": "parent-message",
        "content": [
            {
                "type": "tool_use",
                "id": "spawn-1",
                "name": "Agent",
                "input": {"prompt": "Write the marker."},
            }
        ],
    }
    spawned = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "spawn-1", "content": "DONE"}],
    }
    child_call = {
        "id": "child-message-1",
        "content": [{"type": "tool_use", "id": "child-call", "name": "Bash", "input": {}}],
    }
    child_result = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "child-call", "content": ""}],
    }
    child_reply = {"id": "child-message-2", "content": [{"type": "text", "text": "DONE"}]}

    def native(*events: tuple[str, dict[str, object]]) -> str:
        return "".join(
            json.dumps({"type": kind, "sessionId": "parent-1", "message": message}) + "\n"
            for kind, message in events
        )

    project = tmp_path / ".claude-config/projects/project"
    (project / "parent-1/subagents").mkdir(parents=True)
    (project / "parent-1.jsonl").write_text(
        native(
            ("user", {"role": "user", "content": "Do the task."}),
            ("assistant", spawn),
            ("user", spawned),
        )
    )
    (tmp_path / f"{CHILD_LOG}.jsonl").write_text(
        native(
            ("user", {"role": "user", "content": "Write the marker."}),
            ("assistant", child_call),
            ("user", child_result),
            ("assistant", child_reply),
        )
    )
    (tmp_path / f"{CHILD_LOG}.meta.json").write_text(json.dumps({"toolUseId": "spawn-1"}))
    stdout_log(tmp_path).write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in (
                {"type": "assistant", "message": spawn},
                {"type": "assistant", "parent_tool_use_id": "spawn-1", "message": child_call},
                {"type": "user", "parent_tool_use_id": "spawn-1", "message": child_result},
                {"type": "user", "message": spawned},
                {"type": "result", "result": "Delegated.", "total_cost_usd": 0.01},
            )
        )
    )
    return replace(parse_context(tmp_path), session_id="parent-1")


def test_native_subagent_is_linked_through_its_meta_file(tmp_path: Path) -> None:
    """2.1.227 names the spawning call in ``agent-<id>.meta.json``, not on the events."""
    trajectory = ClaudeCodeHarness().parse_trajectory(delegated_run(tmp_path))
    (subagent,) = trajectory.subagent_trajectories
    refs = {
        result.source_call_id: [ref.trajectory_id for ref in result.subagent_trajectory_ref]
        for step in trajectory.steps
        for result in (step.observation.results if step.observation else ())
        if result.subagent_trajectory_ref
    }

    assert subagent.trajectory_id == "trajectory-subagent-child-1"
    assert refs == {"spawn-1": [subagent.trajectory_id]}
    first, write, final = subagent.steps
    assert (first.source, first.message) == ("user", "Write the marker.")
    assert [call.function_name for call in write.tool_calls] == ["Bash"]
    # Only the native record has the child's reply; stdout stops at its last call.
    assert final.message == "DONE"
    assert "parse_issues" not in (trajectory.extra or {}).get("ale", {})
    # The native result reports a price; ALE reports tokens only.
    assert "cost" not in trajectory.model_dump_json()


def test_native_subagent_without_its_meta_file_stays_reported(tmp_path: Path) -> None:
    parse = delegated_run(tmp_path)
    (tmp_path / f"{CHILD_LOG}.meta.json").unlink()

    trajectory = ClaudeCodeHarness().parse_trajectory(parse)

    assert trajectory.extra["ale"]["parse_issues"] == [
        {"reason": "unlinked_subagent", "source": f"{CHILD_LOG}.jsonl"}
    ]


def test_resumed_turns_stay_in_order_without_a_repeated_final_answer(tmp_path: Path) -> None:
    from dataclasses import replace

    def assistant(message_id: str, text: str) -> dict[str, object]:
        return {
            "type": "assistant",
            "sessionId": "root",
            "message": {"id": message_id, "content": [{"type": "text", "text": text}]},
        }

    def user(text: str) -> dict[str, object]:
        return {"type": "user", "sessionId": "root", "message": {"role": "user", "content": text}}

    native = tmp_path / ".claude-config/projects/project/root.jsonl"
    native.parent.mkdir(parents=True)
    native.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in (
                user("first"),
                assistant("a", "ONE"),
                user("second"),
                assistant("b", "TWO"),
            )
        )
    )
    segments = [
        [
            {"type": "system", "subtype": "init", "session_id": "root"},
            assistant("a", "ONE"),
            {"type": "result", "session_id": "root", "result": "ONE"},
        ],
        [
            {"type": "system", "subtype": "init", "session_id": "root"},
            assistant("b", "TWO"),
            {"type": "result", "session_id": "root", "result": "TWO"},
        ],
    ]
    stdout = [
        "".join(json.dumps(event) + "\n" for event in segment).encode() for segment in segments
    ]
    stdout_log(tmp_path).write_bytes(b"".join(stdout))
    (tmp_path / ".ale-harness/invocations.jsonl").write_text(
        json.dumps({"prompt": "first", "session_id": "root", "stdout_offset": 0})
        + "\n"
        + json.dumps({"prompt": "second", "session_id": "root", "stdout_offset": len(stdout[0])})
        + "\n"
    )
    trajectory = ClaudeCodeHarness().parse_trajectory(
        replace(parse_context(tmp_path), session_id="root")
    )
    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "first"),
        ("agent", "ONE"),
        ("user", "second"),
        ("agent", "TWO"),
    ]


def test_without_a_native_session_each_turn_starts_with_its_recorded_prompt(
    tmp_path: Path,
) -> None:
    first = json.dumps({"type": "result", "session_id": "root", "result": "ONE"}) + "\n"
    second = json.dumps({"type": "result", "session_id": "root", "result": "TWO"}) + "\n"
    stdout_log(tmp_path).write_text(first + second)
    (tmp_path / ".ale-harness/invocations.jsonl").write_text(
        json.dumps({"prompt": "first", "stdout_offset": 0})
        + "\n"
        + json.dumps({"prompt": "second", "stdout_offset": len(first)})
        + "\n"
    )
    trajectory = ClaudeCodeHarness().parse_trajectory(parse_context(tmp_path))
    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "first"),
        ("agent", "ONE"),
        ("user", "second"),
        ("agent", "TWO"),
    ]
    assert trajectory.extra["ale"]["parse_issues"] == [
        {"reason": "native_session_unavailable", "session_id": "root"}
    ]
