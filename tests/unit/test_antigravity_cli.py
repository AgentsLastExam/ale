from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

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

FIXTURES = Path(__file__).parents[1] / "fixtures" / "harness-logs"
SETTINGS = "/home/agent/.gemini/antigravity-cli/settings.json"
MCP_CONFIG = "/home/agent/.gemini/config/mcp_config.json"
SESSION_ID = "2b28aae6-2b4d-4d47-a581-0a14d5f91707"
PARENT_ID = "f3387877-ab62-478a-b9e2-a4af4aba51e4"
CHILD_ID = "6c8e1c33-c3f8-4fb8-9635-eb94f741963e"
MODEL = "gemini-3.8-flash"


def test_preset_is_complete_and_selects_the_gemini_dialect() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "antigravity-cli.toml")
    harness = _harness(config)

    assert isinstance(harness, AntigravityCliHarness)
    assert config.gateway.dialect == "gemini"
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


@pytest.mark.parametrize("native_session_id", [None, SESSION_ID])
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
    assert argv[11:] == ([] if native_session_id is None else ["--conversation", SESSION_ID])
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
    brain = f".gemini/antigravity-cli/brain/{SESSION_ID}"
    assert harness.is_session_file(PurePosixPath(f"{brain}/.system_generated/x.jsonl"), SESSION_ID)
    assert harness.is_session_file(
        PurePosixPath(f".gemini/antigravity-cli/conversations/{SESSION_ID}.db"), SESSION_ID
    )
    assert not harness.is_session_file(PurePosixPath(brain), SESSION_ID)
    assert not harness.is_session_file(PurePosixPath(f"{brain}x/a.json"), SESSION_ID)
    assert not harness.is_session_file(
        PurePosixPath(f".gemini/antigravity-cli/conversations/{SESSION_ID}.db-wal"), SESSION_ID
    )


def _context(tmp_path: Path, fixture: str, **updates: object) -> TrajectoryParseContext:
    logs = tmp_path / "logs"
    shutil.copytree(FIXTURES / fixture, logs)
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "trajectory",
        "instruction": "unused: the invocation record names the prompt",
        "logs_dir": logs,
        "model": MODEL,
        "agent_version": "1.4.7",
        "blobs": BlobStore(tmp_path / "blobs"),
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def test_parser_follows_the_native_session_through_tools_mcp_images_and_resume(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, "antigravity-cli-session")
    harness = AntigravityCliHarness()
    AutonomousHarnessConformance.check_parser_is_pure(harness, context)
    trajectory = harness.parse_trajectory(context)
    steps = trajectory.steps

    assert trajectory.session_id == SESSION_ID
    assert [step.source for step in steps] == [
        "user",
        "agent",
        "agent",
        "agent",
        "user",
        "system",
        "agent",
    ]
    assert steps[0].message == "Use the probe MCP.\nThen report.\n"
    assert steps[4].message == "Continue, please."

    tools = steps[1]
    assert tools.message == "Running tools now."
    assert tools.reasoning_content == "I should run a command and read a file."
    assert [call.function_name for call in tools.tool_calls or ()] == ["run_command", "read_file"]
    assert tools.tool_calls[0].arguments["CommandLine"] == "echo hi; ls /nonexistent"
    assert tools.metrics is not None
    assert (tools.metrics.prompt_tokens, tools.metrics.cached_tokens) == (100, 40)
    shell, missing = tools.observation.results  # type: ignore[union-attr]
    assert "Command exited with code 2." in str(shell.content)
    assert shell.extra is None
    assert missing.extra == {"ale": {"is_error": True, "error": "file does not exist"}}

    mcp = steps[2]
    echo, snap = mcp.tool_calls or ()
    assert (echo.function_name, echo.arguments) == ("mcp__probe__echo", {"text": "hi"})
    assert echo.extra == {
        "ale": {"mcp": {"server": "probe", "tool": "echo"}, "native_tool": "call_mcp_tool"}
    }
    results = {result.source_call_id: result for result in mcp.observation.results}  # type: ignore[union-attr]
    assert str(results[echo.tool_call_id].content).endswith("echo:hi")
    image = [part for part in results[snap.tool_call_id].content if part.type == "image"]  # type: ignore[union-attr]
    assert len(image) == 1 and image[0].source.media_type == "image/png"  # type: ignore[union-attr]

    assert steps[3].message == "All DONE."
    assert steps[6].message == "Second turn done."
    issues = trajectory.extra["ale"]["parse_issues"]  # type: ignore[index]
    assert [issue["reason"] for issue in issues] == ["unconverted_native_files"]


def test_parser_embeds_a_subagent_where_it_was_spawned(tmp_path: Path) -> None:
    trajectory = AntigravityCliHarness().parse_trajectory(
        _context(tmp_path, "antigravity-cli-subagent")
    )

    spawn = trajectory.steps[1]
    assert [call.function_name for call in spawn.tool_calls or ()] == ["invoke_subagent"]
    (result,) = spawn.observation.results  # type: ignore[union-attr]
    (ref,) = result.subagent_trajectory_ref or ()
    assert ref.session_id == CHILD_ID
    (child,) = trajectory.subagent_trajectories or ()
    assert child.trajectory_id == ref.trajectory_id == f"trajectory-subagent-{CHILD_ID}"
    assert [step.source for step in child.steps] == ["system", "agent"]
    assert child.steps[1].message == "sub done"
    assert trajectory.session_id == PARENT_ID


def test_parser_keeps_retries_and_the_stdout_failure(tmp_path: Path) -> None:
    steps = (
        AntigravityCliHarness()
        .parse_trajectory(_context(tmp_path, "antigravity-cli-failure"))
        .steps
    )

    assert steps[0].source == "user"
    assert all(step.source == "system" for step in steps[1:])
    assert steps[1].message.startswith("API error (attempt 1): Error 429")
    assert steps[-1].message.startswith("agent executor error")


def test_without_the_native_session_each_turn_comes_from_stdout(tmp_path: Path) -> None:
    context = _context(tmp_path, "antigravity-cli-session")
    shutil.rmtree(context.logs_dir / ".gemini")
    trajectory = AntigravityCliHarness().parse_trajectory(context)

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Use the probe MCP.\nThen report.\n"),
        ("agent", "Running tools now.\nAll DONE."),
        ("user", "Continue, please."),
        ("agent", "Second turn done."),
    ]
    assert trajectory.extra["ale"]["parse_issues"][0]["reason"] == "native_session_unavailable"  # type: ignore[index]


def test_a_result_the_stdout_attributes_to_another_tool_is_an_issue(tmp_path: Path) -> None:
    context = _context(tmp_path, "antigravity-cli-session")
    stdout = context.logs_dir / ".ale-harness" / "stdout.log"
    stdout.write_text(stdout.read_text().replace('"tool_name":"read_file"', '"tool_name":"x"'))
    issues = AntigravityCliHarness().parse_trajectory(context).extra["ale"]["parse_issues"]  # type: ignore[index]

    assert {
        "reason": "tool_result_mismatch",
        "step_index": 3,
        "expected": "read_file",
        "observed": "x",
    } in issues
