from __future__ import annotations

import json
import shlex
import shutil
import tomllib
from pathlib import Path, PurePosixPath

import pytest
from tests.unit.test_cli_harnesses import FakeSandbox, resources, session

from ale.core.config import load_run_config
from ale.core.errors import (
    AgentError,
    AgentUnsupportedError,
    ConfigError,
    HarnessLimitError,
)
from ale.core.harness import EffectiveAgentResources, ResolvedMcpServer, TrajectoryParseContext
from ale.core.taskspec import OperatingSystem, StdioMcpServer, StreamableHttpMcpServer
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.harnesses.base import INVOCATIONS_PATH, STDOUT_PATH
from ale.run.harnesses.forgecode import ForgecodeHarness
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit

STATE = "/home/agent/.forge-ale"
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


def test_preset_pins_release_binaries_for_both_systems() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "forgecode.toml")
    harness = _harness(config)
    assert isinstance(harness, ForgecodeHarness)
    assert harness.gateway_dialect == config.gateway.dialect == "anthropic"
    assert config.gateway.provider == "anthropic"
    assert harness.version() == "2.14.0"
    assert set(harness.supported_os) == set(OperatingSystem)
    assert harness.integrity().startswith("sha256:")
    assert all(
        asset.url.startswith("https://github.com/tailcallhq/forgecode/releases/download/v2.14.0/")
        and len(asset.sha256) == 64
        for asset in harness.release_assets.values()
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"reasoning": "high"},
        {"reasoning_effort": "ultra"},
        {"max_output_tokens": 0},
        {"max_requests_per_turn": -1},
        {"max_requests_per_turn": "unlimited"},
    ],
)
def test_settings_are_strict(settings: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        ForgecodeHarness(settings=settings)


def test_other_versions_and_dialects_fail_closed() -> None:
    with pytest.raises(ConfigError, match=r"2\.14\.0"):
        ForgecodeHarness(cli_version="2.13.3")
    with pytest.raises(ConfigError, match="dialect"):
        ForgecodeHarness(gateway_dialect="gemini")


@pytest.mark.parametrize(
    ("dialect", "provider", "url", "response_type", "variable"),
    [
        (
            "anthropic",
            "anthropic_compatible",
            "{{ANTHROPIC_URL}}/messages",
            "Anthropic",
            "ANTHROPIC_URL",
        ),
        (
            "openai-chat-completions",
            "openai_compatible",
            "{{OPENAI_URL}}/chat/completions",
            "OpenAI",
            "OPENAI_URL",
        ),
        (
            "openai-responses",
            "openai_responses_compatible",
            "{{OPENAI_URL}}/responses",
            "OpenAIResponses",
            "OPENAI_URL",
        ),
    ],
)
async def test_one_listed_model_on_forges_compatible_provider(
    tmp_path: Path, dialect: str, provider: str, url: str, response_type: str, variable: str
) -> None:
    sandbox = FakeSandbox()
    harness = ForgecodeHarness(
        gateway_dialect=dialect,
        settings={
            "reasoning_effort": "high",
            "max_output_tokens": 32000,
            "max_requests_per_turn": 40,
        },
    )
    await harness.install_resources(sandbox, session(), resources(tmp_path))  # type: ignore[arg-type]

    config = tomllib.loads(sandbox.files[f"{STATE}/.forge.toml"].decode())
    assert config["updates"] == {"frequency": "never", "auto_update": False}
    assert config["session"] == {"provider_id": provider, "model_id": "test-model"}
    assert config["reasoning"] == {"effort": "high"}
    assert (config["max_tokens"], config["max_requests_per_turn"]) == (32000, 40)
    (native,) = config["providers"]
    assert (native["id"], native["url"], native["response_type"]) == (provider, url, response_type)
    (model,) = native["models"]
    assert model["id"] == "test-model" and model["supports_reasoning"] is True
    credentials = json.loads(sandbox.files[f"{STATE}/.credentials.json"])
    assert credentials == [
        {
            "id": provider,
            "auth_details": {"api_key": "token"},
            "url_params": {variable: "http://gateway/v1"},
        }
    ]
    assert ["chmod", "600", f"{STATE}/.credentials.json"] in sandbox.commands


async def test_defaults_leave_reasoning_and_limits_to_forge() -> None:
    sandbox = FakeSandbox()
    await ForgecodeHarness().install_resources(sandbox, session(), EffectiveAgentResources())  # type: ignore[arg-type]
    config = tomllib.loads(sandbox.files[f"{STATE}/.forge.toml"].decode())
    assert {"reasoning", "max_tokens", "max_requests_per_turn"}.isdisjoint(config)
    assert "supports_reasoning" not in config["providers"][0]["models"][0]


async def test_skills_and_mcp_servers_are_native_config(tmp_path: Path) -> None:
    effective = resources(tmp_path)
    remote = mcp(
        "remote",
        StreamableHttpMcpServer(name="remote", transport="streamable-http", url="https://mcp.test"),
    )
    effective = effective.model_copy(update={"mcp_servers": (*effective.mcp_servers, remote)})
    sandbox = FakeSandbox()
    harness = ForgecodeHarness()
    harness.validate_resources(effective)
    await harness.install_resources(sandbox, session(), effective)  # type: ignore[arg-type]

    assert sandbox.uploads == [(str(tmp_path / "resource-proof"), f"{STATE}/skills/resource-proof")]
    assert json.loads(sandbox.files[f"{STATE}/.mcp.json"]) == {
        "mcpServers": {
            "task-proof": {
                "args": ["/home/agent/server.py"],
                "command": "python3",
                "env": {"PROOF_HOME": "/home/agent"},
            },
            "remote": {"oauth": False, "url": "https://mcp.test"},
        }
    }


def test_stdio_mcp_with_a_working_directory_is_rejected() -> None:
    server = StdioMcpServer(name="local", transport="stdio", command="python3", cwd="/srv")
    with pytest.raises(AgentUnsupportedError, match="working directory"):
        ForgecodeHarness().validate_resources(
            EffectiveAgentResources(mcp_servers=(mcp("local", server),))
        )


@pytest.mark.parametrize("native_session_id", [None, SESSION_ID])
async def test_each_invocation_continues_one_conversation_and_exports_it(
    native_session_id: str | None,
) -> None:
    command = await ForgecodeHarness().prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=native_session_id,
    )
    assert native_session_id is None or command.session_id == native_session_id
    requested = shlex.quote(str(command.session_id))
    assert f"forge --conversation-id {requested} < /home/agent/.ale-harness/prompt.md;" in (
        command.script
    )
    assert f"forge conversation dump {requested} < /dev/null" in command.script
    assert f"/home/agent/.forge-ale/dumps/{command.session_id}" in command.script
    assert command.script.endswith("exit $status")
    assert command.env["FORGE_CONFIG"] == STATE
    assert command.env["FORGE_UPDATES__FREQUENCY"] == "never"
    assert "token" not in command.env.values()


def stdout(*lines: str) -> str:
    return "".join(line + "\n" for line in lines)


def test_only_a_finished_turn_succeeds() -> None:
    harness = ForgecodeHarness()
    done = harness.read_result(
        stdout(
            f"● [05:19:36] Initialize {SESSION_ID}", "done", f"● [05:19:37] Finished {SESSION_ID}"
        )
    )
    assert (done.session_id, done.error) == (SESSION_ID, None)
    failed = harness.read_result(
        stdout(
            f"● [05:19:36] Initialize {SESSION_ID}",
            "● [05:19:36] ERROR: POST http://gateway/v1/messages",
            "",
            "Caused by:",
            "    0: Connection refused (os error 111)",
        )
    )
    assert failed.session_id == SESSION_ID
    assert failed.error is not None and failed.error.startswith("POST http://gateway/v1/messages")
    assert "Connection refused" in failed.error
    limited = harness.read_result(
        stdout(
            f"● [05:27:18] Initialize {SESSION_ID}",
            "● [05:27:18] Maximum request (1) per turn achieved",
            f"● [05:27:18] Finished {SESSION_ID}",
        )
    )
    assert limited.error == "forge reached its request limit (1 per turn)"


async def test_exit_zero_without_a_finished_turn_fails_and_limits_are_typed() -> None:
    sandbox = FakeSandbox()
    sandbox.native_stdout = stdout(f"● [05:19:36] Initialize {SESSION_ID}").encode()
    with pytest.raises(AgentError, match="did not finish"):
        await ForgecodeHarness().launch("go", sandbox, session(), timeout_sec=60)  # type: ignore[arg-type]
    limited = ForgecodeHarness(settings={"max_requests_per_turn": 1})
    assert isinstance(
        limited.classify_failure("forge reached its request limit (1 per turn)"), AgentError
    )
    error = limited.classify_failure("native: Maximum request (1) per turn achieved")
    assert isinstance(error, HarnessLimitError)


def test_session_files_are_the_conversations_exports() -> None:
    harness = ForgecodeHarness()
    dumps = PurePosixPath(".forge-ale/dumps")
    assert harness.is_session_file(dumps / SESSION_ID / "20261026T051937Z.json", SESSION_ID)
    assert not harness.is_session_file(dumps / f"{SESSION_ID}.json", SESSION_ID)
    assert not harness.is_session_file(dumps / SESSION_ID / "x.txt", SESSION_ID)


def recorded_context(root: Path, **updates: object) -> TrajectoryParseContext:
    first = json.loads((root / INVOCATIONS_PATH).read_text().splitlines()[0])
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "forge-trajectory",
        "instruction": first["prompt"],
        "logs_dir": root,
        "model": "scripted-model",
        "agent_version": "2.14.0",
        "blobs": BlobStore(root),
        "session_id": first["session_id"],
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def message(role: str, content: str, **fields: object) -> dict[str, object]:
    return {"text": {"role": role, "content": content, **fields}}


def write_dump(root: Path, conversation: dict[str, object], related: list[object]) -> None:
    path = root / ".forge-ale/dumps" / SESSION_ID / "20261026T000000Z.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"conversation": conversation, "related_conversations": related}))


def record(root: Path, *prompts: str, out: str | list[str]) -> None:
    """Each invocation's prompt; a list gives each invocation its own stdout."""
    parts = out if isinstance(out, list) else [""] * (len(prompts) - 1) + [out]
    offsets = [sum(len(part.encode()) for part in parts[:index]) for index in range(len(parts))]
    (root / STDOUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / STDOUT_PATH).write_text("".join(parts))
    (root / INVOCATIONS_PATH).write_text(
        "".join(
            json.dumps({"prompt": prompt, "session_id": SESSION_ID, "stdout_offset": offset}) + "\n"
            for prompt, offset in zip(prompts, offsets, strict=True)
        )
    )


RESUMED_PROMPTS = ["Write the file.", "Write it again."]


def write_resumed_run(root: Path) -> None:
    """A launch and one resume, each running one command: the export and each turn's stdout."""
    messages: list[dict[str, object]] = []
    outs: list[str] = []
    for turn, prompt in enumerate(RESUMED_PROMPTS, 1):
        call = f"call-{turn}"
        messages += [
            message("User", f"<task>{prompt}</task>", raw_content={"Text": prompt}),
            {
                **message(
                    "Assistant",
                    "",
                    tool_calls=[{"name": "shell", "call_id": call, "arguments": {"command": "ls"}}],
                ),
                "usage": {"prompt_tokens": {"actual": 10 * turn}},
            },
            {"tool": {"call_id": call, "output": {"values": [{"text": "ok"}]}}},
            message("Assistant", "Done."),
        ]
        start = "Initialize" if turn == 1 else "Continue"
        outs.append(
            stdout(f"● [00:00:00] {start} {SESSION_ID}", f"● [00:00:01] Finished {SESSION_ID}")
        )
    write_dump(root, {"id": SESSION_ID, "context": {"messages": messages}}, [])
    record(root, *RESUMED_PROMPTS, out=outs)


def test_a_launch_and_its_resume_convert_in_native_order(tmp_path: Path) -> None:
    write_resumed_run(tmp_path)
    trajectory = ForgecodeHarness().parse_trajectory(recorded_context(tmp_path))

    assert trajectory.session_id == SESSION_ID
    assert not trajectory.extra
    assert [(step.source, step.message) for step in trajectory.steps] == [
        step
        for prompt in RESUMED_PROMPTS
        for step in [("user", prompt), ("agent", ""), ("agent", "Done.")]
    ]
    for turn, step in enumerate(trajectory.steps[1::3], 1):
        (call,) = step.tool_calls or ()
        assert (call.tool_call_id, call.function_name) == (f"call-{turn}", "shell")
        (result,) = step.observation.results  # type: ignore[union-attr]
        assert (result.source_call_id, result.content) == (call.tool_call_id, "ok")
        assert step.metrics is not None and step.metrics.prompt_tokens == 10 * turn


def test_without_an_export_each_turn_keeps_its_prompt_and_outcome(tmp_path: Path) -> None:
    write_resumed_run(tmp_path)
    shutil.rmtree(tmp_path / ".forge-ale/dumps")
    trajectory = ForgecodeHarness().parse_trajectory(recorded_context(tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", prompt) for prompt in RESUMED_PROMPTS
    ]
    ale = trajectory.extra["ale"]  # type: ignore[index]
    assert ale["parse_issues"] == [
        {"reason": "native_session_unavailable", "session_id": SESSION_ID}
    ]
    assert ale["limitations"]


def delegation(call_id: str, child_id: str) -> list[dict[str, object]]:
    """A ``task`` call and the result naming the conversation it delegated to."""
    return [
        message(
            "Assistant",
            "",
            tool_calls=[{"name": "task", "call_id": call_id, "arguments": {"tasks": ["Go."]}}],
        ),
        {
            "tool": {
                "name": "task",
                "call_id": call_id,
                "output": {
                    "is_error": False,
                    "values": [{"aI": {"value": "done", "conversation_id": child_id}}],
                },
            }
        },
    ]


def test_delegation_mcp_names_and_failures_convert(tmp_path: Path) -> None:
    child_id = "11111111-2222-3333-4444-555555555555"
    grandchild_id = "66666666-7777-8888-9999-000000000000"
    record(tmp_path, "Delegate.", out=stdout(f"● [00:00:00] Initialize {SESSION_ID}"))
    (tmp_path / ".forge-ale/.mcp.json").parent.mkdir(parents=True)
    (tmp_path / ".forge-ale/.mcp.json").write_text(json.dumps({"mcpServers": {"Task-Proof": {}}}))
    write_dump(
        tmp_path,
        {
            "id": SESSION_ID,
            "context": {
                "messages": [
                    message("System", "You are Forge."),
                    message("User", "<task>Delegate.</task>", raw_content={"Text": "Delegate."}),
                    message(
                        "Assistant",
                        "",
                        tool_calls=[
                            {"name": "task", "call_id": "c1", "arguments": {"tasks": ["Sum."]}},
                            {
                                "name": "mcp_task_proof_tool_derive",
                                "call_id": "c2",
                                "arguments": {},
                            },
                        ],
                        reasoning_details=[{"text": "plan"}],
                    ),
                    {
                        "tool": {
                            "name": "task",
                            "call_id": "c1",
                            "output": {
                                "is_error": False,
                                "values": [{"aI": {"value": "done", "conversation_id": child_id}}],
                            },
                        }
                    },
                    {
                        "tool": {
                            "name": "mcp_task_proof_tool_derive",
                            "call_id": "c2",
                            "output": {"is_error": True, "values": [{"text": "boom"}]},
                        }
                    },
                ]
            },
        },
        [
            {
                "id": child_id,
                "title": "Sum.",
                "context": {
                    "messages": [
                        message("User", "<task>Sum.</task>", raw_content={"Text": "Sum."}),
                        *delegation("c3", grandchild_id),
                        message("Assistant", "Summed."),
                    ]
                },
            },
            {
                "id": grandchild_id,
                "context": {"messages": [message("User", "<task>Go.</task>")]},
            },
            {"id": "orphan-b", "context": {"messages": []}},
            {"id": "orphan-a", "context": {"messages": []}},
        ],
    )
    trajectory = ForgecodeHarness().parse_trajectory(recorded_context(tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Delegate."),
        ("agent", ""),
        ("system", "forge did not finish the turn"),
    ]
    agent = trajectory.steps[1]
    assert agent.reasoning_content == "plan"
    _, derive = agent.tool_calls or ()
    assert derive.extra == {"ale": {"mcp": {"server": "Task-Proof", "tool": "derive"}}}
    delegated, failed = agent.observation.results  # type: ignore[union-attr]
    (ref,) = delegated.subagent_trajectory_ref or ()
    assert ref.session_id == child_id
    (child,) = trajectory.subagent_trajectories or ()
    assert child.trajectory_id == ref.trajectory_id == f"forge-trajectory-subagent-{child_id}"
    assert [step.message for step in child.steps] == ["Sum.", "", "Summed."]
    # A nested delegation embeds in the trajectory of the conversation that spawned it.
    (grandchild,) = child.subagent_trajectories or ()
    assert grandchild.trajectory_id == f"{child.trajectory_id}-subagent-{grandchild_id}"
    (nested,) = child.steps[1].observation.results  # type: ignore[union-attr]
    assert [ref.trajectory_id for ref in nested.subagent_trajectory_ref or ()] == [
        grandchild.trajectory_id
    ]
    assert failed.extra == {"ale": {"is_error": True}} and failed.content == "boom"
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "unlinked_subagent", "session_id": "orphan-a"},
        {"reason": "unlinked_subagent", "session_id": "orphan-b"},
    ]
