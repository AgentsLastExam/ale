from __future__ import annotations

import json
import shutil
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

FIXTURES = Path(__file__).parents[1] / "fixtures" / "harness-logs"
RUN = FIXTURES / "muse-code-run"
RUN_ID = "c2a583fb-5ab2-44d0-9a18-84b5fccbf2ac"
RUN_LOG = f".local/share/muse/sessions/2026/10/26/{RUN_ID}/session.jsonl"
SPAWN = FIXTURES / "muse-code-subagent"
SPAWN_ID = "01a17674-a411-7e50-9322-53c66e75930d"
SPAWN_LOG = f".local/share/muse/sessions/2026/10/26/{SPAWN_ID}/session.jsonl"
CHILD_ID = "01a17674-a591-7ae0-a66b-37670faa9a02"
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


def test_result_reading_uses_the_terminal_record() -> None:
    harness = MuseCodeHarness()
    result = harness.read_result((RUN / ".ale-harness/stdout.log").read_text())
    assert result.session_id == RUN_ID
    assert result.final_message == "SECOND-DONE"
    assert result.error is None
    failed = harness.read_result(
        json.dumps(
            {
                "stream": {"kind": "session", "id": RUN_ID},
                "payload_type": "run.terminal.failed",
                "payload": {"terminal": "failed", "reason": "API error 500", "text": ""},
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
    assert harness.is_session_file(
        PurePosixPath(sessions, f"2026/10/26/{SPAWN_ID}/subagent/{CHILD_ID}/session.jsonl"),
        SPAWN_ID,
    )
    assert not harness.is_session_file(PurePosixPath(RUN_LOG), RUN_ID[:-1])
    assert not harness.is_session_file(
        PurePosixPath(sessions, f".msp-view-g6/{RUN_ID}/x/journal.bin"), RUN_ID
    )
    assert not harness.is_session_file(PurePosixPath(sessions, f"2026/10/{RUN_ID}/x"), RUN_ID)


def context(logs: Path, tmp_path: Path, **updates: object) -> TrajectoryParseContext:
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "trajectory",
        "instruction": "Use the shell, the MCP tool, then look at large.png",
        "logs_dir": logs,
        "model": "muse-spark-1.3",
        "agent_version": "1.4.13-R6996.1",
        "blobs": BlobStore(tmp_path / "episode"),
        "session_id": RUN_ID,
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def test_parser_follows_the_session_log(tmp_path: Path) -> None:
    parse = context(RUN, tmp_path)
    AutonomousHarnessConformance.check_parser_is_pure(MuseCodeHarness(), parse)
    trajectory = MuseCodeHarness().parse_trajectory(parse)

    assert trajectory.session_id == RUN_ID
    assert trajectory.extra is None
    assert [(step.source, step.message) for step in trajectory.steps if not step.tool_calls] == [
        ("user", "Use the shell, the MCP tool, then look at large.png"),
        ("agent", "FIRST-DONE"),
        ("user", "second"),
        ("agent", "SECOND-DONE"),
    ]
    calls = {
        call.function_name: (call, step.observation.results[0])  # type: ignore[union-attr]
        for step in trajectory.steps
        for call in step.tool_calls or ()
    }
    assert set(calls) == {"bash", "mcp__proof__echo", "read_file"}
    assert '"exit_code": 0' in str(calls["bash"][1].content)
    mcp_call, mcp_result = calls["mcp__proof__echo"]
    assert mcp_call.extra == {"ale": {"mcp": {"server": "proof", "tool": "echo"}}}
    assert mcp_result.content == "echo:mcp-proof"
    image_result = calls["read_file"][1]
    assert isinstance(image_result.content, list)
    image = image_result.content[1]
    assert image.type == "image" and image.source is not None
    assert (tmp_path / "episode" / image.source.path).is_file()
    assert all(step.metrics is not None for step in trajectory.steps if step.source == "agent")
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 55


def test_parser_embeds_the_subagent_its_call_started(tmp_path: Path) -> None:
    trajectory = MuseCodeHarness().parse_trajectory(
        context(SPAWN, tmp_path, instruction="Spawn a helper.", session_id=None)
    )

    assert trajectory.session_id == SPAWN_ID
    assert "parse_issues" not in (trajectory.extra or {}).get("ale", {})
    spawn = trajectory.steps[1]
    assert [call.function_name for call in spawn.tool_calls or ()] == ["subagent_spawn"]
    (result,) = spawn.observation.results  # type: ignore[union-attr]
    (child,) = trajectory.subagent_trajectories or ()
    assert result.subagent_trajectory_ref is not None
    assert result.subagent_trajectory_ref[0].trajectory_id == child.trajectory_id
    assert child.trajectory_id == f"trajectory-subagent-{CHILD_ID}"
    assert [(step.source, step.message) for step in child.steps] == [
        ("user", "Role: helper\nObjective: Say done."),
        ("agent", "FAKE-FINAL-2"),
    ]
    # Muse delivers the subagent's result to the parent before the parent's next reply.
    delivery = next(step for step in trajectory.steps if step.source == "system")
    assert "summary: FAKE-FINAL-2" in str(delivery.message)
    assert trajectory.steps[-1].message == "FAKE-FINAL-4"


def test_a_subagent_spawned_by_a_subagent_is_embedded_in_it(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    shutil.copytree(SPAWN, logs)
    root = logs / SPAWN_LOG
    child = root.parent / "subagent" / CHILD_ID / "session.jsonl"
    grandchild_id = "01a17674-a591-7ae0-a66b-37670faa9a03"
    grandchild = child.parent / "subagent" / grandchild_id / "session.jsonl"
    grandchild.parent.mkdir(parents=True)
    grandchild.write_text(child.read_text().replace(CHILD_ID, grandchild_id))
    # The child spawns as its parent did: the call, its start attestation and its result.
    spawn = [
        line.replace(CHILD_ID, grandchild_id)
        .replace(SPAWN_ID, CHILD_ID)
        .replace('"call_1"', '"call_nested"')
        .replace('"resp_1"', '"resp_nested"')
        for line in root.read_text().splitlines(keepends=True)
        if '"call_1"' in line
        and any(
            kind in line
            for kind in (
                "assistant_tool_calls_committed",
                "subagent.control.start_attested",
                "tool_result_batch_committed",
            )
        )
    ]
    assert len(spawn) == 3
    with child.open("a") as stream:
        stream.writelines(spawn)

    trajectory = MuseCodeHarness().parse_trajectory(
        context(logs, tmp_path, instruction="Spawn a helper.", session_id=None, incomplete=True)
    )

    assert "parse_issues" not in trajectory.extra["ale"]  # type: ignore[index]
    (helper,) = trajectory.subagent_trajectories or ()
    (nested,) = helper.subagent_trajectories or ()
    assert nested.trajectory_id == f"trajectory-subagent-{CHILD_ID}-subagent-{grandchild_id}"
    (result,) = next(step for step in helper.steps if step.tool_calls).observation.results  # type: ignore[union-attr]
    assert result.subagent_trajectory_ref is not None
    assert result.subagent_trajectory_ref[0].trajectory_id == nested.trajectory_id
    # An interrupted run leaves every session in it incomplete.
    for subagent in (helper, nested):
        assert subagent.extra == {"ale": {"incomplete": True, "incomplete_reason": "interrupted"}}


def test_a_subagent_without_its_start_attestation_stays_raw_evidence(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    shutil.copytree(SPAWN, logs)
    root = logs / SPAWN_LOG
    root.write_text(
        "".join(
            line
            for line in root.read_text().splitlines(keepends=True)
            if "subagent.control.start_attested" not in line
        )
    )

    trajectory = MuseCodeHarness().parse_trajectory(
        context(logs, tmp_path, instruction="Spawn a helper.", session_id=None)
    )

    assert trajectory.subagent_trajectories is None
    source = str(PurePosixPath(SPAWN_LOG).parent / "subagent" / CHILD_ID / "session.jsonl")
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "unlinked_subagent", "source": source},
        {"reason": "unconverted_native_files", "count": 1, "sources": [source]},
    ]
    (result,) = trajectory.steps[1].observation.results  # type: ignore[union-attr]
    assert result.subagent_trajectory_ref is None


def test_reminder_agents_stay_raw_evidence_without_being_unlinked(tmp_path: Path) -> None:
    # Muse runs its reminder agents on its own, with no call of the model to start them, and
    # keeps their logs below the session as it does a subagent's.
    logs = tmp_path / "logs"
    shutil.copytree(RUN, logs)
    root = logs / RUN_LOG
    reminder_id = "01a17668-4626-75d1-b7ea-7feebe24ea39"
    linked = {
        "id": "01a17668-462c-7e03-996c-1989ae653473",
        "stream": {"kind": "session", "id": RUN_ID},
        "recorded_at": 1792987907628713,
        "payload_type": "runtime.session",
        "payload": {
            "kind": "run",
            "event": {
                "kind": "memory_reminder_child_session_linked",
                "child_session_id": reminder_id,
                "child_session_log_path": f"subagent/{reminder_id}/session.jsonl",
                "parent_session_id": RUN_ID,
                "reminder_agent_id": "goal-reminder",
            },
        },
    }
    with root.open("a") as stream:
        stream.write(json.dumps(linked) + "\n")
    reminder = root.parent / "subagent" / reminder_id / "session.jsonl"
    reminder.parent.mkdir(parents=True)
    reminder.write_text(
        json.dumps(
            {
                "id": "r1",
                "stream": {"kind": "session", "id": reminder_id},
                "payload_type": "runtime.session",
                "payload": {"kind": "run", "event": {"kind": "started"}},
            }
        )
        + "\n"
    )

    trajectory = MuseCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert trajectory.subagent_trajectories is None
    source = str(PurePosixPath(RUN_LOG).parent / "subagent" / reminder_id / "session.jsonl")
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "unconverted_native_files", "count": 1, "sources": [source]}
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
    shutil.copytree(RUN, logs)
    shutil.rmtree(logs / ".local")

    trajectory = MuseCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Use the shell, the MCP tool, then look at large.png"),
        ("agent", "FIRST-DONE"),
        ("user", "second"),
        ("agent", "SECOND-DONE"),
    ]
    ale = trajectory.extra["ale"]  # type: ignore[index]
    assert {"reason": "native_session_unavailable", "session_id": RUN_ID} in ale["parse_issues"]
