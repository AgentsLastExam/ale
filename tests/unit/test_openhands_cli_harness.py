from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path, PurePosixPath

import pytest
from tests.unit.test_cli_harnesses import FakeSandbox, resources, session

from ale.core.config import load_run_config
from ale.core.errors import AgentError, ConfigError
from ale.core.harness import EffectiveAgentResources, TrajectoryParseContext
from ale.core.taskspec import OperatingSystem
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.harnesses.base import INVOCATIONS_PATH, STDOUT_PATH
from ale.run.harnesses.openhands_cli import OpenHandsCliHarness
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit

STATE = "/home/agent/.openhands-ale"
SESSION_ID = "0b7c5f1e6f394f0a9d476a0f0c8f3a11"


def test_preset_pins_the_final_linux_release() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "openhands-cli.toml")
    harness = _harness(config)
    assert isinstance(harness, OpenHandsCliHarness)
    assert harness.gateway_dialect == config.gateway.dialect == "anthropic"
    assert config.gateway.provider == "anthropic"
    assert harness.version() == "1.16.0"
    assert harness.supported_os == (OperatingSystem.LINUX,)
    with pytest.raises(Exception, match="windows"):
        harness.validate_os(OperatingSystem.WINDOWS)


def test_settings_and_other_versions_fail_closed() -> None:
    with pytest.raises(ConfigError):
        OpenHandsCliHarness(settings={"reasoning_effort": "high"})
    with pytest.raises(ConfigError):
        OpenHandsCliHarness(cli_version="1.15.0")
    with pytest.raises(ConfigError, match="dialect"):
        OpenHandsCliHarness(gateway_dialect="gemini")


@pytest.mark.parametrize(
    ("dialect", "model", "llm_model", "base_url"),
    [
        ("anthropic", "claude-opus-4-8", "anthropic/claude-opus-4-8", "http://gateway"),
        ("openai-chat-completions", "o4-mini", "openai/o4-mini", "http://gateway/v1"),
        ("openai-responses", "gpt-5.6-luna", "openai/gpt-5.6-luna", "http://gateway/v1"),
    ],
)
async def test_the_gateway_is_the_model_route_the_sdk_takes(
    dialect: str, model: str, llm_model: str, base_url: str
) -> None:
    command = await OpenHandsCliHarness(gateway_dialect=dialect).prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(model=model),
        native_session_id=None,
    )
    assert command.env["LLM_MODEL"] == llm_model
    assert command.env["LLM_BASE_URL"] == base_url
    assert command.env["LLM_API_KEY"] == "token"
    assert command.env["OPENHANDS_PERSISTENCE_DIR"] == STATE


@pytest.mark.parametrize(
    ("dialect", "model"),
    [("openai-chat-completions", "gpt-5-mini"), ("openai-responses", "o4-mini")],
)
async def test_a_dialect_the_sdk_would_not_use_for_the_model_fails(
    tmp_path: Path, dialect: str, model: str
) -> None:
    with pytest.raises(ConfigError, match="does not match"):
        await OpenHandsCliHarness(gateway_dialect=dialect).install_resources(
            FakeSandbox(),  # type: ignore[arg-type]
            session(model=model),
            EffectiveAgentResources(),
        )


async def test_skills_mcp_and_an_empty_public_skills_cache(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    await OpenHandsCliHarness().install_resources(sandbox, session(), resources(tmp_path))  # type: ignore[arg-type]

    assert sandbox.uploads == [
        (str(tmp_path / "resource-proof"), "/home/agent/.openhands/skills/resource-proof")
    ]
    prepare = sandbox.commands[0][2]
    assert "git -C /home/agent/.openhands/cache/skills/public-skills init -q" in prepare
    assert json.loads(sandbox.files[f"{STATE}/mcp.json"]) == {
        "mcpServers": {
            "task-proof": {
                "args": ["/home/agent/server.py"],
                "command": "python3",
                "env": {"PROOF_HOME": "/home/agent"},
                "transport": "stdio",
            }
        }
    }


@pytest.mark.parametrize("native_session_id", [None, SESSION_ID])
async def test_command_resumes_or_creates_one_exact_conversation(
    native_session_id: str | None,
) -> None:
    command = await OpenHandsCliHarness().prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(model="claude"),
        native_session_id=native_session_id,
    )
    invocation, stdin = command.script.split(" < ")
    assert stdin == "/dev/null"
    argv = shlex.split(invocation)
    assert argv[:5] == ["openhands", "--headless", "--json", "--override-with-envs", "--resume"]
    assert argv[5] == command.session_id and len(argv[5]) == 32
    assert argv[6:] == ["--file", "/home/agent/.ale-harness/prompt.md"]
    assert native_session_id is None or command.session_id == native_session_id


def lines(*items: object) -> str:
    return "".join((item if isinstance(item, str) else json.dumps(item)) + "\n" for item in items)


def test_result_reads_events_among_progress_text() -> None:
    harness = OpenHandsCliHarness()
    done = harness.read_result(
        lines(
            "Initializing agent...",
            {
                "kind": "MessageEvent",
                "source": "agent",
                "llm_message": {"content": [{"type": "text", "text": "done"}]},
            },
            "Agent finished",
            f"Conversation ID: {SESSION_ID}",
        )
    )
    assert (done.session_id, done.final_message, done.error) == (SESSION_ID, "done", None)
    failed = harness.read_result(
        lines(
            {"kind": "ConversationErrorEvent", "code": "LLMServiceUnavailableError", "detail": "x"},
            f"Conversation ID: {SESSION_ID}",
        )
    )
    assert failed.error == "LLMServiceUnavailableError: x"


async def test_exit_zero_with_a_conversation_error_fails() -> None:
    sandbox = FakeSandbox()
    sandbox.native_stdout = lines(
        {"kind": "ConversationErrorEvent", "code": "LLMServiceUnavailableError", "detail": "down"},
        f"Conversation ID: {SESSION_ID}",
    ).encode()
    with pytest.raises(AgentError, match="down"):
        await OpenHandsCliHarness().launch("go", sandbox, session(model="claude"), timeout_sec=60)  # type: ignore[arg-type]


def test_session_files_are_the_conversations_events_and_state() -> None:
    harness = OpenHandsCliHarness()
    root = PurePosixPath(".openhands-ale/conversations", SESSION_ID)
    assert harness.is_session_file(root / "base_state.json", SESSION_ID)
    assert harness.is_session_file(root / "events/event-00001-x.json", SESSION_ID)
    assert not harness.is_session_file(root / "events/.eventlog.lock", SESSION_ID)
    assert not harness.is_session_file(root / "subagents/a/base_state.json", SESSION_ID)


def recorded_context(root: Path, **updates: object) -> TrajectoryParseContext:
    first = json.loads((root / INVOCATIONS_PATH).read_text().splitlines()[0])
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "openhands-trajectory",
        "instruction": first["prompt"],
        "logs_dir": root,
        "model": "scripted-model",
        "agent_version": "1.16.0",
        "blobs": BlobStore(root),
        "session_id": first["session_id"],
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def write_events(root: Path, *events: dict[str, object], below: str = "") -> None:
    directory = root / ".openhands-ale/conversations" / SESSION_ID / below / "events"
    directory.mkdir(parents=True, exist_ok=True)
    for index, event in enumerate(events):
        (directory / f"event-{index:05d}-e{index}.json").write_text(json.dumps(event))


def record(root: Path, *prompts: str, stdout: list[str] | None = None) -> None:
    """Each invocation's prompt and, when given, its own stdout."""
    parts = stdout or [""] * len(prompts)
    offsets = [sum(len(part.encode()) for part in parts[:index]) for index in range(len(parts))]
    (root / STDOUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / STDOUT_PATH).write_text("".join(parts))
    (root / INVOCATIONS_PATH).write_text(
        "".join(
            json.dumps({"prompt": prompt, "session_id": SESSION_ID, "stdout_offset": offset}) + "\n"
            for prompt, offset in zip(prompts, offsets, strict=True)
        )
    )


def text(source: str, value: str, **fields: object) -> dict[str, object]:
    return {
        "kind": "MessageEvent",
        "source": source,
        "llm_message": {"content": [{"type": "text", "text": value}]},
        **fields,
    }


def framed(prompt: str) -> str:
    return (
        "Starting this session with file context.\n\nFile path: /home/agent/.ale-harness/"
        f"prompt.md\n\nFile contents:\n{'-' * 20}\n{prompt}\n{'-' * 20}\n"
    )


def terminal(call_id: str, response: str, at: str) -> list[dict[str, object]]:
    """A ``terminal`` call and its observation."""
    return [
        {
            "kind": "ActionEvent",
            "timestamp": at,
            "llm_response_id": response,
            "tool_name": "terminal",
            "tool_call_id": call_id,
            "tool_call": {"id": call_id, "name": "terminal", "arguments": '{"command": "ls"}'},
        },
        {
            "kind": "ObservationEvent",
            "timestamp": at,
            "tool_call_id": call_id,
            "observation": {
                "kind": "TerminalObservation",
                "content": [{"type": "text", "text": "ok"}],
            },
        },
    ]


def test_mcp_names_and_errors_convert(tmp_path: Path) -> None:
    record(tmp_path, "Use the server.")
    (tmp_path / ".openhands-ale/mcp.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".openhands-ale/mcp.json").write_text(
        json.dumps({"mcpServers": {"task-proof": {}, "other": {}}})
    )
    write_events(
        tmp_path,
        {"kind": "SystemPromptEvent", "source": "agent"},
        text("user", framed("Use the server.")),
        {
            "kind": "ActionEvent",
            "llm_response_id": "r1",
            "tool_name": "task-proof_derive",
            "tool_call_id": "c1",
            "tool_call": {"id": "c1", "name": "task-proof_derive", "arguments": "{}"},
            "thought": [{"type": "text", "text": "plan"}],
            "action": {"kind": "MCPToolAction", "data": {}},
        },
        {"kind": "AgentErrorEvent", "tool_call_id": "c1", "error": "bad arguments"},
        {"kind": "ConversationErrorEvent", "code": "LLMServiceUnavailableError", "detail": "x"},
    )
    trajectory = OpenHandsCliHarness().parse_trajectory(recorded_context(tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Use the server."),
        ("agent", ""),
        ("system", "LLMServiceUnavailableError: x"),
    ]
    agent = trajectory.steps[1]
    assert agent.reasoning_content == "plan"
    (call,) = agent.tool_calls or ()
    assert call.extra == {"ale": {"mcp": {"server": "task-proof", "tool": "derive"}}}
    (failed,) = agent.observation.results  # type: ignore[union-attr]
    assert failed.content == "bad arguments" and failed.extra == {"ale": {"is_error": True}}
    assert not trajectory.extra


def task_call(call_id: str, response: str, at: str, prompt: str) -> dict[str, object]:
    return {
        "kind": "ActionEvent",
        "timestamp": at,
        "llm_response_id": response,
        "tool_name": "task",
        "tool_call_id": call_id,
        "tool_call": {"id": call_id, "name": "task", "arguments": json.dumps({"prompt": prompt})},
        "action": {"kind": "TaskAction", "prompt": prompt},
    }


def task_result(call_id: str, task_id: str, at: str) -> dict[str, object]:
    return {
        "kind": "ObservationEvent",
        "timestamp": at,
        "tool_call_id": call_id,
        "observation": {
            "kind": "TaskObservation",
            "task_id": task_id,
            "content": [{"type": "text", "text": "done"}],
        },
    }


def state(below: str, tasks: dict[str, list[str]], root: Path) -> None:
    path = root / ".openhands-ale/conversations" / SESSION_ID / below / "base_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    usage = {
        f"task:{task}": {"token_usages": [{"response_id": r} for r in responses]}
        for task, responses in tasks.items()
    }
    path.write_text(json.dumps({"stats": {"usage_to_metrics": usage}}))


def test_a_delegation_links_through_the_tasks_metrics(tmp_path: Path) -> None:
    record(tmp_path, "Delegate.")
    write_events(
        tmp_path,
        text("user", framed("Delegate."), timestamp="2026-10-26T10:00:00"),
        *terminal("c1", "p1", "2026-10-26T10:00:01"),
        task_call("c2", "p2", "2026-10-26T10:00:02", "Child."),
        task_result("c2", "task_00000001", "2026-10-26T10:00:10"),
    )
    state("", {"task_00000001": ["a1"]}, tmp_path)
    write_events(
        tmp_path,
        text("user", "Child.", timestamp="2026-10-26T10:00:03"),
        *terminal("cc", "a1", "2026-10-26T10:00:04"),
        below="subagents/child",
    )
    trajectory = OpenHandsCliHarness().parse_trajectory(recorded_context(tmp_path))

    assert not trajectory.extra
    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Delegate."),
        ("agent", ""),
        ("agent", ""),
    ]
    command, task = trajectory.steps[1:]
    (output,) = command.observation.results  # type: ignore[union-attr]
    assert (output.content, output.subagent_trajectory_ref) == ("ok", None)
    (child,) = trajectory.subagent_trajectories or ()
    assert child.session_id == "child"
    assert child.trajectory_id == "openhands-trajectory-subagent-child"
    (result,) = task.observation.results  # type: ignore[union-attr]
    assert [ref.trajectory_id for ref in result.subagent_trajectory_ref or ()] == [
        child.trajectory_id
    ]
    assert [(step.source, step.message) for step in child.steps] == [
        ("user", "Child."),
        ("agent", ""),
    ]
    assert child.steps[1].tool_calls[0].tool_call_id == "cc"  # type: ignore[index]


def test_nested_delegations_link_by_metrics_inside_each_call(tmp_path: Path) -> None:
    record(tmp_path, "Delegate.")
    write_events(
        tmp_path,
        text("user", framed("Delegate."), timestamp="2026-10-26T10:00:00"),
        task_call("c1", "p1", "2026-10-26T10:00:01", "Child."),
        task_result("c1", "task_00000001", "2026-10-26T10:00:10"),
        # A second task whose ID collides with a later process's task: no child events
        # fall inside this call, so its metrics claim nothing.
        task_call("c2", "p2", "2026-10-26T10:00:11", "Stale."),
        task_result("c2", "task_00000002", "2026-10-26T10:00:12"),
        # A task whose metrics were never written.
        task_call("c3", "p3", "2026-10-26T10:00:13", "Killed."),
        task_result("c3", "task_00000003", "2026-10-26T10:00:20"),
    )
    state("", {"task_00000001": ["a1", "a2", "g1"], "task_00000002": ["late"]}, tmp_path)
    write_events(
        tmp_path,
        text("user", "Child.", timestamp="2026-10-26T10:00:02", llm_response_id=None),
        task_call("cc", "a1", "2026-10-26T10:00:03", "Grandchild."),
        task_result("cc", "task_00000001", "2026-10-26T10:00:08"),
        text("agent", "Child done.", timestamp="2026-10-26T10:00:09", llm_response_id="a2"),
        below="subagents/child",
    )
    state("subagents/child", {"task_00000001": ["g1"]}, tmp_path)
    write_events(
        tmp_path,
        text("user", "Grandchild.", timestamp="2026-10-26T10:00:04"),
        text("agent", "Grandchild done.", timestamp="2026-10-26T10:00:05", llm_response_id="g1"),
        below="subagents/child/subagents/grandchild",
    )
    write_events(
        tmp_path,
        text("agent", "Late.", timestamp="2026-10-26T11:00:00", llm_response_id="late"),
        below="subagents/late",
    )
    write_events(
        tmp_path,
        text("agent", "Killed.", timestamp="2026-10-26T10:00:14", llm_response_id="k1"),
        below="subagents/killed",
    )
    trajectory = OpenHandsCliHarness().parse_trajectory(recorded_context(tmp_path))

    (child,) = trajectory.subagent_trajectories or ()
    assert child.trajectory_id == "openhands-trajectory-subagent-child"
    (grandchild,) = child.subagent_trajectories or ()
    assert grandchild.trajectory_id == f"{child.trajectory_id}-subagent-grandchild"
    assert [step.message for step in grandchild.steps] == ["Grandchild.", "Grandchild done."]
    nested = next(step for step in child.steps if step.tool_calls)
    assert nested.observation.results[0].subagent_trajectory_ref[0].trajectory_id == (  # type: ignore[union-attr, index]
        grandchild.trajectory_id
    )
    stale, killed = (
        step.observation.results[0]  # type: ignore[union-attr]
        for step in trajectory.steps
        if step.tool_calls and step.tool_calls[0].tool_call_id in {"c2", "c3"}
    )
    assert stale.subagent_trajectory_ref is None and killed.subagent_trajectory_ref is None
    root = f".openhands-ale/conversations/{SESSION_ID}/subagents"
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "unlinked_subagent", "source": f"{root}/killed"},
        {"reason": "unlinked_subagent", "source": f"{root}/late"},
    ]


RESUMED_PROMPTS = ["Write the file.", "Write it again."]
RESUMED_STEPS = [
    step
    for prompt in RESUMED_PROMPTS
    for step in [("user", prompt), ("agent", ""), ("agent", "Done.")]
]


def write_resumed_run(root: Path) -> None:
    """A launch and one resume, each running one command: the conversation and each
    turn's JSON events."""
    events: list[dict[str, object]] = []
    streams: list[str] = []
    for turn, prompt in enumerate(RESUMED_PROMPTS, 1):
        at = f"2026-10-26T10:00:0{turn}"
        own = [
            text("user", framed(prompt), timestamp=at),
            *terminal(f"call-{turn}", f"r{turn}", at),
            text("agent", "Done.", timestamp=at),
        ]
        events += own
        streams.append(lines(*own))
    write_events(root, *events)
    record(root, *RESUMED_PROMPTS, stdout=streams)


def test_a_launch_and_its_resume_convert_in_native_order(tmp_path: Path) -> None:
    write_resumed_run(tmp_path)
    trajectory = OpenHandsCliHarness().parse_trajectory(recorded_context(tmp_path))

    assert trajectory.session_id == SESSION_ID
    assert not trajectory.extra
    assert [(step.source, step.message) for step in trajectory.steps] == RESUMED_STEPS
    for turn, step in enumerate(trajectory.steps[1::3], 1):
        (call,) = step.tool_calls or ()
        assert (call.tool_call_id, call.function_name) == (f"call-{turn}", "terminal")
        assert call.arguments == {"command": "ls"}
        (result,) = step.observation.results  # type: ignore[union-attr]
        assert (result.source_call_id, result.content) == (call.tool_call_id, "ok")


def test_without_the_conversation_each_turn_comes_from_its_json_events(tmp_path: Path) -> None:
    write_resumed_run(tmp_path)
    shutil.rmtree(tmp_path / ".openhands-ale/conversations")
    trajectory = OpenHandsCliHarness().parse_trajectory(recorded_context(tmp_path))

    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "native_session_unavailable", "session_id": SESSION_ID}
    ]
    assert [(step.source, step.message) for step in trajectory.steps] == RESUMED_STEPS
