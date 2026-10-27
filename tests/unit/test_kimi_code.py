from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
from tests.unit.test_cli_harnesses import FakeSandbox, resources, session

from ale.core.config import load_run_config
from ale.core.errors import AgentError, ConfigError, HarnessLimitError
from ale.core.harness import EffectiveAgentResources, ResolvedMcpServer, TrajectoryParseContext
from ale.core.taskspec import StreamableHttpMcpServer
from ale.core.testkit import AutonomousHarnessConformance
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.harnesses.kimi_code import KimiCodeHarness
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit

SESSION_ID = "session-1"
SESSION_DIR = f".kimi-ale/sessions/wd-1/{SESSION_ID}"
CONFIG = "/home/agent/.kimi-ale/config.toml"
PROMPTS = ("delegate", "second")


def test_preset_is_complete_and_selects_the_native_chat_protocol() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "kimi-code.toml")
    harness = _harness(config)

    assert isinstance(harness, KimiCodeHarness)
    assert harness.gateway_dialect == "openai-chat-completions"
    assert config.agent.version == harness.default_version
    assert set(config.agent.settings) == set(harness.settings_type.model_fields)


@pytest.mark.parametrize(
    "settings",
    [
        {"max_steps": 3},
        {"max_steps_per_turn": 0},
        {"max_steps_per_turn": -1},
        {"max_steps_per_turn": False},
        {"max_context_size": 0},
        {"max_context_size": "default"},
        {"thinking_effort": "high"},
        {"thinking_effort": "ultra", "capabilities": ["thinking"]},
        {"capabilities": ["vision"]},
        {"capabilities": ["thinking", "thinking"]},
    ],
)
def test_settings_are_strict(settings: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        KimiCodeHarness(settings=settings)


def test_unknown_gateway_dialect_fails() -> None:
    with pytest.raises(ConfigError, match="gateway dialect"):
        KimiCodeHarness(gateway_dialect="gemini")


@pytest.mark.parametrize(
    ("dialect", "provider", "base_url"),
    [
        ("openai-chat-completions", "kimi", "http://gateway/v1"),
        ("anthropic", "anthropic", "http://gateway"),
        ("openai-responses", "openai_responses", "http://gateway/v1"),
    ],
)
async def test_configuration_is_episode_local_and_uses_the_gateway(
    tmp_path: Path, dialect: str, provider: str, base_url: str
) -> None:
    sandbox = FakeSandbox()
    harness = KimiCodeHarness(gateway_dialect=dialect)
    effective = resources(tmp_path)

    harness.validate_resources(effective)
    await harness.install_resources(sandbox, session(), effective)  # type: ignore[arg-type]

    config = sandbox.files[CONFIG].decode()
    assert f'type = "{provider}"' in config
    assert f'base_url = "{base_url}"' in config
    assert 'api_key_env = "ALE_GATEWAY_TOKEN"' in config
    assert 'default_model = "test-model"' in config
    assert '[models."test-model"]' in config
    assert "telemetry = false" in config
    assert "max_steps_per_turn = 0" in config
    assert "[thinking]" not in config
    assert "token" not in config.replace("TOKEN", "")
    assert sandbox.uploads == [
        (str(effective.skills[0].path), "/home/agent/.kimi-ale/skills/resource-proof")
    ]
    servers = json.loads(sandbox.files["/home/agent/.kimi-ale/mcp.json"])["mcpServers"]
    assert servers == {
        "task-proof": {
            "command": "python3",
            "args": ["/home/agent/server.py"],
            "env": {"PROOF_HOME": "/home/agent"},
        }
    }


async def test_native_controls_translate_totally() -> None:
    sandbox = FakeSandbox()
    harness = KimiCodeHarness(
        settings={
            "max_context_size": 1048576,
            "capabilities": ["image_in", "thinking"],
            "thinking_effort": "max",
            "max_steps_per_turn": 40,
        }
    )
    await harness.install_resources(sandbox, session(), EffectiveAgentResources())  # type: ignore[arg-type]
    config = sandbox.files[CONFIG].decode()
    assert "max_context_size = 1048576" in config
    assert 'capabilities = ["image_in", "thinking"]' in config
    assert '[thinking]\nenabled = true\neffort = "max"' in config
    assert "max_steps_per_turn = 40" in config

    sandbox = FakeSandbox()
    harness = KimiCodeHarness(settings={"thinking_effort": "off", "max_steps_per_turn": "default"})
    await harness.install_resources(sandbox, session(), EffectiveAgentResources())  # type: ignore[arg-type]
    config = sandbox.files[CONFIG].decode()
    assert "[thinking]\nenabled = false" in config
    assert "loop_control" not in config


async def test_http_mcp_servers_use_the_native_url_form(tmp_path: Path) -> None:
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
    await KimiCodeHarness().install_resources(
        sandbox,  # type: ignore[arg-type]
        session(),
        EffectiveAgentResources(mcp_servers=(remote,), digest="sha256:" + "f" * 64),
    )
    servers = json.loads(sandbox.files["/home/agent/.kimi-ale/mcp.json"])["mcpServers"]
    assert servers == {"remote": {"url": "https://mcp.example/mcp"}}


async def test_command_isolates_discovery_and_resumes_the_exact_session() -> None:
    harness = KimiCodeHarness()
    launch = await harness.prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=None,
    )
    assert launch.session_id is None
    assert "--skills-dir /home/agent/.kimi-ale/skills" in launch.script
    assert "--output-format stream-json" in launch.script
    assert "--session" not in launch.script
    assert launch.script.startswith("unset KIMI_MODEL_NAME ")
    assert launch.env["KIMI_CODE_HOME"] == "/home/agent/.kimi-ale"
    assert launch.env["ALE_GATEWAY_TOKEN"] == "token"
    assert launch.env["KIMI_CODE_NO_AUTO_UPDATE"] == "1"
    assert "MSYS2_ARG_CONV_EXCL" not in launch.env

    resume = await harness.prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=SESSION_ID,
    )
    assert resume.session_id == SESSION_ID
    assert f"--session {SESSION_ID}" in resume.script


async def test_windows_command_keeps_git_bash_from_rewriting_the_prompt() -> None:
    command = await KimiCodeHarness().prepare_command(
        "C:/Users/Agent/.ale-harness/prompt.md",
        FakeSandbox(stdout=r"C:\Windows\System32"),  # type: ignore[arg-type]
        session(home=r"C:\Users\Agent"),
        native_session_id=None,
    )
    assert command.env["MSYS2_ARG_CONV_EXCL"] == "*"
    assert command.env["KIMI_SHELL_PATH"] == "C:/Program Files/Git/bin/bash.exe"
    assert command.env["KIMI_CODE_HOME"] == "C:/Users/Agent/.kimi-ale"


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
async def test_the_prompt_file_reaches_the_cli_as_its_exact_bytes(tmp_path: Path) -> None:
    prompt = "Line one: 'quoted' \"double\" $HOME `cmd` \\ 多行\n--flag-like\n\n\n"
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "kimi"
    fake.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    command = await KimiCodeHarness().prepare_command(
        str(prompt_path),
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=None,
    )
    completed = subprocess.run(
        ["sh", "-c", command.script],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    argv = json.loads(completed.stdout)
    assert argv[-1] == f"--prompt={prompt}"


def stdout(reply: str) -> str:
    """One invocation's stdout: its reply, then the hint naming the session to resume."""
    events = [
        {"role": "assistant", "content": reply},
        {"role": "meta", "type": "session.resume_hint", "session_id": SESSION_ID},
    ]
    return "".join(json.dumps(event) + "\n" for event in events)


def test_result_reading_uses_the_resume_hint_and_last_reply() -> None:
    result = KimiCodeHarness().read_result(stdout("PARENT-DONE") + stdout("SECOND-DONE"))
    assert result.session_id == SESSION_ID
    assert result.final_message == "SECOND-DONE"
    assert result.error is None
    occupied = KimiCodeHarness().read_result(
        '{"type":"error","code":"session.occupied","message":"held by another client"}\n'
    )
    assert occupied.error == "held by another client; session.occupied"
    assert occupied.session_id is None


@pytest.mark.parametrize(
    ("settings", "error"),
    [({}, AgentError), ({"max_steps_per_turn": 5}, HarnessLimitError)],
)
async def test_native_failures_keep_the_cause(settings: dict[str, object], error: type) -> None:
    sandbox = FakeSandbox(1)
    sandbox.native_stdout = b'{"role":"meta","type":"system.version","version":"2.3.2"}\n'
    sandbox.native_stderr = (
        b"error: failed to run prompt: loop.max_steps_exceeded: Turn exceeded maxSteps=5 token\n"
    )
    with pytest.raises(error) as caught:
        await KimiCodeHarness(settings=settings).launch(
            "work",
            sandbox,  # type: ignore[arg-type]
            session(),
            timeout_sec=60,
        )
    if error is AgentError:
        assert "loop.max_steps_exceeded" in str(caught.value)
        assert "exit code 1" in str(caught.value)
        assert "token" not in str(caught.value)
    else:
        assert caught.value.limit == "max_steps_per_turn"


def test_session_files_match_only_the_exact_session() -> None:
    harness = KimiCodeHarness()
    assert harness.is_session_file(PurePosixPath(SESSION_DIR, "agents/main/wire.jsonl"), SESSION_ID)
    assert harness.is_session_file(PurePosixPath(SESSION_DIR, "state.json"), SESSION_ID)
    assert not harness.is_session_file(
        PurePosixPath(".kimi-ale/sessions/.index-dirty", f"{SESSION_ID}.1792989075328"),
        SESSION_ID,
    )
    assert not harness.is_session_file(PurePosixPath(SESSION_DIR, "state.json"), SESSION_ID[:-1])
    assert not harness.is_session_file(
        PurePosixPath(".kimi-ale/sessions/wd_x", SESSION_ID), SESSION_ID
    )


def context(logs: Path, tmp_path: Path, **updates: object) -> TrajectoryParseContext:
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "trajectory",
        "instruction": PROMPTS[0],
        "logs_dir": logs,
        "model": "scripted-model",
        "agent_version": "2.3.2",
        "blobs": BlobStore(tmp_path / "episode"),
        "session_id": SESSION_ID,
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def write_run(logs: Path, *outputs: str) -> None:
    """One recorded invocation per prompt, each with its own part of stdout."""
    records: list[dict[str, object]] = []
    offset = 0
    for prompt, output in zip(PROMPTS, outputs, strict=True):
        records.append({"prompt": prompt, "stdout_offset": offset})
        offset += len(output.encode())
    write_jsonl(logs / ".ale-harness/invocations.jsonl", records)
    (logs / ".ale-harness/stdout.log").write_text("".join(outputs))


def prompt(agent: str, text: str, **fields: object) -> dict[str, object]:
    record = {"type": "turn.prompt", "agentId": agent, "input": [{"type": "text", "text": text}]}
    return record | fields


def step(
    agent: str, *events: dict[str, object], usage: dict[str, int] | None = None
) -> list[dict[str, object]]:
    """One native step: its loop events between the step's begin and end."""
    loop = [{"type": "step.begin"}, *events, {"type": "step.end", "usage": usage}]
    return [
        {"type": "context.append_loop_event", "agentId": agent, "event": event} for event in loop
    ]


def tool(
    call_id: str, name: str, args: dict[str, object], output: object, **result: object
) -> list[dict[str, object]]:
    return [
        {"type": "tool.call", "toolCallId": call_id, "name": name, "args": args},
        {"type": "tool.result", "toolCallId": call_id, "result": {"output": output, **result}},
    ]


def text_part(text: str) -> dict[str, object]:
    return {"type": "content.part", "part": {"type": "text", "text": text}}


def spawning(agent: str, call_id: str, child: str) -> list[dict[str, object]]:
    """A step whose Agent call spawned ``child``, and the record of that spawn."""
    return [
        *step(agent, *tool(call_id, "Agent", {"prompt": "run child"}, "CHILD-DONE")),
        {
            "type": "subagent.spawned",
            "parentAgentId": agent,
            "parentToolCallId": call_id,
            "subagentId": child,
        },
    ]


def child_wire(agent: str) -> list[dict[str, object]]:
    """A subagent's conversation: the delegated task, then one command."""
    return [
        prompt(agent, "run child"),
        *step(
            agent, *tool("call-3", "Bash", {"command": "echo hi"}, "hi"), usage={"inputOther": 7}
        ),
    ]


def kimi_logs(tmp_path: Path) -> Path:
    """Two turns; the first spawns ``child-1`` and then reads an image."""
    logs = tmp_path / "logs"
    write_run(logs, stdout("PARENT-DONE"), stdout("SECOND-DONE"))
    agents = logs / SESSION_DIR / "agents"
    image = [{"type": "image_url", "imageUrl": {"url": "blobref:image/png;blob-1"}}]
    write_jsonl(
        agents / "main/wire.jsonl",
        [
            prompt("main", PROMPTS[0], origin={"kind": "user"}),
            *spawning("main", "call-1", "child-1"),
            *step(
                "main",
                *tool("call-2", "ReadMediaFile", {"path": "a.png"}, image, note="1x1 pixels"),
            ),
            *step("main", text_part("PARENT-DONE"), usage={"inputOther": 5}),
            prompt("main", PROMPTS[1], origin={"kind": "user"}),
            *step("main", text_part("SECOND-DONE")),
        ],
    )
    (agents / "main/blobs").mkdir()
    (agents / "main/blobs/blob-1").write_bytes(b"png")
    write_jsonl(agents / "child-1/wire.jsonl", child_wire("child-1"))
    return logs


def test_parser_follows_the_native_session_and_embeds_its_subagent(tmp_path: Path) -> None:
    parse = context(kimi_logs(tmp_path), tmp_path)
    AutonomousHarnessConformance.check_parser_is_pure(KimiCodeHarness(), parse)
    trajectory = KimiCodeHarness().parse_trajectory(parse)

    assert trajectory.session_id == SESSION_ID
    assert trajectory.extra is None
    assert [(step.source, step.message) for step in trajectory.steps if not step.tool_calls] == [
        ("user", "delegate"),
        ("agent", "PARENT-DONE"),
        ("user", "second"),
        ("agent", "SECOND-DONE"),
    ]
    spawn = trajectory.steps[1]
    assert [call.function_name for call in spawn.tool_calls or ()] == ["Agent"]
    (result,) = spawn.observation.results  # type: ignore[union-attr]
    assert result.subagent_trajectory_ref is not None
    assert result.subagent_trajectory_ref[0].trajectory_id == "trajectory-subagent-child-1"
    (child,) = trajectory.subagent_trajectories or ()
    assert child.trajectory_id == "trajectory-subagent-child-1"
    assert child.session_id == "child-1"
    assert [step.source for step in child.steps] == ["user", "agent"]
    assert child.steps[0].message == "run child"
    assert child.steps[1].observation.results[0].content == "hi"  # type: ignore[union-attr]

    image_step = trajectory.steps[2]
    (image_result,) = image_step.observation.results  # type: ignore[union-attr]
    assert isinstance(image_result.content, list)
    image = next(part for part in image_result.content if part.type == "image")
    assert image.source is not None and image.source.media_type == "image/png"
    assert (tmp_path / "episode" / image.source.path).is_file()
    assert image_result.content[-1].text == "1x1 pixels"
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 5


def test_unlinked_subagents_and_missing_media_stay_raw_evidence(tmp_path: Path) -> None:
    logs = kimi_logs(tmp_path)
    wire = logs / SESSION_DIR / "agents/main/wire.jsonl"
    wire.write_text(
        "".join(
            line + "\n" for line in wire.read_text().splitlines() if "subagent.spawned" not in line
        )
    )
    shutil.rmtree(logs / SESSION_DIR / "agents/main/blobs")

    trajectory = KimiCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert trajectory.subagent_trajectories is None
    issues = trajectory.extra["ale"]["parse_issues"]  # type: ignore[index]
    child = f"{SESSION_DIR}/agents/child-1/wire.jsonl"
    assert {"reason": "unlinked_subagent", "source": child} in issues
    assert any(
        issue["reason"] == "unconverted_native_files" and child in issue["sources"]
        for issue in issues
    )
    assert any(issue["reason"] == "missing_blob" for issue in issues)
    image_result = trajectory.steps[2].observation.results[0]  # type: ignore[union-attr]
    assert "[image unavailable: blobref:image/png;" in json.dumps(image_result.content)


def test_a_subagent_spawned_by_a_subagent_is_embedded_in_it(tmp_path: Path) -> None:
    logs = kimi_logs(tmp_path)
    agents = logs / SESSION_DIR / "agents"
    write_jsonl(
        agents / "child-1/wire.jsonl",
        [*child_wire("child-1"), *spawning("child-1", "call-9", "child-2")],
    )
    write_jsonl(agents / "child-2/wire.jsonl", child_wire("child-2"))

    trajectory = KimiCodeHarness().parse_trajectory(context(logs, tmp_path, incomplete=True))

    assert "parse_issues" not in trajectory.extra["ale"]  # type: ignore[index]
    (helper,) = trajectory.subagent_trajectories or ()
    (nested,) = helper.subagent_trajectories or ()
    assert nested.trajectory_id == "trajectory-subagent-child-1-subagent-child-2"
    spawned = next(
        step
        for step in helper.steps
        if [call.function_name for call in step.tool_calls or ()] == ["Agent"]
    )
    (result,) = spawned.observation.results  # type: ignore[union-attr]
    assert result.subagent_trajectory_ref is not None
    assert result.subagent_trajectory_ref[0].trajectory_id == nested.trajectory_id
    # An interrupted run leaves every agent in it incomplete.
    for subagent in (helper, nested):
        assert subagent.extra == {"ale": {"incomplete": True, "incomplete_reason": "interrupted"}}


def test_a_failed_turn_is_recorded_where_it_happened(tmp_path: Path) -> None:
    logs = kimi_logs(tmp_path)
    failed = {
        "type": "turn.ended",
        "agentId": "main",
        "reason": "failed",
        "error": {"code": "provider.api_error", "message": "400 refused"},
    }
    with (logs / SESSION_DIR / "agents/main/wire.jsonl").open("a") as stream:
        stream.write(json.dumps(failed) + "\n")

    trajectory = KimiCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert trajectory.steps[-1].source == "system"
    assert trajectory.steps[-1].message == "400 refused; provider.api_error"


def test_without_the_session_each_turn_comes_from_its_prompt_and_stdout(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    write_run(logs, stdout("PARENT-DONE"), stdout("SECOND-DONE"))

    trajectory = KimiCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "delegate"),
        ("agent", "PARENT-DONE"),
        ("user", "second"),
        ("agent", "SECOND-DONE"),
    ]
    ale = trajectory.extra["ale"]  # type: ignore[index]
    assert {"reason": "native_session_unavailable", "session_id": SESSION_ID} in ale["parse_issues"]
    assert ale["limitations"]
