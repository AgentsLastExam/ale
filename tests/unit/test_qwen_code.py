from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath

import pytest
from tests.unit.test_cli_harnesses import FakeSandbox, resources, session

from ale.core.config import load_run_config
from ale.core.errors import AgentError, ConfigError, HarnessLimitError
from ale.core.harness import EffectiveAgentResources, ResolvedMcpServer, TrajectoryParseContext
from ale.core.taskspec import StreamableHttpMcpServer
from ale.core.testkit import AutonomousHarnessConformance
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.harnesses.qwen_code import QwenCodeHarness
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parents[1] / "fixtures" / "harness-logs" / "qwen-code-subagent"
SESSION_ID = "a47044d0-0034-4676-be60-b837055bbad4"
PROJECT = ".qwen-ale/projects/-home-user"
CHAT = f"{PROJECT}/chats/{SESSION_ID}.jsonl"
CHILD = f"{PROJECT}/subagents/{SESSION_ID}/agent-general-purpose-call_2"
SETTINGS = "/home/agent/.qwen-ale/system-settings.json"


def test_preset_is_complete_and_uses_chat_completions() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "qwen-code.toml")
    harness = _harness(config)

    assert isinstance(harness, QwenCodeHarness)
    assert harness.gateway_dialect == "openai-chat-completions"
    assert config.agent.version == harness.default_version
    assert set(config.agent.settings) == set(harness.settings_type.model_fields)


@pytest.mark.parametrize(
    "settings",
    [
        {"max_turns": 3},
        {"max_session_turns": 0},
        {"max_session_turns": -1},
        {"max_session_turns": False},
        {"reasoning_effort": "ultra"},
        {"context_window_size": 0},
        {"modalities": ["vision"]},
        {"modalities": ["image", "image"]},
    ],
)
def test_settings_are_strict(settings: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        QwenCodeHarness(settings=settings)


def test_unknown_gateway_dialect_fails() -> None:
    with pytest.raises(ConfigError, match="gateway dialect"):
        QwenCodeHarness(gateway_dialect="gemini")


@pytest.mark.parametrize(
    ("dialect", "protocol", "provider", "base_url", "wire_api"),
    [
        ("openai-chat-completions", "openai", "openai", "http://gateway/v1", None),
        ("openai-responses", "openai-responses", "openai", "http://gateway/v1", "responses"),
        ("anthropic", "anthropic", "anthropic", "http://gateway", None),
    ],
)
async def test_system_settings_route_the_model_through_the_gateway(
    tmp_path: Path,
    dialect: str,
    protocol: str,
    provider: str,
    base_url: str,
    wire_api: str | None,
) -> None:
    sandbox = FakeSandbox()
    harness = QwenCodeHarness(gateway_dialect=dialect)
    effective = resources(tmp_path)

    harness.validate_resources(effective)
    await harness.install_resources(sandbox, session(), effective)  # type: ignore[arg-type]

    rendered = sandbox.files[SETTINGS].decode()
    settings = json.loads(rendered)
    assert settings["security"]["auth"] == {"selectedType": protocol, "enforcedType": protocol}
    route = {"id": "test-model", "envKey": "ALE_GATEWAY_TOKEN", "baseUrl": base_url}
    if wire_api:
        route["wireApi"] = wire_api
    assert settings["modelProviders"] == {provider: [route]}
    assert settings["model"] == {"name": "test-model", "maxSessionTurns": -1}
    assert settings["mcpServers"] == {
        "task-proof": {
            "command": "python3",
            "args": ["/home/agent/server.py"],
            "env": {"PROOF_HOME": "/home/agent"},
        }
    }
    assert settings["privacy"] == {"usageStatisticsEnabled": False}
    assert settings["telemetry"] == {"enabled": False}
    assert "token" not in rendered.replace("TOKEN", "")
    assert sandbox.files["/home/agent/.qwen-ale/system-defaults.json"] == b"{}\n"
    assert sandbox.uploads == [
        (str(effective.skills[0].path), "/home/agent/.qwen-ale/skills/resource-proof")
    ]


async def test_native_controls_translate_totally() -> None:
    sandbox = FakeSandbox()
    harness = QwenCodeHarness(
        settings={
            "max_session_turns": 30,
            "reasoning_effort": "high",
            "context_window_size": 200000,
            "modalities": ["image", "pdf"],
        }
    )
    await harness.install_resources(sandbox, session(), EffectiveAgentResources())  # type: ignore[arg-type]
    settings = json.loads(sandbox.files[SETTINGS])
    assert settings["model"] == {
        "name": "test-model",
        "maxSessionTurns": 30,
        "reasoningEffort": "high",
    }
    assert settings["modelProviders"]["openai"][0]["generationConfig"] == {
        "contextWindowSize": 200000,
        "modalities": {"image": True, "pdf": True},
    }

    sandbox = FakeSandbox()
    await QwenCodeHarness(settings={"max_session_turns": "default"}).install_resources(
        sandbox,  # type: ignore[arg-type]
        session(),
        EffectiveAgentResources(),
    )
    settings = json.loads(sandbox.files[SETTINGS])
    assert settings["model"] == {"name": "test-model"}
    assert "generationConfig" not in settings["modelProviders"]["openai"][0]


async def test_http_mcp_servers_use_the_streamable_http_field() -> None:
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
    await QwenCodeHarness().install_resources(
        sandbox,  # type: ignore[arg-type]
        session(),
        EffectiveAgentResources(mcp_servers=(remote,), digest="sha256:" + "f" * 64),
    )
    settings = json.loads(sandbox.files[SETTINGS])
    assert settings["mcpServers"] == {"remote": {"httpUrl": "https://mcp.example/mcp"}}


async def test_command_reads_the_prompt_from_stdin_and_names_its_session() -> None:
    harness = QwenCodeHarness()
    launch = await harness.prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=None,
    )
    assert launch.session_id is not None
    assert f"--session-id {launch.session_id}" in launch.script
    assert launch.script.endswith(" < /home/agent/.ale-harness/prompt.md")
    assert launch.script.startswith("unset OPENAI_API_KEY OPENAI_BASE_URL ")
    assert "--approval-mode yolo" in launch.script
    assert launch.env["QWEN_HOME"] == "/home/agent/.qwen-ale"
    assert launch.env["QWEN_RUNTIME_DIR"] == "/home/agent/.qwen-ale"
    assert launch.env["QWEN_CODE_SYSTEM_SETTINGS_PATH"] == SETTINGS
    assert launch.env["QWEN_CODE_MODELS_DEV_REFRESH"] == "off"
    assert launch.env["QWEN_CODE_NO_RELAUNCH"] == "1"
    assert launch.env["ALE_GATEWAY_TOKEN"] == "token"

    resume = await harness.prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=SESSION_ID,
    )
    assert resume.session_id == SESSION_ID
    assert f"--resume={SESSION_ID}" in resume.script
    assert "--session-id" not in resume.script


async def test_windows_command_uses_native_paths() -> None:
    command = await QwenCodeHarness().prepare_command(
        "C:/Users/Agent/.ale-harness/prompt.md",
        FakeSandbox(stdout=r"C:\Windows\System32"),  # type: ignore[arg-type]
        session(home=r"C:\Users\Agent"),
        native_session_id=None,
    )
    assert command.env["QWEN_HOME"] == "C:/Users/Agent/.qwen-ale"
    assert command.env["HOME"] == "C:/Users/Agent"
    assert command.script.endswith(" < C:/Users/Agent/.ale-harness/prompt.md")


def test_result_reading_uses_the_terminal_result() -> None:
    stdout = (FIXTURE / ".ale-harness/stdout.log").read_text()
    result = QwenCodeHarness().read_result(stdout)
    assert result.session_id == SESSION_ID
    assert result.final_message == "SECOND-DONE"
    assert result.error is None
    failed = QwenCodeHarness().read_result(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "session_id": SESSION_ID,
                "is_error": True,
                "error": {"message": "[API Error: 400 refused]"},
            }
        )
        + "\n"
    )
    assert failed.error == "[API Error: 400 refused]"
    assert failed.final_message is None


@pytest.mark.parametrize(
    ("settings", "error"),
    [({}, AgentError), ({"max_session_turns": 2}, HarnessLimitError)],
)
async def test_native_failures_keep_the_cause(settings: dict[str, object], error: type) -> None:
    sandbox = FakeSandbox(53)
    sandbox.native_stderr = (
        b"Reached max session turns for this session. Increase the number of turns token\n"
    )
    with pytest.raises(error) as caught:
        await QwenCodeHarness(settings=settings).launch(
            "work",
            sandbox,  # type: ignore[arg-type]
            session(),
            timeout_sec=60,
        )
    if error is AgentError:
        assert "exit code 53" in str(caught.value)
        assert "token" not in str(caught.value)
    else:
        assert caught.value.limit == "max_session_turns"


def test_session_files_match_only_the_exact_session() -> None:
    harness = QwenCodeHarness()
    assert harness.is_session_file(PurePosixPath(CHAT), SESSION_ID)
    assert harness.is_session_file(PurePosixPath(f"{CHILD}.jsonl"), SESSION_ID)
    assert not harness.is_session_file(PurePosixPath(CHAT), SESSION_ID[:-1])
    assert not harness.is_session_file(PurePosixPath(f"{PROJECT}/meta.json"), SESSION_ID)
    assert not harness.is_session_file(
        PurePosixPath(f"{PROJECT}/chats/x/{SESSION_ID}.jsonl"), SESSION_ID
    )


def context(logs: Path, tmp_path: Path, **updates: object) -> TrajectoryParseContext:
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "trajectory",
        "instruction": "Delegate, then look at large.png",
        "logs_dir": logs,
        "model": "scripted-model",
        "agent_version": "0.26.1",
        "blobs": BlobStore(tmp_path / "episode"),
        "session_id": SESSION_ID,
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def copy_fixture(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    shutil.copytree(FIXTURE, logs)
    return logs


def test_parser_follows_the_session_and_embeds_its_subagent(tmp_path: Path) -> None:
    parse = context(FIXTURE, tmp_path)
    AutonomousHarnessConformance.check_parser_is_pure(QwenCodeHarness(), parse)
    trajectory = QwenCodeHarness().parse_trajectory(parse)

    assert trajectory.session_id == SESSION_ID
    assert trajectory.extra is None
    # Qwen appends a blank line to a prompt read from stdin; the turn is the prompt sent.
    assert [(step.source, step.message) for step in trajectory.steps if not step.tool_calls] == [
        ("user", "Delegate, then look at large.png"),
        ("agent", "PARENT-DONE"),
        ("user", "second"),
        ("agent", "SECOND-DONE"),
    ]
    spawn = trajectory.steps[2]
    (call,) = spawn.tool_calls or ()
    assert call.function_name == "agent"
    assert call.extra == {"ale": {"native_tool": "tool_call"}}
    assert call.arguments["subagent_type"] == "general-purpose"
    (result,) = spawn.observation.results  # type: ignore[union-attr]
    assert result.content == "CHILD-DONE"
    assert result.subagent_trajectory_ref is not None
    (child,) = trajectory.subagent_trajectories or ()
    assert result.subagent_trajectory_ref[0].trajectory_id == child.trajectory_id
    assert child.trajectory_id == "trajectory-subagent-general-purpose-call_2"
    assert [step.source for step in child.steps] == ["user", "agent", "agent"]
    assert "child-proof" in str(child.steps[1].observation.results[0].content)  # type: ignore[union-attr]

    (image_result,) = trajectory.steps[3].observation.results  # type: ignore[union-attr]
    assert isinstance(image_result.content, list)
    image = image_result.content[1]
    assert image.type == "image" and image.source is not None
    assert (tmp_path / "episode" / image.source.path).is_file()
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 55


def test_mcp_calls_through_the_deferred_tool_wrapper_keep_server_and_tool(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    chat = logs / CHAT
    chat.parent.mkdir(parents=True)
    records = [
        {"type": "user", "provenance": "real_user", "message": {"parts": [{"text": "go\n\n"}]}},
        {
            "type": "assistant",
            "message": {
                "parts": [
                    {
                        "functionCall": {
                            "id": "call_9",
                            "name": "tool_call",
                            "args": {"name": "mcp__proof__echo", "arguments": {"text": "hi"}},
                        }
                    }
                ]
            },
        },
        {
            "type": "tool_result",
            "toolCallResult": {"status": "error"},
            "message": {
                "parts": [
                    {
                        "functionResponse": {
                            "id": "call_9",
                            "name": "tool_call",
                            "response": {"output": "server unavailable"},
                        }
                    }
                ]
            },
        },
    ]
    chat.write_text("".join(json.dumps(record) + "\n" for record in records))

    trajectory = QwenCodeHarness().parse_trajectory(context(logs, tmp_path, instruction="go"))

    (call,) = trajectory.steps[1].tool_calls or ()
    assert call.function_name == "mcp__proof__echo"
    assert call.arguments == {"text": "hi"}
    assert call.extra == {
        "ale": {"native_tool": "tool_call", "mcp": {"server": "proof", "tool": "echo"}}
    }
    (result,) = trajectory.steps[1].observation.results  # type: ignore[union-attr]
    assert result.content == "server unavailable"
    assert result.extra == {"ale": {"is_error": True}}


def test_a_subagent_spawned_by_a_subagent_is_embedded_in_it(tmp_path: Path) -> None:
    logs = copy_fixture(tmp_path)
    child_id, nested_id = "general-purpose-call_2", "general-purpose-call_9"
    nested = f"{PROJECT}/subagents/{SESSION_ID}/agent-{nested_id}"
    child = logs / f"{CHILD}.jsonl"
    (logs / f"{nested}.jsonl").write_text(child.read_text().replace(child_id, nested_id))
    meta = json.loads((logs / f"{CHILD}.meta.json").read_text())
    (logs / f"{nested}.meta.json").write_text(
        json.dumps(meta | {"agentId": nested_id, "toolUseId": "call_9", "parentAgentId": child_id})
    )
    # The child spawns as the session did: the call, then its result.
    spawn = [
        line.replace("call_2", "call_9")
        for line in (logs / CHAT).read_text().splitlines(keepends=True)
        if '"id":"call_2"' in line
    ]
    assert len(spawn) == 2
    with child.open("a") as stream:
        stream.writelines(spawn)

    trajectory = QwenCodeHarness().parse_trajectory(context(logs, tmp_path, incomplete=True))

    assert "parse_issues" not in trajectory.extra["ale"]  # type: ignore[index]
    (helper,) = trajectory.subagent_trajectories or ()
    (inner,) = helper.subagent_trajectories or ()
    assert inner.trajectory_id == f"trajectory-subagent-{child_id}-subagent-{nested_id}"
    spawned = next(
        step
        for step in helper.steps
        if [call.tool_call_id for call in step.tool_calls or ()] == ["call_9"]
    )
    (result,) = spawned.observation.results  # type: ignore[union-attr]
    assert result.subagent_trajectory_ref is not None
    assert result.subagent_trajectory_ref[0].trajectory_id == inner.trajectory_id
    # An interrupted run leaves every agent in it incomplete.
    for subagent in (helper, inner):
        assert subagent.extra == {"ale": {"incomplete": True, "incomplete_reason": "interrupted"}}


def test_unlinked_subagents_stay_raw_evidence(tmp_path: Path) -> None:
    logs = copy_fixture(tmp_path)
    meta = logs / f"{CHILD}.meta.json"
    data = json.loads(meta.read_text())
    meta.write_text(json.dumps({**data, "toolUseId": "call_missing"}))

    trajectory = QwenCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert trajectory.subagent_trajectories is None
    issues = trajectory.extra["ale"]["parse_issues"]  # type: ignore[index]
    assert {"reason": "unlinked_subagent", "source": f"{CHILD}.jsonl"} in issues
    assert any(
        issue["reason"] == "unconverted_native_files" and f"{CHILD}.jsonl" in issue["sources"]
        for issue in issues
    )


def test_a_failed_turn_is_recorded_at_its_end(tmp_path: Path) -> None:
    logs = copy_fixture(tmp_path)
    stdout = logs / ".ale-harness/stdout.log"
    lines = stdout.read_text().splitlines()
    index = max(i for i, line in enumerate(lines) if '"type":"result"' in line)
    failed = json.loads(lines[index]) | {
        "subtype": "error_during_execution",
        "is_error": True,
        "error": {"message": "[API Error: 400 refused]"},
    }
    lines[index] = json.dumps(failed)
    stdout.write_text("\n".join(lines) + "\n")

    trajectory = QwenCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert (trajectory.steps[-1].source, trajectory.steps[-1].message) == (
        "system",
        "[API Error: 400 refused]",
    )


def test_without_the_session_each_turn_comes_from_its_prompt_and_stdout(tmp_path: Path) -> None:
    logs = copy_fixture(tmp_path)
    shutil.rmtree(logs / ".qwen-ale")

    trajectory = QwenCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Delegate, then look at large.png"),
        ("agent", "PARENT-DONE"),
        ("user", "second"),
        ("agent", "SECOND-DONE"),
    ]
    ale = trajectory.extra["ale"]  # type: ignore[index]
    assert {"reason": "native_session_unavailable", "session_id": SESSION_ID} in ale["parse_issues"]
