from __future__ import annotations

import base64
import json
from pathlib import Path, PurePosixPath

import pytest
from tests.unit.test_cli_harnesses import FakeSandbox, resources, session

from ale.core.config import load_run_config
from ale.core.errors import AgentError, ConfigError, HarnessLimitError
from ale.core.harness import EffectiveAgentResources, ResolvedMcpServer, TrajectoryParseContext
from ale.core.taskspec import OperatingSystem, StreamableHttpMcpServer
from ale.core.testkit import AutonomousHarnessConformance
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.harnesses.muse_code import MuseCodeHarness
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit

RUN_ID = "parent-1"
RUN_LOG = f".local/share/muse/sessions/2026/10/26/{RUN_ID}/session.jsonl"
CHILD_ID = "child-1"
CHILD_LOG = str(PurePosixPath(RUN_LOG).parent / "subagent" / CHILD_ID / "session.jsonl")
SETTINGS = "/home/agent/.config/muse/settings.json"


def test_preset_is_complete_and_speaks_meta_responses() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "muse-code.toml")
    harness = _harness(config)

    assert isinstance(harness, MuseCodeHarness)
    assert config.gateway.dialect == "openai-responses"
    assert config.agent.version == harness.default_version
    assert set(config.agent.settings) == set(harness.settings_type.model_fields)


def test_other_gateway_dialects_are_rejected() -> None:
    config = load_run_config(
        preset_path=PRESET_DIR / "muse-code.toml",
        overrides=['gateway.dialect="anthropic"'],
    )
    with pytest.raises(ConfigError, match=r"requires gateway\.dialect"):
        _harness(config)


def test_release_binaries_are_pinned_for_both_systems() -> None:
    harness = MuseCodeHarness()
    assert set(harness.supported_os) == {OperatingSystem.LINUX, OperatingSystem.WINDOWS}
    assert {key for key in harness.release_assets} == {
        (OperatingSystem.LINUX, "x86_64"),
        (OperatingSystem.LINUX, "aarch64"),
        (OperatingSystem.WINDOWS, "x86_64"),
    }
    assert all(
        f"version={harness.default_version}&" in asset.url
        for asset in harness.release_assets.values()
    )
    with pytest.raises(ConfigError, match="pins release digests"):
        MuseCodeHarness(cli_version="1.4.10-R6427.1")


@pytest.mark.parametrize(
    "settings",
    [
        {"max_steps": 3},
        {"max_model_steps": 0},
        {"max_model_steps": -1},
        {"max_model_steps": False},
        {"reasoning_effort": "none"},
        {"reminders": "off"},
    ],
)
def test_settings_are_strict(settings: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        MuseCodeHarness(settings=settings)


async def test_settings_file_holds_episode_resources(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    harness = MuseCodeHarness()
    effective = resources(tmp_path)

    harness.validate_resources(effective)
    await harness.install_resources(sandbox, session(), effective)  # type: ignore[arg-type]

    rendered = sandbox.files[SETTINGS].decode()
    assert json.loads(rendered) == {
        "schema_version": 1,
        "endpoint_transport": {"base_url": "http://gateway/v1", "auth": "bearer"},
        "telemetry": {"enabled": False},
        "mcp_servers": {
            "task-proof": {
                "transport": "stdio",
                "command": "python3",
                "args": ["/home/agent/server.py"],
                "env": {"PROOF_HOME": "/home/agent"},
                "enabled": True,
            }
        },
    }
    assert "token" not in rendered
    assert sandbox.uploads == [
        (str(effective.skills[0].path), "/home/agent/.config/muse/skills/resource-proof")
    ]


async def test_http_mcp_servers_use_the_streamable_http_transport() -> None:
    sandbox = FakeSandbox()
    remote = ResolvedMcpServer(
        name="remote",
        server=StreamableHttpMcpServer(
            name="remote", transport="streamable-http", url="https://mcp.example/mcp"
        ),
        source_layers=("run",),
        declared_sources=("remote",),
        digest="sha256:" + "e" * 64,
        reportable=True,
    )
    await MuseCodeHarness().install_resources(
        sandbox,  # type: ignore[arg-type]
        session(),
        EffectiveAgentResources(mcp_servers=(remote,), digest="sha256:" + "f" * 64),
    )
    assert json.loads(sandbox.files[SETTINGS])["mcp_servers"] == {
        "remote": {
            "transport": "streamable_http",
            "url": "https://mcp.example/mcp",
            "enabled": True,
        }
    }


async def test_command_reads_the_prompt_file_and_names_its_session() -> None:
    harness = MuseCodeHarness(
        settings={
            "reasoning_effort": "xhigh",
            "max_model_steps": 40,
            "reminders": False,
            "web_tools": False,
        }
    )
    launch = await harness.prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=None,
    )
    assert launch.session_id is not None
    script = launch.script
    assert script.startswith(
        "unset CODEX_HOME CLAUDE_CONFIG_DIR; /home/agent/.local/bin/muse exec "
    )
    assert "--prompt-file /home/agent/.ale-harness/prompt.md" in script
    assert "--base-url" not in script
    assert "--model test-model" in script
    assert f"--session-id {launch.session_id}" in script
    assert "--yolo --no-foreign-personal-context" in script
    assert "--reasoning-effort xhigh --max-model-steps 40" in script
    assert "--disable-reminders --disable-web-tools" in script
    assert launch.env["META_API_KEY"] == "token"
    assert launch.env["XDG_CONFIG_HOME"] == "/home/agent/.config"
    assert launch.env["XDG_DATA_HOME"] == "/home/agent/.local/share"

    resume = await MuseCodeHarness().prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=RUN_ID,
    )
    assert resume.session_id == RUN_ID
    assert f"--session-id {RUN_ID}" in resume.script
    assert "--reasoning-effort" not in resume.script
    assert "--max-model-steps" not in resume.script


async def test_windows_command_runs_the_native_executable() -> None:
    command = await MuseCodeHarness().prepare_command(
        "C:/Users/Agent/.ale-harness/prompt.md",
        FakeSandbox(stdout=r"C:\Windows\System32"),  # type: ignore[arg-type]
        session(home=r"C:\Users\Agent"),
        native_session_id=None,
    )
    assert "C:/Users/Agent/.local/bin/muse.exe exec " in command.script
    assert command.env["XDG_DATA_HOME"] == "C:/Users/Agent/.local/share"


def terminal(session_id: str, reply: str) -> str:
    """The stdout line with which Muse ends a completed run."""
    record = {
        "stream": {"kind": "session", "id": session_id},
        "payload_type": "run.terminal.completed",
        "payload": {"text": reply},
    }
    return json.dumps(record) + "\n"


def test_result_reading_uses_the_terminal_record() -> None:
    harness = MuseCodeHarness()
    result = harness.read_result(terminal(RUN_ID, "FIRST-DONE") + terminal(RUN_ID, "SECOND-DONE"))
    assert result.session_id == RUN_ID
    assert result.final_message == "SECOND-DONE"
    assert result.error is None
    failed = harness.read_result(
        json.dumps(
            {
                "stream": {"kind": "session", "id": RUN_ID},
                "payload_type": "run.terminal.failed",
                "payload": {"reason": "API error 500"},
            }
        )
        + "\n"
    )
    assert failed.error == "API error 500"
    unfinished = harness.read_result(
        json.dumps({"stream": {"kind": "session", "id": RUN_ID}, "payload_type": "x"}) + "\n"
    )
    assert unfinished.error == "Muse Code reported no terminal result"


@pytest.mark.parametrize(
    ("settings", "error"),
    [({}, AgentError), ({"max_model_steps": 2}, HarnessLimitError)],
)
async def test_native_failures_keep_the_cause(settings: dict[str, object], error: type) -> None:
    sandbox = FakeSandbox(1)
    sandbox.native_stdout = (
        json.dumps(
            {
                "stream": {"kind": "session", "id": RUN_ID},
                "payload_type": "run.terminal.failed",
                "payload": {
                    "terminal": "failed",
                    "reason": "model did not reach a terminal state within 2 step(s)",
                },
            }
        ).encode()
        + b"\n"
    )
    sandbox.native_stderr = b"run ended with Failed token\n"
    with pytest.raises(error) as caught:
        await MuseCodeHarness(settings=settings).launch(
            "work",
            sandbox,  # type: ignore[arg-type]
            session(),
            timeout_sec=60,
        )
    if error is AgentError:
        assert "within 2 step(s)" in str(caught.value)
        assert "token" not in str(caught.value)
    else:
        assert caught.value.limit == "max_model_steps"


def test_session_files_match_only_the_exact_session() -> None:
    harness = MuseCodeHarness()
    sessions = ".local/share/muse/sessions"
    assert harness.is_session_file(PurePosixPath(RUN_LOG), RUN_ID)
    assert harness.is_session_file(PurePosixPath(CHILD_LOG), RUN_ID)
    assert not harness.is_session_file(PurePosixPath(RUN_LOG), RUN_ID[:-1])
    assert not harness.is_session_file(
        PurePosixPath(sessions, f".msp-view-g6/{RUN_ID}/x/journal.bin"), RUN_ID
    )
    assert not harness.is_session_file(PurePosixPath(sessions, f"2026/10/{RUN_ID}/x"), RUN_ID)


def context(logs: Path, tmp_path: Path, **updates: object) -> TrajectoryParseContext:
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "trajectory",
        "instruction": "Use the tools.",
        "logs_dir": logs,
        "model": "muse-spark-1.3",
        "agent_version": "1.4.13-R6996.1",
        "blobs": BlobStore(tmp_path / "episode"),
        "session_id": RUN_ID,
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def run(kind: str, **event: object) -> dict[str, object]:
    """One event of a run in a Muse session log."""
    return {
        "payload_type": "runtime.session",
        "payload": {"kind": "run", "event": {"kind": kind, **event}},
    }


def write_turns(logs: Path, requested: str | None, *turns: tuple[str, str]) -> None:
    """One recorded invocation per prompt, its stdout ending in the reply."""
    records: list[dict[str, object]] = []
    stdout = ""
    for prompt, reply in turns:
        records.append({"prompt": prompt, "session_id": requested, "stdout_offset": len(stdout)})
        stdout += terminal(RUN_ID, reply)
    write_jsonl(logs / ".ale-harness/invocations.jsonl", records)
    (logs / ".ale-harness/stdout.log").write_text(stdout)


def spawning_session(
    prompt: str, child_id: str, *, attested: bool = True
) -> list[dict[str, object]]:
    """A session whose one response runs a command and spawns ``child_id``."""
    calls = [
        {"call_id": "call-1", "name": "bash", "args": "{}"},
        {"call_id": "call-2", "name": "subagent_spawn", "args": "{}"},
    ]
    start = {
        "payload_type": "subagent.control.start_attested",
        "payload": {
            "record": {"dispatched_via_tool_call_id": "call-2", "subagent_session_id": child_id}
        },
    }
    results = [
        {"tool_call_id": "call-1", "text": "ok"},
        {"tool_call_id": "call-2", "text": "accepted"},
    ]
    return [
        run("started", prompt=prompt),
        run("assistant_tool_calls_committed", response_id="r1", tool_calls=calls),
        *([start] if attested else []),
        run("tool_result_batch_committed", results=results),
    ]


def working_session() -> list[dict[str, object]]:
    """A subagent session that runs one command, then replies."""
    return [
        run("started", prompt="Role: helper"),
        run(
            "assistant_tool_calls_committed",
            response_id="r1",
            tool_calls=[{"call_id": "call-1", "name": "bash", "args": "{}"}],
        ),
        run("tool_result_batch_committed", results=[{"tool_call_id": "call-1", "text": "ok"}]),
        run("assistant_message_committed", response_id="r2", text="helper done"),
    ]


def write_spawn(logs: Path, child: list[dict[str, object]], *, attested: bool = True) -> Path:
    """A launch whose agent spawns ``child``, then replies once Muse delivers its result.

    Returns the child's session log.
    """
    write_turns(logs, None, ("Spawn a helper.", "parent done"))
    write_jsonl(
        logs / RUN_LOG,
        [
            *spawning_session("Spawn a helper.", CHILD_ID, attested=attested),
            run("started"),
            run("inbox_item_drained", delivery_snapshot={"body": "summary: helper done"}),
            run("assistant_message_committed", response_id="r2", text="parent done"),
        ],
    )
    write_jsonl(logs / CHILD_LOG, child)
    return logs / CHILD_LOG


def test_parser_follows_the_session_log(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    write_turns(logs, RUN_ID, ("Use the tools.", "FIRST-DONE"), ("second", "SECOND-DONE"))
    calls = [
        {"call_id": "call-1", "name": "bash", "args": "{}"},
        {"call_id": "call-2", "name": "mcp__proof__echo", "args": "{}"},
        {"call_id": "call-3", "name": "read_file", "args": "{}"},
    ]
    results = [
        {"tool_call_id": "call-1", "text": "ok"},
        {"tool_call_id": "call-2", "text": "echo:proof"},
        {"tool_call_id": "call-3", "text": "large.png"},
    ]
    png = {"media_type": "image/png", "base64_data": base64.b64encode(b"png").decode()}
    framed = run("assistant_message_committed", response_id="r2", text="FIRST-DONE")
    second = {"id": "started-2", **run("started", prompt="second")}
    write_jsonl(
        logs / RUN_LOG,
        [
            run("started", prompt="Use the tools."),
            # Muse records a response's usage before its tool calls but after its text.
            run("model_completed", usage={"input_tokens": 10}),
            run("assistant_tool_calls_committed", response_id="r1", tool_calls=calls),
            run("tool_result_batch_committed", results=results),
            run("tool_result_model_visible_content", call_id="call-3", content=[png]),
            # A record may sit inside a retained frame, and the same record may recur.
            {"children": [{"record_json": json.dumps(framed)}]},
            run("model_completed", usage={"input_tokens": 20}),
            second,
            second,
            run("assistant_message_committed", response_id="r3", text="SECOND-DONE"),
            run("model_completed", usage={"input_tokens": 30}),
        ],
    )
    parse = context(logs, tmp_path)
    AutonomousHarnessConformance.check_parser_is_pure(MuseCodeHarness(), parse)
    trajectory = MuseCodeHarness().parse_trajectory(parse)

    assert trajectory.session_id == RUN_ID
    assert trajectory.extra is None
    assert [(step.source, step.message) for step in trajectory.steps if not step.tool_calls] == [
        ("user", "Use the tools."),
        ("agent", "FIRST-DONE"),
        ("user", "second"),
        ("agent", "SECOND-DONE"),
    ]
    (acting,) = [step for step in trajectory.steps if step.tool_calls]
    assert [(call.function_name, call.extra) for call in acting.tool_calls or ()] == [
        ("bash", None),
        ("mcp__proof__echo", {"ale": {"mcp": {"server": "proof", "tool": "echo"}}}),
        ("read_file", None),
    ]
    shell, echo, image = acting.observation.results  # type: ignore[union-attr]
    assert (shell.content, echo.content) == ("ok", "echo:proof")
    assert isinstance(image.content, list)
    text, picture = image.content
    assert text.text == "large.png"
    assert picture.type == "image" and picture.source is not None
    assert (tmp_path / "episode" / picture.source.path).read_bytes() == b"png"
    agent = [step for step in trajectory.steps if step.source == "agent"]
    assert [step.metrics.prompt_tokens for step in agent] == [10, 20, 30]  # type: ignore[union-attr]
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 60


def test_provider_run_calls_join_the_response_that_made_them(tmp_path: Path) -> None:
    """Hosted web search runs inside a response, and Muse records it apart from its calls."""
    logs = tmp_path / "logs"
    write_turns(logs, None, ("Search the web.", "2.34.2"))
    search = {"type": "search", "query": "requests pypi"}
    page = {"type": "open_page", "url": "https://pypi.org/project/requests/"}
    hits = [{"title": "requests", "url": "https://pypi.org/project/requests/"}]
    write_jsonl(
        logs / RUN_LOG,
        [
            run("started", prompt="Search the web."),
            run("model_response_created"),
            # A search may come before anything else its response commits.
            run(
                "provider_tool_call_completed",
                call_id="ws-1",
                name="web_search",
                action=search,
                status="completed",
                results=hits,
            ),
            run("assistant_message_committed", response_id="r1", text="Searching."),
            run(
                "provider_tool_call_completed",
                call_id="ws-2",
                name="web_search",
                action=page,
                status="failed",
            ),
            run(
                "assistant_tool_calls_committed",
                response_id="r1",
                tool_calls=[{"call_id": "call-1", "name": "bash", "args": "{}"}],
            ),
            run("tool_result_batch_committed", results=[{"tool_call_id": "call-1", "text": "ok"}]),
            run("model_response_created"),
            run("assistant_message_committed", response_id="r2", text="2.34.2"),
        ],
    )
    trajectory = MuseCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert trajectory.extra is None
    (acting,) = [step for step in trajectory.steps if step.tool_calls]
    assert acting.message == "Searching."
    assert [
        (call.tool_call_id, call.function_name, call.arguments) for call in acting.tool_calls or ()
    ] == [("ws-1", "web_search", search), ("ws-2", "web_search", page), ("call-1", "bash", {})]
    found, opened, shell = acting.observation.results  # type: ignore[union-attr]
    assert json.loads(str(found.content)) == {"status": "completed", "results": hits}
    assert found.extra is None
    assert opened.extra == {"ale": {"is_error": True}}
    assert shell.content == "ok"
    assert trajectory.steps[-1].message == "2.34.2"


def test_parser_embeds_the_subagent_its_call_started(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    write_spawn(logs, working_session())

    trajectory = MuseCodeHarness().parse_trajectory(context(logs, tmp_path, session_id=None))

    assert trajectory.session_id == RUN_ID
    assert "parse_issues" not in (trajectory.extra or {}).get("ale", {})
    # Muse delivers the subagent's result to the parent before the parent's next reply.
    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Spawn a helper."),
        ("agent", ""),
        ("system", "summary: helper done"),
        ("agent", "parent done"),
    ]
    spawn = trajectory.steps[1]
    assert [call.function_name for call in spawn.tool_calls or ()] == ["bash", "subagent_spawn"]
    shell, spawned = spawn.observation.results  # type: ignore[union-attr]
    (child,) = trajectory.subagent_trajectories or ()
    assert shell.subagent_trajectory_ref is None
    assert spawned.subagent_trajectory_ref is not None
    assert spawned.subagent_trajectory_ref[0].trajectory_id == child.trajectory_id
    assert child.trajectory_id == f"trajectory-subagent-{CHILD_ID}"
    assert [(step.source, step.message) for step in child.steps] == [
        ("user", "Role: helper"),
        ("agent", ""),
        ("agent", "helper done"),
    ]
    assert [call.function_name for call in child.steps[1].tool_calls or ()] == ["bash"]
    (result,) = child.steps[1].observation.results  # type: ignore[union-attr]
    assert result.content == "ok"


def test_a_subagent_spawned_by_a_subagent_is_embedded_in_it(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    child = write_spawn(logs, spawning_session("Role: helper", "grandchild-1"))
    write_jsonl(child.parent / "subagent/grandchild-1/session.jsonl", working_session())

    trajectory = MuseCodeHarness().parse_trajectory(
        context(logs, tmp_path, session_id=None, incomplete=True)
    )

    assert "parse_issues" not in trajectory.extra["ale"]  # type: ignore[index]
    (helper,) = trajectory.subagent_trajectories or ()
    (nested,) = helper.subagent_trajectories or ()
    assert nested.trajectory_id == f"trajectory-subagent-{CHILD_ID}-subagent-grandchild-1"
    shell, spawned = helper.steps[1].observation.results  # type: ignore[union-attr]
    assert shell.subagent_trajectory_ref is None
    assert spawned.subagent_trajectory_ref is not None
    assert spawned.subagent_trajectory_ref[0].trajectory_id == nested.trajectory_id
    # An interrupted run leaves every session in it incomplete.
    for subagent in (helper, nested):
        assert subagent.extra == {"ale": {"incomplete": True, "incomplete_reason": "interrupted"}}


def test_a_subagent_without_its_start_attestation_stays_raw_evidence(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    write_spawn(logs, working_session(), attested=False)

    trajectory = MuseCodeHarness().parse_trajectory(context(logs, tmp_path, session_id=None))

    assert trajectory.subagent_trajectories is None
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "unlinked_subagent", "source": CHILD_LOG},
        {"reason": "unconverted_native_files", "count": 1, "sources": [CHILD_LOG]},
    ]
    results = trajectory.steps[1].observation.results  # type: ignore[union-attr]
    assert [result.subagent_trajectory_ref for result in results] == [None, None]


def test_reminder_agents_stay_raw_evidence_without_being_unlinked(tmp_path: Path) -> None:
    # Muse runs its reminder agents on its own, with no call of the model to start them, and
    # keeps their logs below the session as it does a subagent's.
    logs = tmp_path / "logs"
    write_jsonl(
        logs / RUN_LOG,
        [
            run("started", prompt="Use the tools."),
            run("memory_reminder_child_session_linked", child_session_id="reminder-1"),
        ],
    )
    reminder = str(PurePosixPath(RUN_LOG).parent / "subagent/reminder-1/session.jsonl")
    write_jsonl(logs / reminder, [run("started")])

    trajectory = MuseCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert trajectory.subagent_trajectories is None
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "unconverted_native_files", "count": 1, "sources": [reminder]}
    ]


def test_tool_failures_and_failed_runs_are_recorded(tmp_path: Path) -> None:
    def record(payload_type: str, payload: dict[str, object], at: int) -> str:
        return (
            json.dumps(
                {
                    "id": f"r{at}",
                    "stream": {"kind": "session", "id": RUN_ID},
                    "recorded_at": 1792988700000000 + at,
                    "payload_type": payload_type,
                    "payload": payload,
                }
            )
            + "\n"
        )

    log = tmp_path / "logs" / RUN_LOG
    log.parent.mkdir(parents=True)
    log.write_text(
        record("runtime.session", {"kind": "run", "event": {"kind": "started", "prompt": "go"}}, 1)
        + record(
            "runtime.session",
            {
                "kind": "run",
                "event": {
                    "kind": "assistant_tool_calls_committed",
                    "response_id": "resp_1",
                    "tool_calls": [{"call_id": "call_1", "name": "bash", "args": "{}"}],
                },
            },
            2,
        )
        + record(
            "runtime.session",
            {
                "kind": "task",
                "event": {
                    "kind": "side_effect_intent",
                    "idempotency_key": "tool:call_1",
                    "task_id": "t1",
                },
            },
            3,
        )
        + record(
            "runtime.session",
            {"kind": "task", "event": {"kind": "failed", "task_id": "t1", "reason": "denied"}},
            4,
        )
        + record(
            "runtime.session",
            {"kind": "run", "event": {"kind": "terminal", "terminal": "failed", "reason": "boom"}},
            5,
        )
    )

    trajectory = MuseCodeHarness().parse_trajectory(
        context(tmp_path / "logs", tmp_path, instruction="go")
    )

    (result,) = trajectory.steps[1].observation.results  # type: ignore[union-attr]
    assert result.content == "denied"
    assert result.extra == {"ale": {"is_error": True}}
    assert (trajectory.steps[-1].source, trajectory.steps[-1].message) == ("system", "boom")


def test_without_the_session_each_turn_comes_from_its_prompt_and_stdout(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    write_turns(logs, RUN_ID, ("Use the tools.", "FIRST-DONE"), ("second", "SECOND-DONE"))

    trajectory = MuseCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Use the tools."),
        ("agent", "FIRST-DONE"),
        ("user", "second"),
        ("agent", "SECOND-DONE"),
    ]
    ale = trajectory.extra["ale"]  # type: ignore[index]
    assert ale["parse_issues"] == [{"reason": "native_session_unavailable", "session_id": RUN_ID}]
