from __future__ import annotations

import json
import shlex
from pathlib import Path, PurePosixPath

import pytest
from tests.unit.test_cli_harnesses import FakeSandbox, resources, session

from orbit.core.config import load_run_config
from orbit.core.errors import AgentError, AgentUnsupportedError, ConfigError
from orbit.core.harness import EffectiveAgentResources, ResolvedMcpServer, TrajectoryParseContext
from orbit.core.taskspec import StdioMcpServer, StreamableHttpMcpServer
from orbit.run.cli.tasks import PRESET_DIR, _harness
from orbit.run.harnesses.base import INVOCATIONS_PATH, STDOUT_PATH
from orbit.run.harnesses.droid import DroidHarness
from orbit.run.recording import BlobStore

pytestmark = pytest.mark.unit

STATE = "/home/agent/.droid-orbit/.factory"
SESSION_ID = "0b7c5f1e-6f39-4f0a-9d47-6a0f0c8f3a11"


def mcp(name: str, server: StdioMcpServer | StreamableHttpMcpServer) -> ResolvedMcpServer:
    return ResolvedMcpServer(
        name=name,
        server=server,
        source_layers=("run",),
        declared_sources=(name,),
        digest="sha256:" + "c" * 64,
        reportable=True,
    )


def test_preset_is_complete_and_follows_the_gateway_dialect() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "droid.toml")
    harness = _harness(config)
    assert isinstance(harness, DroidHarness)
    assert harness.gateway_dialect == config.gateway.dialect == "anthropic"
    assert config.gateway.provider == "anthropic"
    assert harness.version() == "0.249.0"
    assert harness.integrity() == "npm:droid@0.249.0"
    chat = _harness(
        load_run_config(
            preset_path=PRESET_DIR / "droid.toml",
            overrides=['gateway.dialect="openai-chat-completions"'],
        )
    )
    assert isinstance(chat, DroidHarness) and chat.gateway_dialect == "openai-chat-completions"


@pytest.mark.parametrize(
    "settings",
    [
        {"max_output_token": 1},
        {"max_output_tokens": 0},
        {"max_output_tokens": -1},
        {"max_output_tokens": False},
        {"max_output_tokens": "unlimited"},
        {"disabled_tools": [""]},
        {"disabled_tools": ["Read,Edit"]},
        {"no_image_support": "no"},
        {"no_image_support": 0},
    ],
)
def test_settings_are_strict(settings: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        DroidHarness(settings=settings)


def test_unknown_dialect_fails() -> None:
    with pytest.raises(ConfigError, match="dialect"):
        DroidHarness(gateway_dialect="gemini")


@pytest.mark.parametrize(
    ("dialect", "provider", "base_url"),
    [
        ("anthropic", "anthropic", "http://gateway"),
        ("openai-chat-completions", "generic-chat-completion-api", "http://gateway/v1"),
        ("openai-responses", "openai", "http://gateway/v1"),
    ],
)
async def test_one_custom_model_is_the_gateway_and_holds_only_a_token_reference(
    tmp_path: Path, dialect: str, provider: str, base_url: str
) -> None:
    sandbox = FakeSandbox()
    harness = DroidHarness(
        gateway_dialect=dialect,
        settings={"max_output_tokens": 64000, "no_image_support": False},
    )
    await harness.install_resources(sandbox, session(), resources(tmp_path))  # type: ignore[arg-type]

    settings = json.loads(sandbox.files[f"{STATE}/settings.json"])
    assert settings == {
        "cloudSessionSync": False,
        "customModels": [
            {
                "apiKey": "${ORBIT_GATEWAY_TOKEN}",
                "baseUrl": base_url,
                "displayName": "Orbit Gateway",
                "maxOutputTokens": 64000,
                "model": "test-model",
                "noImageSupport": False,
                "provider": provider,
            }
        ],
    }
    assert all(b'token"' not in data and b': "token' not in data for data in sandbox.files.values())


async def test_default_output_ceiling_and_image_support_are_left_to_droid(
    tmp_path: Path,
) -> None:
    sandbox = FakeSandbox()
    await DroidHarness().install_resources(sandbox, session(), EffectiveAgentResources())  # type: ignore[arg-type]
    settings = json.loads(sandbox.files[f"{STATE}/settings.json"])
    assert "maxOutputTokens" not in settings["customModels"][0]
    assert "noImageSupport" not in settings["customModels"][0]
    assert json.loads(sandbox.files[f"{STATE}/mcp.json"]) == {"mcpServers": {}}


async def test_skills_and_both_mcp_transports_become_native_config(tmp_path: Path) -> None:
    effective = resources(tmp_path)
    effective = effective.model_copy(
        update={
            "mcp_servers": (
                *effective.mcp_servers,
                mcp(
                    "remote",
                    StreamableHttpMcpServer(
                        name="remote", transport="streamable-http", url="https://mcp.test/mcp"
                    ),
                ),
            )
        }
    )
    sandbox = FakeSandbox()
    harness = DroidHarness()
    harness.validate_resources(effective)
    await harness.install_resources(sandbox, session(), effective)  # type: ignore[arg-type]

    assert sandbox.uploads == [(str(tmp_path / "resource-proof"), f"{STATE}/skills/resource-proof")]
    assert json.loads(sandbox.files[f"{STATE}/mcp.json"]) == {
        "mcpServers": {
            "task-proof": {
                "type": "stdio",
                "command": "python3",
                "args": ["/home/agent/server.py"],
                "env": {"PROOF_HOME": "/home/agent"},
            },
            "remote": {"type": "http", "url": "https://mcp.test/mcp", "oauth": False},
        }
    }


def test_stdio_mcp_with_a_working_directory_is_rejected() -> None:
    server = StdioMcpServer(name="local", transport="stdio", command="python3", cwd="/srv")
    with pytest.raises(AgentUnsupportedError, match="working directory"):
        DroidHarness().validate_resources(
            EffectiveAgentResources(mcp_servers=(mcp("local", server),))
        )


@pytest.mark.parametrize("native_session_id", [None, SESSION_ID])
async def test_command_selects_the_session_model_and_removes_account_tools(
    native_session_id: str | None,
) -> None:
    harness = DroidHarness(settings={"disabled_tools": ["WebSearch", "Task"]})
    command = await harness.prepare_command(
        "/home/agent/.orbit-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=native_session_id,
    )
    unset, invocation = command.script.split("; ", 1)
    assert unset == "unset FACTORY_API_KEY"
    argv = shlex.split(invocation)
    assert argv[:2] == ["droid", "exec"]
    options = {flag: argv[argv.index(flag) + 1] for flag in argv if flag.startswith("--")}
    assert options["--output-format"] == "stream-json"
    assert options["--model"] == "custom:Orbit-Gateway-0"
    assert options["--remove-tools"] == "Task,WebSearch"
    assert options["--file"] == "/home/agent/.orbit-harness/prompt.md"
    assert options["--cwd"] == "/home/agent"
    assert options["--session-id"] == command.session_id
    if native_session_id:
        assert command.session_id == native_session_id
    assert "--skip-permissions-unsafe" in argv
    assert command.env["FACTORY_HOME_OVERRIDE"] == "/home/agent/.droid-orbit"
    assert command.env["ORBIT_GATEWAY_TOKEN"] == "token"
    assert command.env["FACTORY_DROID_AUTO_UPDATE_ENABLED"] == "false"


def stream(*events: dict[str, object]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def test_result_needs_completion_and_recovered_errors_do_not_fail() -> None:
    harness = DroidHarness()
    failed = harness.read_result(
        stream(
            {"type": "message", "role": "user", "text": "x", "session_id": SESSION_ID},
            {"type": "error", "source": "agent_loop", "message": "Connection error."},
            {"type": "error", "source": "cli", "message": "Connection error."},
        )
    )
    assert failed.session_id == SESSION_ID and failed.error == "Connection error."
    recovered = harness.read_result(
        stream(
            {"type": "error", "source": "agent_loop", "message": "retrying"},
            {"type": "completion", "finalText": "done", "session_id": SESSION_ID},
        )
    )
    assert (recovered.session_id, recovered.final_message, recovered.error) == (
        SESSION_ID,
        "done",
        None,
    )


async def test_failed_exec_keeps_the_native_reason() -> None:
    sandbox = FakeSandbox(exit_code=1)
    sandbox.native_stdout = stream(
        {"type": "error", "source": "cli", "message": "Unable to reach the provider"}
    ).encode()
    with pytest.raises(AgentError, match="Unable to reach the provider") as raised:
        await DroidHarness().launch("go", sandbox, session(), timeout_sec=60)  # type: ignore[arg-type]
    assert "exit code 1" in str(raised.value)


def test_session_files_are_exact_and_one_directory_below_sessions() -> None:
    harness = DroidHarness()
    sessions = PurePosixPath(".droid-orbit/.factory/sessions")
    assert harness.is_session_file(sessions / "-home-user" / f"{SESSION_ID}.jsonl", SESSION_ID)
    assert not harness.is_session_file(sessions / f"{SESSION_ID}.jsonl", SESSION_ID)
    assert not harness.is_session_file(
        sessions / "-home-user" / f"{SESSION_ID}.settings.json", SESSION_ID
    )
    assert not harness.is_session_file(sessions / "-home-user" / f"x{SESSION_ID}.jsonl", SESSION_ID)


def recorded_context(root: Path, **updates: object) -> TrajectoryParseContext:
    first = json.loads((root / INVOCATIONS_PATH).read_text().splitlines()[0])
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "droid-trajectory",
        "instruction": first["prompt"],
        "logs_dir": root,
        "model": "scripted-model",
        "agent_version": "0.249.0",
        "blobs": BlobStore(root),
        "session_id": first["session_id"],
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def write_session(root: Path, session_id: str, *events: dict[str, object]) -> None:
    path = root / ".droid-orbit/.factory/sessions/-home-agent" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stream(*events))


def record(root: Path, *prompts: str, stdout: str | list[str] = "") -> None:
    """Each invocation's prompt; a list gives each invocation its own stdout."""
    parts = stdout if isinstance(stdout, list) else [""] * (len(prompts) - 1) + [stdout]
    offsets = [sum(len(part.encode()) for part in parts[:index]) for index in range(len(parts))]
    (root / STDOUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / STDOUT_PATH).write_text("".join(parts))
    (root / INVOCATIONS_PATH).write_text(
        "".join(
            json.dumps({"prompt": prompt, "session_id": SESSION_ID, "stdout_offset": offset}) + "\n"
            for prompt, offset in zip(prompts, offsets, strict=True)
        )
    )


def message(role: str, *content: dict[str, object], **fields: object) -> dict[str, object]:
    return {
        "type": "message",
        "id": str(fields.pop("id", role)),
        "message": {"role": role, "content": list(content), **fields},
    }


RESUMED_PROMPTS = ["Write the file.", "Write it again."]
RESUMED_STEPS = [
    step
    for prompt in RESUMED_PROMPTS
    for step in [("user", prompt), ("agent", ""), ("agent", "Done.")]
]


def write_resumed_run(root: Path) -> None:
    """A launch and one resume, each running one command: the session and each stream."""
    events: list[dict[str, object]] = []
    streams: list[str] = []
    for turn, prompt in enumerate(RESUMED_PROMPTS, 1):
        call, command = f"call-{turn}", {"command": f"echo {turn}"}
        events += [
            message("user", {"type": "text", "text": "<system-reminder>"}, visibility="llm_only"),
            message("user", {"type": "text", "text": prompt}),
            message(
                "assistant", {"type": "tool_use", "id": call, "name": "Execute", "input": command}
            ),
            message("user", {"type": "tool_result", "tool_use_id": call, "content": str(turn)}),
            message("assistant", {"type": "text", "text": "Done."}),
            {"type": "agent_turn_outcome", "reason": "completed"},
        ]
        streams.append(
            stream(
                {
                    "type": "tool_call",
                    "id": call,
                    "messageId": f"m{turn}",
                    "toolName": "Execute",
                    "parameters": command,
                },
                {"type": "tool_result", "id": call, "value": str(turn)},
                {"type": "message", "role": "assistant", "text": "Done."},
                {"type": "completion", "usage": {"input_tokens": 10 * turn, "output_tokens": 1}},
            )
        )
    write_session(root, SESSION_ID, *events)
    record(root, *RESUMED_PROMPTS, stdout=streams)


def test_a_launch_and_its_resume_convert_in_native_order(tmp_path: Path) -> None:
    write_resumed_run(tmp_path)
    trajectory = DroidHarness().parse_trajectory(recorded_context(tmp_path))

    assert trajectory.session_id == SESSION_ID
    assert not (trajectory.extra or {}).get("orbit")
    assert [(step.source, step.message) for step in trajectory.steps] == RESUMED_STEPS
    for turn, step in enumerate(trajectory.steps[1::3], 1):
        (call,) = step.tool_calls or ()
        assert (call.tool_call_id, call.function_name) == (f"call-{turn}", "Execute")
        assert call.arguments == {"command": f"echo {turn}"}
        (result,) = step.observation.results  # type: ignore[union-attr]
        assert (result.source_call_id, result.content) == (call.tool_call_id, str(turn))
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 30


def test_without_the_session_each_turn_comes_from_its_stream(tmp_path: Path) -> None:
    import shutil

    write_resumed_run(tmp_path)
    shutil.rmtree(tmp_path / ".droid-orbit")
    trajectory = DroidHarness().parse_trajectory(recorded_context(tmp_path))

    assert trajectory.extra["orbit"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "native_session_unavailable", "session_id": SESSION_ID}
    ]
    assert [(step.source, step.message) for step in trajectory.steps] == RESUMED_STEPS
    for turn, step in enumerate(trajectory.steps[1::3], 1):
        (call,) = step.tool_calls or ()
        assert call.tool_call_id == f"call-{turn}"
        assert step.observation.results[0].source_call_id == call.tool_call_id  # type: ignore[union-attr]


def test_notices_and_failed_turns_become_system_steps_and_mcp_is_named(tmp_path: Path) -> None:
    record(tmp_path, "Use the server.")
    write_session(
        tmp_path,
        SESSION_ID,
        {"type": "session_start", "id": SESSION_ID},
        message("user", {"type": "text", "text": "<system-reminder>"}, visibility="llm_only"),
        message("user", {"type": "text", "text": "Use the server."}),
        message(
            "assistant",
            {"type": "thinking", "thinking": "plan"},
            {"type": "tool_use", "id": "t1", "name": "task-proof___derive", "input": {"n": 1}},
        ),
        message(
            "user",
            {"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True},
        ),
        message(
            "user",
            {"type": "text", "text": "Unable to reach the provider."},
            visibility="user_only",
        ),
        {"type": "agent_turn_outcome", "reason": "model_provider_unreachable"},
        {"type": "compaction_v9"},
    )
    trajectory = DroidHarness().parse_trajectory(recorded_context(tmp_path, session_id=SESSION_ID))

    assert [step.source for step in trajectory.steps] == ["user", "agent", "system"]
    agent = trajectory.steps[1]
    assert agent.reasoning_content == "plan"
    (call,) = agent.tool_calls or ()
    assert call.extra == {"orbit": {"mcp": {"server": "task-proof", "tool": "derive"}}}
    (result,) = agent.observation.results  # type: ignore[union-attr]
    assert result.extra == {"orbit": {"is_error": True}} and result.content == "boom"
    assert trajectory.steps[2].message == "Unable to reach the provider."
    assert trajectory.extra["orbit"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "unknown_native_event_types", "types": ["compaction_v9"]}
    ]


def test_a_message_droid_writes_as_the_user_is_a_notice(tmp_path: Path) -> None:
    warning = "Error: Your custom model appears to be a Claude model."
    record(tmp_path, "Go.")
    write_session(
        tmp_path,
        SESSION_ID,
        message("user", {"type": "text", "text": warning}, visibility="both", id="warning"),
        message("user", {"type": "text", "text": "<system-reminder>"}, visibility="llm_only"),
        message("user", {"type": "text", "text": "Go."}),
        message("assistant", {"type": "text", "text": "Done."}),
        {"type": "agent_turn_outcome", "reason": "completed"},
    )
    trajectory = DroidHarness().parse_trajectory(recorded_context(tmp_path, session_id=SESSION_ID))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("system", warning),
        ("user", "Go."),
        ("agent", "Done."),
    ]
    assert not trajectory.extra


def test_a_failure_only_the_stream_reports_ends_its_turn(tmp_path: Path) -> None:
    record(
        tmp_path,
        "Go.",
        stdout=stream({"type": "error", "source": "cli", "message": "terminated"}),
    )
    write_session(
        tmp_path,
        SESSION_ID,
        message("user", {"type": "text", "text": "Go."}),
        message("assistant", {"type": "text", "text": "Working"}),
    )
    other = "11111111-2222-3333-4444-555555555555"
    write_session(tmp_path, other, message("user", {"type": "text", "text": "other"}))
    trajectory = DroidHarness().parse_trajectory(recorded_context(tmp_path, session_id=SESSION_ID))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Go."),
        ("agent", "Working"),
        ("system", "terminated"),
    ]
    (issue,) = trajectory.extra["orbit"]["parse_issues"]  # type: ignore[index]
    assert issue["reason"] == "unconverted_native_sessions"
    assert issue["sources"] == [f".droid-orbit/.factory/sessions/-home-agent/{other}.jsonl"]


def test_steps_take_the_times_droid_records(tmp_path: Path) -> None:
    """The session's ISO times; a turn only stream-json has takes its events' Unix
    milliseconds. Neither records a model call's usage or response ID."""
    call = {"type": "tool_use", "id": "t1", "name": "LS", "input": {}}
    write_session(
        tmp_path,
        SESSION_ID,
        {**message("user", {"type": "text", "text": "List."}), "timestamp": "2026-10-26T00:00:00Z"},
        {**message("assistant", call, id="a1"), "timestamp": "2026-10-26T00:00:01.500Z"},
        message("user", {"type": "tool_result", "tool_use_id": "t1", "content": "a"}),
        # A time that names no zone is ambiguous.
        {
            **message("assistant", {"type": "text", "text": "Done."}, id="a2"),
            "timestamp": "2026-10-26T00:00:02",
        },
    )
    epoch = 1792972800000  # 2026-10-26T00:00:00Z
    record(
        tmp_path,
        "List.",
        "Again.",
        stdout=[
            "",
            stream(
                {"type": "message", "role": "user", "timestamp": epoch + 3000},
                {
                    "type": "tool_call",
                    "id": "t2",
                    "messageId": "m2",
                    "toolName": "LS",
                    "parameters": {},
                    "timestamp": epoch + 4000,
                },
                {"type": "tool_result", "id": "t2", "value": "a"},
                {
                    "type": "message",
                    "role": "assistant",
                    "text": "Done.",
                    "timestamp": epoch + 5000,
                },
                {"type": "error", "message": "terminated", "timestamp": epoch + 6000},
            ),
        ],
    )

    trajectory = DroidHarness().parse_trajectory(recorded_context(tmp_path))

    assert [(step.source, step.timestamp) for step in trajectory.steps] == [
        ("user", "2026-10-26T00:00:00Z"),
        ("agent", "2026-10-26T00:00:01.500Z"),
        ("agent", None),
        ("user", "2026-10-26T00:00:03.000000Z"),
        ("agent", "2026-10-26T00:00:04.000000Z"),
        ("agent", "2026-10-26T00:00:05.000000Z"),
        ("system", "2026-10-26T00:00:06.000000Z"),
    ]
    assert all(step.metrics is None for step in trajectory.steps)
    assert trajectory.steps[1].extra == {"orbit": {"native_event_ids": ["a1"]}}


def test_each_invocations_usage_counts_as_the_gateway_does(tmp_path: Path) -> None:
    """Stream-json reports only each invocation's total; its input leaves out the cache
    reads and writes."""
    write_session(tmp_path, SESSION_ID, message("user", {"type": "text", "text": "Go."}))
    usage = {
        "input_tokens": 7,
        "output_tokens": 40,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 300,
    }
    record(
        tmp_path,
        "Go.",
        "Again.",
        stdout=[
            stream({"type": "completion", "usage": usage}),
            stream({"type": "completion", "usage": {"input_tokens": 7, "output_tokens": "x"}}),
        ],
    )

    trajectory = DroidHarness().parse_trajectory(recorded_context(tmp_path))

    assert trajectory.final_metrics is not None
    assert (
        trajectory.final_metrics.total_prompt_tokens,
        trajectory.final_metrics.total_completion_tokens,
        trajectory.final_metrics.total_cached_tokens,
    ) == (1307, 40, 1000)
    assert {"reason": "invalid_token_usage", "invocation": 2} in trajectory.extra["orbit"][  # type: ignore[index]
        "parse_issues"
    ]
