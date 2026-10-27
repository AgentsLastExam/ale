from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from tests.unit.test_cli_harnesses import FakeSandbox, resources, session

from ale.core.config import load_run_config
from ale.core.errors import AgentError, AgentUnsupportedError, ConfigError
from ale.core.harness import EffectiveAgentResources, ResolvedMcpServer, TrajectoryParseContext
from ale.core.taskspec import OperatingSystem, StreamableHttpMcpServer
from ale.core.testkit import AutonomousHarnessConformance
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.harnesses._release import release_identity
from ale.run.harnesses.antigravity_cli import AntigravityCliHarness
from ale.run.harnesses.antigravity_cli import adapter as agy
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit

SETTINGS = "/home/agent/.gemini/antigravity-cli/settings.json"
MCP_CONFIG = "/home/agent/.gemini/config/mcp_config.json"
ROOT, CHILD = "root", "child"
BRAIN = ".gemini/antigravity-cli/brain"
MODEL = "gemini-3.8-flash"


def test_preset_is_complete_and_selects_the_gemini_provider() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "antigravity-cli.toml")
    harness = _harness(config)

    assert isinstance(harness, AntigravityCliHarness)
    assert (config.gateway.provider, config.gateway.dialect) == ("gemini", "gemini")
    assert (config.gateway.base_url, config.gateway.api_key_env) == ("", "")
    assert config.agent.version == harness.default_version
    assert set(config.agent.settings) == set(harness.settings_type.model_fields)
    assert config.agent.model in agy._MODELS


@pytest.mark.parametrize("settings", [{"efort": "high"}, {"effort": "max"}, {"effort": 1}])
def test_settings_are_strict(settings: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        AntigravityCliHarness(settings=settings)


def test_only_the_pinned_build_is_accepted() -> None:
    assert AntigravityCliHarness(cli_version="1.4.7").version() == "1.4.7"
    with pytest.raises(ConfigError, match=r"1\.4\.7 only"):
        AntigravityCliHarness(cli_version="1.4.6")


def test_release_table_pins_every_official_build() -> None:
    harness = AntigravityCliHarness()

    assert harness.release_assets is agy.RELEASE_ASSETS
    assert harness.integrity() == release_identity(agy.RELEASE_ASSETS)
    assert set(agy.RELEASE_ASSETS) == {
        (system, architecture)
        for system in OperatingSystem
        for architecture in ("x86_64", "aarch64")
    }
    for (system, _), asset in agy.RELEASE_ASSETS.items():
        assert asset.url.startswith(agy._RELEASE)
        # Linux ships a tar.gz holding the executable; Windows ships the .exe itself.
        assert asset.member == ("antigravity" if system is OperatingSystem.LINUX else None)


async def test_configuration_is_episode_local_and_selects_api_key_mode(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    harness = AntigravityCliHarness()
    effective = resources(tmp_path)

    harness.validate_resources(effective)
    await harness.install_resources(sandbox, session(model=MODEL), effective)  # type: ignore[arg-type]

    # Created as the agent: agy adds its own state to both directories.
    assert sandbox.commands[0] == [
        "mkdir",
        "-p",
        "/home/agent/.gemini/antigravity-cli/skills",
        "/home/agent/.gemini/config",
    ]
    assert json.loads(sandbox.files[SETTINGS]) == {"modelProvider": "gemini"}
    assert json.loads(sandbox.files[MCP_CONFIG]) == {
        "mcpServers": {
            "task-proof": {
                "command": "python3",
                "args": ["/home/agent/server.py"],
                "env": {"PROOF_HOME": "/home/agent"},
            }
        }
    }
    assert sandbox.uploads == [
        (
            str(effective.skills[0].path),
            "/home/agent/.gemini/antigravity-cli/skills/resource-proof",
        )
    ]
    assert all(b"token" not in data for data in sandbox.files.values())


async def test_http_mcp_servers_use_the_native_url_form() -> None:
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
    await AntigravityCliHarness().install_resources(
        sandbox,  # type: ignore[arg-type]
        session(model=MODEL),
        EffectiveAgentResources(mcp_servers=(remote,)),
    )

    assert json.loads(sandbox.files[MCP_CONFIG]) == {
        "mcpServers": {"remote": {"serverUrl": "https://mcp.example/mcp"}}
    }


@pytest.mark.parametrize(
    ("model", "effort"),
    [("test-model", "high"), ("gemini-3.1-pro", "high"), ("gemini-3.1-pro-preview", "medium")],
)
async def test_models_and_efforts_outside_the_native_catalog_fail_before_launch(
    model: str, effort: str
) -> None:
    harness = AntigravityCliHarness(settings={"effort": effort})
    with pytest.raises(AgentUnsupportedError):
        await harness.install_resources(
            FakeSandbox(),  # type: ignore[arg-type]
            session(model=model),
            EffectiveAgentResources(),
        )


@pytest.mark.parametrize("native_session_id", [None, ROOT])
async def test_command_feeds_the_prompt_file_on_stdin_and_points_at_the_gateway(
    native_session_id: str | None,
) -> None:
    prompt = "/home/agent/.ale-harness/prompt.md"
    command = await AntigravityCliHarness().prepare_command(
        prompt,
        FakeSandbox(),  # type: ignore[arg-type]
        session(model="gemini-3.1-pro-preview"),
        native_session_id=native_session_id,
    )

    wrapper, launch = command.script.split(" && ")
    assert wrapper.startswith("unset GOOGLE_API_KEY AGY_ADC_AUTH ")
    assert wrapper.endswith(f"{prompt} /home/agent/.ale-harness/request.jsonl")
    assert launch.endswith("< /home/agent/.ale-harness/request.jsonl")
    argv = launch.split(" < ")[0].split()
    assert argv[0] == "/home/agent/.local/bin/agy"
    assert argv[1:9] == [
        "--dangerously-skip-permissions",
        "--print-timeout=0s",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model",
        "gemini-3.1-pro",
    ]
    assert argv[9:11] == ["--effort", "high"]
    assert argv[11:] == ([] if native_session_id is None else ["--conversation", ROOT])
    assert command.session_id == native_session_id
    assert command.env["GEMINI_API_KEY"] == "token"
    assert command.env["GOOGLE_GEMINI_BASE_URL"] == "http://gateway"
    assert command.env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert command.env["HOME"] == "/home/agent"
    assert "--gemini_dir" not in command.script


async def test_default_effort_leaves_the_native_choice() -> None:
    command = await AntigravityCliHarness(settings={"effort": "default"}).prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(model=MODEL),
        native_session_id=None,
    )
    assert "--effort" not in command.script


async def test_windows_command_runs_the_exe_with_the_os_python() -> None:
    sandbox = FakeSandbox(stdout=r"C:\Windows\System32")
    command = await AntigravityCliHarness().prepare_command(
        "C:/Users/agent/.ale-harness/prompt.md",
        sandbox,  # type: ignore[arg-type]
        session(model=MODEL, home="C:/Users/agent"),
        native_session_id=None,
    )
    assert "; python -Ic " in command.script
    assert "C:/Users/agent/.local/bin/agy.exe --dangerously-skip-permissions" in command.script
    assert command.env["HOME"] == "C:/Users/agent"


@pytest.mark.parametrize(
    "prompt", ["Use the probe MCP.\nThen report.\n\n", "'quoted' $HOME `cmd` 多行\r\nend"]
)
def test_request_wrapper_carries_the_prompt_file_exactly(tmp_path: Path, prompt: str) -> None:
    source, request = tmp_path / "prompt.md", tmp_path / "request.jsonl"
    source.write_bytes(prompt.encode())
    subprocess.run(
        [sys.executable, "-I", "-c", agy._REQUEST, str(source), str(request)], check=True
    )

    data = request.read_bytes()
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert json.loads(data) == {"event": "user", "message": {"content": prompt}}


@pytest.mark.parametrize(
    ("stdout", "session_id", "final", "error"),
    [
        (
            '{"event":"init","conversation_id":"c1","init":{}}\n'
            '{"event":"result","result":{"conversation_id":"c1","status":"SUCCESS",'
            '"response":"PONG\\n"}}\n',
            "c1",
            "PONG",
            None,
        ),
        (
            '{"event":"result","result":{"conversation_id":"c1","status":"ERROR",'
            '"response":"","error":"Error 429, Status: RESOURCE_EXHAUSTED"}}\n',
            "c1",
            None,
            "Error 429, Status: RESOURCE_EXHAUSTED",
        ),
        ('{"event":"init","conversation_id":"c1","init":{}}\n', "c1", None, "without a result"),
    ],
)
def test_result_reports_the_conversation_and_its_terminal_status(
    stdout: str, session_id: str, final: str | None, error: str | None
) -> None:
    result = AntigravityCliHarness().read_result(stdout)
    assert (result.session_id, result.final_message) == (session_id, final)
    assert (result.error is None) if error is None else (error in str(result.error))


async def test_a_failed_turn_raises_with_the_native_reason() -> None:
    sandbox = FakeSandbox(exit_code=3, stderr='AGY_ERROR: {"status":"INVALID_ARGUMENT"}\n')
    sandbox.native_stdout = (
        b'{"event":"result","result":{"conversation_id":"c1","status":"ERROR",'
        b'"error":"Error 400, Message: bad request"}}\n'
    )
    with pytest.raises(AgentError, match="Error 400"):
        await AntigravityCliHarness().launch(
            "Do it.",
            sandbox,  # type: ignore[arg-type]
            session(model=MODEL),
            timeout_sec=60,
        )


def test_session_files_are_the_conversation_brain_and_database() -> None:
    harness = AntigravityCliHarness()
    brain = f"{BRAIN}/{ROOT}"
    assert harness.is_session_file(PurePosixPath(f"{brain}/.system_generated/x.jsonl"), ROOT)
    assert harness.is_session_file(
        PurePosixPath(f".gemini/antigravity-cli/conversations/{ROOT}.db"), ROOT
    )
    assert not harness.is_session_file(PurePosixPath(brain), ROOT)
    assert not harness.is_session_file(PurePosixPath(f"{brain}x/a.json"), ROOT)
    assert not harness.is_session_file(
        PurePosixPath(f".gemini/antigravity-cli/conversations/{ROOT}.db-wal"), ROOT
    )


def _logs(
    tmp_path: Path,
    turns: list[tuple[str, list[dict[str, Any]]]],
    conversations: dict[str, list[dict[str, Any]]] | None = None,
) -> TrajectoryParseContext:
    """Logs laid out as agy leaves them, written for one case.

    ``turns`` holds each invocation's prompt and the stream-json events it printed;
    ``conversations`` holds each conversation's step records for its full transcript.
    """
    logs = tmp_path / "logs"
    stdout, recorded = "", []
    for prompt, events in turns:
        recorded.append({"prompt": prompt, "stdout_offset": len(stdout.encode())})
        stdout += _jsonl(events)
    _write(logs / ".ale-harness/invocations.jsonl", _jsonl(recorded))
    _write(logs / ".ale-harness/stdout.log", stdout)
    for conversation, records in (conversations or {}).items():
        _write(_generated(logs, conversation) / "logs/transcript_full.jsonl", _jsonl(records))
    return TrajectoryParseContext(
        episode_id="episode",
        trajectory_id="trajectory",
        instruction="unused: the invocation record names the prompt",
        logs_dir=logs,
        model=MODEL,
        agent_version="1.4.7",
        blobs=BlobStore(tmp_path / "blobs"),
    )


def _generated(logs: Path, conversation: str) -> Path:
    return logs / BRAIN / conversation / ".system_generated"


def _jsonl(items: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(item) + "\n" for item in items)


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)


def _turn(response: str, *events: dict[str, Any], **result: Any) -> list[dict[str, Any]]:
    """One invocation's stdout: agy names the conversation first and reports its end last."""
    end = {"conversation_id": ROOT, "status": "SUCCESS", "response": response, **result}
    return [{"event": "init", "conversation_id": ROOT}, *events, {"event": "result", "result": end}]


def _tool_event(step: int, name: str) -> dict[str, Any]:
    update = {"step_index": step, "step_type": "tool", "tool_name": name}
    return {"event": "step_update", "step_update": update}


def _user(step: int, text: str) -> dict[str, Any]:
    content = f"<USER_REQUEST>\n{text}\n</USER_REQUEST>"
    return {"step_index": step, "type": "USER_INPUT", "content": content}


def _planner(
    step: int, text: str, *calls: tuple[str, dict[str, Any]], **fields: Any
) -> dict[str, Any]:
    tool_calls = [{"name": name, "args": args} for name, args in calls]
    return {
        "step_index": step,
        "type": "PLANNER_RESPONSE",
        "content": text,
        "tool_calls": tool_calls,
        **fields,
    }


def _result(step: int, content: str, **fields: Any) -> dict[str, Any]:
    return {"step_index": step, "type": "GENERIC", "content": content, **fields}


def _messages(step: int, *sent: tuple[str, str]) -> dict[str, Any]:
    """A SYSTEM_MESSAGE record delivering each (sender, content) message."""
    lines = "".join(
        f"[Message] timestamp=t sender={sender} priority=p content={content}\n"
        for sender, content in sent
    )
    return {
        "step_index": step,
        "type": "SYSTEM_MESSAGE",
        "content": f"<SYSTEM_MESSAGE>\n{lines}</SYSTEM_MESSAGE>",
    }


PNG = b"\x89PNG\r\n\x1a\n"


def test_parser_follows_the_native_session_through_tools_mcp_images_and_resume(
    tmp_path: Path,
) -> None:
    image = f"file:///home/agent/{BRAIN}/{ROOT}/.system_generated/logs/media/step_6_media_0.png"
    snap = {"ServerName": "probe", "ToolName": "snap", "Arguments": {}}
    context = _logs(
        tmp_path,
        [
            (
                "Probe.\n",
                _turn(
                    "Done.",
                    _tool_event(2, "run_command"),
                    _tool_event(3, "read_file"),
                    _tool_event(5, "call_mcp_tool"),
                    _tool_event(6, "call_mcp_tool"),
                ),
            ),
            ("Continue.", _turn("Again.")),
        ],
        {
            ROOT: [
                _user(0, "Probe."),
                _planner(
                    1,
                    "Running.",
                    ("run_command", {"CommandLine": "ls x"}),
                    ("read_file", {"Path": "x"}),
                    thinking="Look first.",
                    input_tokens=6,
                    cache_read_tokens=4,
                    output_tokens=1,
                ),
                _result(2, "Exit code 2."),
                _result(3, "failed", status="ERROR", error="no such file"),
                _planner(
                    4,
                    "",
                    (
                        "call_mcp_tool",
                        {"ServerName": "probe", "ToolName": "echo", "Arguments": {"text": "hi"}},
                    ),
                    ("call_mcp_tool", snap),
                ),
                _result(5, "echo:hi"),
                _result(6, "a screenshot", media=[{"mime_type": "image/png", "uri": image}]),
                _planner(7, "Done."),
                _user(8, "Continue."),
                _messages(9, ("system", "Notice.")),
                _planner(10, "Again."),
            ]
        },
    )
    generated = _generated(context.logs_dir, ROOT)
    _write(generated / "logs/media/step_6_media_0.png", PNG)
    # agy writes the offloaded image twice; the second copy holds the same bytes.
    _write(generated / "steps/6/media_0.png", PNG)
    harness = AntigravityCliHarness()
    AutonomousHarnessConformance.check_parser_is_pure(harness, context)
    trajectory = harness.parse_trajectory(context)
    steps = trajectory.steps

    assert trajectory.session_id == ROOT
    assert [step.source for step in steps] == [
        "user",
        "agent",
        "agent",
        "agent",
        "user",
        "system",
        "agent",
    ]
    # The prompt is the one sent, not the record's wrapped copy of it.
    assert (steps[0].message, steps[4].message) == ("Probe.\n", "Continue.")
    assert "Notice." in str(steps[5].message)

    tools = steps[1]
    assert (tools.message, tools.reasoning_content) == ("Running.", "Look first.")
    assert [call.function_name for call in tools.tool_calls or ()] == ["run_command", "read_file"]
    assert tools.tool_calls[0].arguments == {"CommandLine": "ls x"}  # type: ignore[index]
    assert tools.metrics is not None
    assert (tools.metrics.prompt_tokens, tools.metrics.cached_tokens) == (10, 4)
    shell, missing = tools.observation.results  # type: ignore[union-attr]
    assert (shell.content, shell.extra) == ("Exit code 2.", None)
    assert missing.extra == {"ale": {"is_error": True, "error": "no such file"}}

    echo, screenshot = steps[2].tool_calls or ()
    assert (echo.function_name, echo.arguments) == ("mcp__probe__echo", {"text": "hi"})
    assert echo.extra == {
        "ale": {"mcp": {"server": "probe", "tool": "echo"}, "native_tool": "call_mcp_tool"}
    }
    results = {result.source_call_id: result for result in steps[2].observation.results}  # type: ignore[union-attr]
    assert results[echo.tool_call_id].content == "echo:hi"
    text, picture = results[screenshot.tool_call_id].content  # type: ignore[misc]
    assert (text.text, picture.source.media_type) == ("a screenshot", "image/png")  # type: ignore[union-attr]

    assert (steps[3].message, steps[6].message) == ("Done.", "Again.")
    assert trajectory.extra is None


SPAWN_RECORD = f"{BRAIN}/{ROOT}/.system_generated/subagents/{CHILD}.json"
CHILD_TRANSCRIPT = f"{BRAIN}/{CHILD}/.system_generated/logs/transcript_full.jsonl"


def _spawning(tmp_path: Path) -> TrajectoryParseContext:
    """The root's invoke_subagent result at step 2 spawned CHILD, which its record names."""
    context = _logs(
        tmp_path,
        [("Delegate.", _turn("Done."))],
        {
            ROOT: [
                _user(0, "Delegate."),
                _planner(1, "", ("invoke_subagent", {"Prompt": "Say hi."})),
                _result(2, "Started child."),
                _planner(3, "Done."),
            ],
            CHILD: [
                _messages(0, (ROOT, "Say hi.")),
                _planner(1, "hi"),
                _messages(2, ("task-1", "Timer fired.")),
                _messages(3, (ROOT, "Again.")),
                _planner(4, "hi again"),
            ],
        },
    )
    _write(
        context.logs_dir / SPAWN_RECORD,
        json.dumps({"conversationId": CHILD, "spawnStepIndex": 2}),
    )
    return context


def test_parser_embeds_a_subagent_where_it_was_spawned(tmp_path: Path) -> None:
    trajectory = AntigravityCliHarness().parse_trajectory(_spawning(tmp_path))

    spawn = trajectory.steps[1]
    (result,) = spawn.observation.results  # type: ignore[union-attr]
    (ref,) = result.subagent_trajectory_ref or ()
    (child,) = trajectory.subagent_trajectories or ()
    assert (ref.session_id, ref.trajectory_id) == (CHILD, child.trajectory_id)
    assert child.trajectory_id == f"trajectory-subagent-{CHILD}"
    # What the parent sent is the subagent's user turns; other senders' messages are not.
    assert [(step.source, step.message) for step in child.steps if step.source != "system"] == [
        ("user", "Say hi."),
        ("agent", "hi"),
        ("user", "Again."),
        ("agent", "hi again"),
    ]
    (notice,) = [step for step in child.steps if step.source == "system"]
    assert "Timer fired." in str(notice.message)
    assert trajectory.session_id == ROOT
    assert trajectory.extra is None


def _subagent_issues(context: TrajectoryParseContext) -> tuple[list[str], list[dict]]:
    trajectory = AntigravityCliHarness().parse_trajectory(context)
    issues = trajectory.extra["ale"]["parse_issues"]  # type: ignore[index]
    assert trajectory.subagent_trajectories is None
    assert not any(
        result.subagent_trajectory_ref
        for step in trajectory.steps
        for result in (step.observation.results if step.observation else ())
    )
    unlinked = [issue["source"] for issue in issues if issue["reason"] == "unlinked_subagent"]
    return unlinked, issues


def test_a_conversation_no_spawn_record_links_stays_raw_evidence(tmp_path: Path) -> None:
    context = _spawning(tmp_path)
    (context.logs_dir / SPAWN_RECORD).unlink()

    unlinked, issues = _subagent_issues(context)
    assert unlinked == [CHILD_TRANSCRIPT]
    (files,) = [issue for issue in issues if issue["reason"] == "unconverted_native_files"]
    assert CHILD_TRANSCRIPT in files["sources"]


def test_a_spawn_record_whose_step_has_no_result_links_nothing(tmp_path: Path) -> None:
    context = _spawning(tmp_path)
    _write(
        context.logs_dir / SPAWN_RECORD,
        json.dumps({"conversationId": CHILD, "spawnStepIndex": 99}),
    )

    unlinked, _ = _subagent_issues(context)
    assert unlinked == [CHILD_TRANSCRIPT]


def test_a_spawned_conversation_without_records_is_unlinked(tmp_path: Path) -> None:
    context = _spawning(tmp_path)
    (context.logs_dir / CHILD_TRANSCRIPT).write_text("")

    unlinked, _ = _subagent_issues(context)
    assert unlinked == [CHILD_TRANSCRIPT]


def test_a_spawn_record_naming_a_missing_conversation_is_unlinked(tmp_path: Path) -> None:
    context = _spawning(tmp_path)
    shutil.rmtree(context.logs_dir / BRAIN / CHILD)

    unlinked, issues = _subagent_issues(context)
    assert unlinked == [SPAWN_RECORD]
    (files,) = [issue for issue in issues if issue["reason"] == "unconverted_native_files"]
    assert files["sources"] == [SPAWN_RECORD]


DEFINITION = {"Name": "tester", "Description": "Tests.", "SystemPrompt": "Test things."}
RECORDS = [
    _user(0, "Run it."),
    _planner(1, "", ("schedule", {"DurationSeconds": 300}), ("define_subagent", DEFINITION)),
    _result(2, "Timer running in the background."),
    _result(3, "Defined tester."),
    _messages(4, ("system", "Notice.")),
    _planner(5, "Done."),
]


def _session(tmp_path: Path, *events: dict[str, Any]) -> TrajectoryParseContext:
    """One turn: a timer in the background, a subagent definition and a notice."""
    return _logs(tmp_path, [("Run it.", _turn("Done.", *events))], {ROOT: RECORDS})


def test_a_step_output_its_record_lacks_is_kept_on_the_result(tmp_path: Path) -> None:
    context = _session(tmp_path)
    # The timer was cancelled after the model received its running notice.
    _write(_generated(context.logs_dir, ROOT) / "steps/2/output.txt", "Wait cancelled.\n")
    trajectory = AntigravityCliHarness().parse_trajectory(context)

    timer = trajectory.steps[1].observation.results[0]  # type: ignore[union-attr]
    assert timer.content == "Timer running in the background."
    assert timer.extra == {"ale": {"final_output": "Wait cancelled.\n"}}
    assert trajectory.extra is None


def _database(path: Path, steps: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("create table steps (idx integer primary key, step_payload blob)")
    connection.executemany("insert into steps values (?, x'00')", [(i,) for i in range(steps)])
    connection.commit()
    connection.close()


@pytest.mark.parametrize(("steps", "consumed"), [(6, True), (7, False)])
def test_a_conversation_database_is_consumed_unless_it_holds_more_steps(
    tmp_path: Path, steps: int, consumed: bool
) -> None:
    """The records cover steps 0-5; a seventh is a turn the transcript lost."""
    context = _session(tmp_path)
    database = context.logs_dir / f".gemini/antigravity-cli/conversations/{ROOT}.db"
    _database(database, steps)
    database.with_name(database.name + "-shm").write_bytes(b"\0" * 8)
    snapshot = sorted((path, path.stat().st_mtime_ns) for path in database.parent.iterdir())

    extra = AntigravityCliHarness().parse_trajectory(context).extra
    assert sorted((path, path.stat().st_mtime_ns) for path in database.parent.iterdir()) == snapshot
    if consumed:
        assert extra is None
    else:
        (issue,) = extra["ale"]["parse_issues"]  # type: ignore[index]
        assert database.relative_to(context.logs_dir).as_posix() in issue["sources"]


#: The abridged transcript: arguments as JSON text, and a long field cut in the middle.
ABRIDGED = [
    RECORDS[0],
    {
        **RECORDS[1],
        "tool_calls": [
            {"name": "schedule", "args": {"DurationSeconds": "300"}},
            {"name": "define_subagent", "args": {k: json.dumps(v) for k, v in DEFINITION.items()}},
        ],
    },
    {
        **RECORDS[2],
        "content": "Timer running\n<truncated 8 bytes>\nbackground.",
        "truncated_fields": ["content"],
    },
    *RECORDS[3:],
]
OTHER = {**RECORDS[5], "content": "Not this."}


#: Each copy agy keeps, relative to its conversation: content the records hold, then
#: content of its own (``None`` where such content is the late output kept on a result).
COPIES = [
    (
        ".system_generated/logs/transcript.jsonl",
        _jsonl(ABRIDGED),
        _jsonl([*ABRIDGED[:5], OTHER]),
    ),
    (
        ".system_generated/logs/chunks/transcript/00000000.jsonl",
        _jsonl(ABRIDGED[1:3]),
        _jsonl([OTHER]),
    ),
    (
        ".system_generated/logs/chunks/transcript_full/00000000.jsonl",
        _jsonl(RECORDS[3:]),
        _jsonl([OTHER]),
    ),
    (".system_generated/steps/3/output.txt", "Defined tester.\n", None),
    (
        ".system_generated/messages/m1.json",
        '{"content": "Notice."}',
        '{"content": "Never sent."}',
    ),
    (".system_generated/messages/read.json", '{"m1": true}', '{"m2": true}'),
    (".system_generated/tasks/task-1.log", "Timer running in the background.", "Timer fired."),
    (".agents/agents/tester/agent.md", "Tests.\n\nTest things.\n", "Tests.\n\nOther.\n"),
]


@pytest.mark.parametrize(
    ("relative", "covered", "uncovered"), COPIES, ids=[copy[0] for copy in COPIES]
)
def test_a_copy_is_consumed_only_while_the_records_hold_its_content(
    tmp_path: Path, relative: str, covered: str, uncovered: str | None
) -> None:
    """agy keeps copies beside each full transcript; one with content of its own stays."""
    context = _session(tmp_path)
    generated = _generated(context.logs_dir, ROOT)
    # The abridged transcript and a delivered message with its read receipt, as agy leaves them.
    _write(generated / "logs/transcript.jsonl", _jsonl(ABRIDGED))
    _write(generated / "messages/m1.json", '{"content": "Notice."}')
    _write(generated / "messages/read.json", '{"m1": true}')
    path = context.logs_dir / BRAIN / ROOT / relative
    harness = AntigravityCliHarness()

    _write(path, covered)
    assert harness.parse_trajectory(context).extra is None
    if uncovered is None:
        return
    _write(path, uncovered)
    (issue,) = harness.parse_trajectory(context).extra["ale"]["parse_issues"]  # type: ignore[index]
    assert issue["reason"] == "unconverted_native_files"
    assert path.relative_to(context.logs_dir).as_posix() in issue["sources"]


def test_parser_keeps_retries_and_the_stdout_failure(tmp_path: Path) -> None:
    failure = "agent executor error: Error 429"
    context = _logs(
        tmp_path,
        [("Go.", _turn("", status="ERROR", error=failure))],
        {
            ROOT: [
                _user(0, "Go."),
                {"step_index": 1, "type": "ERROR_MESSAGE", "error": "API error (attempt 1)"},
                {"step_index": 2, "type": "ERROR_MESSAGE", "error": "API error (attempt 2)"},
            ]
        },
    )
    steps = AntigravityCliHarness().parse_trajectory(context).steps

    assert [(step.source, step.message) for step in steps] == [
        ("user", "Go."),
        ("system", "API error (attempt 1)"),
        ("system", "API error (attempt 2)"),
        ("system", failure),
    ]


def test_without_the_native_session_each_turn_comes_from_stdout(tmp_path: Path) -> None:
    context = _logs(tmp_path, [("First.", _turn("One.\n")), ("Second.", _turn("Two."))])
    trajectory = AntigravityCliHarness().parse_trajectory(context)

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "First."),
        ("agent", "One."),
        ("user", "Second."),
        ("agent", "Two."),
    ]
    assert trajectory.extra["ale"]["parse_issues"][0]["reason"] == "native_session_unavailable"  # type: ignore[index]


def test_a_result_the_stdout_attributes_to_another_tool_is_an_issue(tmp_path: Path) -> None:
    context = _session(tmp_path, _tool_event(2, "x"), _tool_event(3, "define_subagent"))
    issues = AntigravityCliHarness().parse_trajectory(context).extra["ale"]["parse_issues"]  # type: ignore[index]

    assert issues == [
        {"reason": "tool_result_mismatch", "step_index": 2, "expected": "schedule", "observed": "x"}
    ]
