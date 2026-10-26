from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path, PurePosixPath

import pytest
from tests.unit.test_cli_harnesses import FakeSandbox, resources, session

from ale.core.config import load_run_config
from ale.core.errors import AgentError, ConfigError
from ale.core.harness import EffectiveAgentResources, ResolvedMcpServer, TrajectoryParseContext
from ale.core.taskspec import StreamableHttpMcpServer
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.harnesses.base import INVOCATIONS_PATH, STDOUT_PATH
from ale.run.harnesses.pi_cli import PiCliHarness
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit

AGENT_DIR = "/home/agent/.pi-ale/agent"
SESSION_ID = "0b7c5f1e-6f39-4f0a-9d47-6a0f0c8f3a11"


def test_preset_is_complete_and_follows_the_gateway_dialect() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "pi-cli.toml")
    harness = _harness(config)
    assert isinstance(harness, PiCliHarness)
    assert harness.gateway_dialect == config.gateway.dialect == "openai-chat-completions"
    assert config.gateway.provider == "openai"
    assert harness.integrity() == "npm:@earendil-works/pi-coding-agent@1.4.0"


@pytest.mark.parametrize(
    "settings",
    [
        {"thinking_level": "low"},
        {"thinking": "xhigh"},
        {"thinking": "max"},
        {"context_window": 0},
        {"max_output_tokens": -1},
        {"max_output_tokens": True},
        {"disabled_tools": [" "]},
    ],
)
def test_settings_are_strict(settings: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        PiCliHarness(settings=settings)


def test_unknown_dialect_fails() -> None:
    with pytest.raises(ConfigError, match="dialect"):
        PiCliHarness(gateway_dialect="gemini")


@pytest.mark.parametrize(
    ("dialect", "api", "base_url"),
    [
        ("anthropic", "anthropic-messages", "http://gateway"),
        ("openai-chat-completions", "openai-completions", "http://gateway/v1"),
        ("openai-responses", "openai-responses", "http://gateway/v1"),
    ],
)
async def test_the_gateway_is_the_only_provider_and_holds_a_token_reference(
    tmp_path: Path, dialect: str, api: str, base_url: str
) -> None:
    sandbox = FakeSandbox()
    harness = PiCliHarness(
        gateway_dialect=dialect,
        settings={"thinking": "high", "context_window": 400000, "max_output_tokens": 32000},
    )
    await harness.install_resources(sandbox, session(), resources(tmp_path))  # type: ignore[arg-type]

    assert json.loads(sandbox.files[f"{AGENT_DIR}/models.json"]) == {
        "providers": {
            "ale": {
                "api": api,
                "apiKey": "$ALE_GATEWAY_TOKEN",
                "baseUrl": base_url,
                "models": [
                    {
                        "contextWindow": 400000,
                        "id": "test-model",
                        "input": ["text", "image"],
                        "maxTokens": 32000,
                        "reasoning": True,
                    }
                ],
            }
        }
    }
    settings = json.loads(sandbox.files[f"{AGENT_DIR}/settings.json"])
    assert settings["defaultProvider"] == "ale" and settings["enableInstallTelemetry"] is False
    assert all(b'"token"' not in data for data in sandbox.files.values())


async def test_defaults_leave_model_limits_and_thinking_to_pi() -> None:
    sandbox = FakeSandbox()
    await PiCliHarness().install_resources(sandbox, session(), EffectiveAgentResources())  # type: ignore[arg-type]
    (model,) = json.loads(sandbox.files[f"{AGENT_DIR}/models.json"])["providers"]["ale"]["models"]
    assert model == {"id": "test-model", "input": ["text", "image"]}
    off = FakeSandbox()
    await PiCliHarness(settings={"thinking": "off"}).install_resources(
        off,  # type: ignore[arg-type]
        session(),
        EffectiveAgentResources(),
    )
    assert "reasoning" not in off.files[f"{AGENT_DIR}/models.json"].decode()


async def test_skills_and_mcp_servers_are_direct_native_tools(tmp_path: Path) -> None:
    effective = resources(tmp_path)
    remote = ResolvedMcpServer(
        name="remote",
        server=StreamableHttpMcpServer(
            name="remote", transport="streamable-http", url="https://mcp.test/mcp"
        ),
        source_layers=("task",),
        declared_sources=("remote",),
        digest="sha256:" + "e" * 64,
        reportable=True,
    )
    effective = effective.model_copy(update={"mcp_servers": (*effective.mcp_servers, remote)})
    sandbox = FakeSandbox()
    await PiCliHarness().install_resources(sandbox, session(), effective)  # type: ignore[arg-type]

    assert sandbox.uploads == [
        (str(tmp_path / "resource-proof"), f"{AGENT_DIR}/skills/resource-proof")
    ]
    assert json.loads(sandbox.files[f"{AGENT_DIR}/mcp.json"]) == {
        "mcpServers": {
            "task-proof": {
                "args": ["/home/agent/server.py"],
                "command": "python3",
                "env": {"PROOF_HOME": "/home/agent"},
                "exposure": "direct",
            },
            "remote": {"exposure": "direct", "url": "https://mcp.test/mcp"},
        }
    }


@pytest.mark.parametrize("native_session_id", [None, SESSION_ID])
async def test_command_reads_stdin_into_one_exact_session(native_session_id: str | None) -> None:
    harness = PiCliHarness(settings={"thinking": "low", "disabled_tools": ["write"]})
    command = await harness.prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=native_session_id,
    )
    invocation, prompt = command.script.split(" < ")
    assert prompt == "/home/agent/.ale-harness/prompt.md"
    argv = shlex.split(invocation)
    options = {flag: argv[argv.index(flag) + 1] for flag in argv if flag.startswith("--")}
    assert argv[0] == "pi"
    assert options == {
        "--mode": "json",
        "--provider": "ale",
        "--model": "test-model",
        "--session-dir": "/home/agent/.pi-ale/sessions",
        "--session-id": command.session_id,
        "--thinking": "low",
        "--exclude-tools": "write",
    }
    assert native_session_id is None or command.session_id == native_session_id
    assert command.env["PI_CODING_AGENT_DIR"] == AGENT_DIR
    assert command.env["PI_OFFLINE"] == "1"
    assert command.env["ALE_GATEWAY_TOKEN"] == "token"


def events(*items: dict[str, object]) -> str:
    return "".join(json.dumps(item) + "\n" for item in items)


def assistant(text: str, **fields: object) -> dict[str, object]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}], **fields}


def test_result_reads_the_last_assistant_message_and_its_stop_reason() -> None:
    harness = PiCliHarness()
    header = {"type": "session", "version": 3, "id": SESSION_ID}
    done = harness.read_result(
        events(header, {"type": "agent_end", "messages": [assistant("done", stopReason="stop")]})
    )
    assert (done.session_id, done.final_message, done.error) == (SESSION_ID, "done", None)
    failed = harness.read_result(
        events(
            header,
            {
                "type": "agent_end",
                "messages": [assistant("", stopReason="error", errorMessage="Connection error.")],
            },
        )
    )
    assert failed.error == "pi stopped (error): Connection error."
    assert harness.read_result(events(header)).error == "pi ran no agent turn for the prompt"


async def test_json_mode_exits_zero_on_a_model_error_but_the_run_fails() -> None:
    sandbox = FakeSandbox()
    sandbox.native_stdout = events(
        {"type": "session", "id": SESSION_ID},
        {"type": "agent_end", "messages": [assistant("", stopReason="error", errorMessage="boom")]},
    ).encode()
    with pytest.raises(AgentError, match="boom"):
        await PiCliHarness().launch("go", sandbox, session(), timeout_sec=60)  # type: ignore[arg-type]


def test_session_files_carry_the_exact_id_after_their_timestamp() -> None:
    harness = PiCliHarness()
    sessions = PurePosixPath(".pi-ale/sessions")
    name = f"2026-10-26T04-16-51-318Z_{SESSION_ID}.jsonl"
    assert harness.is_session_file(sessions / name, SESSION_ID)
    assert not harness.is_session_file(sessions / name, SESSION_ID[1:])
    assert not harness.is_session_file(sessions / "nested" / name, SESSION_ID)
    assert not harness.is_session_file(sessions / f"{SESSION_ID}.jsonl", SESSION_ID)


def recorded_context(root: Path, **updates: object) -> TrajectoryParseContext:
    first = json.loads((root / INVOCATIONS_PATH).read_text().splitlines()[0])
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "pi-trajectory",
        "instruction": first["prompt"],
        "logs_dir": root,
        "model": "scripted-model",
        "agent_version": "1.4.0",
        "blobs": BlobStore(root),
        "session_id": first["session_id"],
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def write_session(root: Path, *entries: dict[str, object]) -> None:
    path = root / ".pi-ale/sessions" / f"2026-10-26T00-00-00-000Z_{SESSION_ID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(events({"type": "session", "version": 3, "id": SESSION_ID}, *entries))


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


def entry(identifier: str, parent: str | None, message: dict[str, object]) -> dict[str, object]:
    return {"type": "message", "id": identifier, "parentId": parent, "message": message}


def test_the_active_branch_converts_with_mcp_results_and_failures(tmp_path: Path) -> None:
    record(tmp_path, "Use the server.")
    write_session(
        tmp_path,
        {"type": "model_change", "id": "m", "parentId": None},
        entry("s", "m", {"role": "system", "content": "", "sections": {}}),
        entry("u", "s", {"role": "user", "content": [{"type": "text", "text": "Use the server."}]}),
        entry("x", "u", assistant("abandoned branch")),
        entry(
            "a",
            "u",
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "plan"},
                    {
                        "type": "toolCall",
                        "id": "t1",
                        "name": "mcp__task_proof__derive",
                        "arguments": {},
                    },
                ],
                "usage": {"input": 5, "output": 2, "cacheRead": 1},
                "stopReason": "toolUse",
            },
        ),
        entry(
            "r",
            "a",
            {
                "role": "toolResult",
                "toolCallId": "t1",
                "content": [{"type": "text", "text": "fragment"}],
                "details": {"server": "task-proof", "tool": "derive"},
                "isError": False,
            },
        ),
        {"type": "compaction", "id": "c", "parentId": "r", "summary": "Earlier work."},
        entry("e", "c", assistant("", stopReason="error", errorMessage="Connection error.")),
    )
    trajectory = PiCliHarness().parse_trajectory(recorded_context(tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Use the server."),
        ("agent", ""),
        ("system", "Earlier work."),
        ("system", "pi stopped (error): Connection error."),
    ]
    agent = trajectory.steps[1]
    assert agent.reasoning_content == "plan"
    (call,) = agent.tool_calls or ()
    assert call.extra == {"ale": {"mcp": {"server": "task-proof", "tool": "derive"}}}
    assert agent.observation.results[0].content == "fragment"  # type: ignore[union-attr]
    assert agent.metrics is not None and agent.metrics.cached_tokens == 1
    assert not trajectory.extra


RESUMED_PROMPTS = ["Write the file.", "Write it again."]
RESUMED_STEPS = [
    step
    for prompt in RESUMED_PROMPTS
    for step in [("user", prompt), ("agent", ""), ("agent", "Done.")]
]


def write_resumed_run(root: Path) -> None:
    """A launch and one resume, each running one command: the session and each turn's events."""
    entries: list[dict[str, object]] = []
    streams: list[str] = []
    parent: str | None = None
    for turn, prompt in enumerate(RESUMED_PROMPTS, 1):
        call = f"call-{turn}"
        messages: list[dict[str, object]] = [
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": call, "name": "bash", "arguments": {"command": "ls"}}
                ],
                "usage": {"input": 10 * turn, "output": 1, "cacheRead": 0},
            },
            {"role": "toolResult", "toolCallId": call, "content": [{"type": "text", "text": "ok"}]},
            assistant("Done."),
        ]
        for message in messages:
            entries.append(entry(f"e{len(entries)}", parent, message))
            parent = f"e{len(entries) - 1}"
        streams.append(events(*({"type": "message_end", "message": item} for item in messages)))
    write_session(root, *entries)
    record(root, *RESUMED_PROMPTS, stdout=streams)


def test_a_launch_and_its_resume_convert_in_native_order(tmp_path: Path) -> None:
    write_resumed_run(tmp_path)
    trajectory = PiCliHarness().parse_trajectory(recorded_context(tmp_path))

    assert trajectory.session_id == SESSION_ID
    assert not trajectory.extra
    assert [(step.source, step.message) for step in trajectory.steps] == RESUMED_STEPS
    for turn, step in enumerate(trajectory.steps[1::3], 1):
        (call,) = step.tool_calls or ()
        assert (call.tool_call_id, call.function_name) == (f"call-{turn}", "bash")
        (result,) = step.observation.results  # type: ignore[union-attr]
        assert (result.source_call_id, result.content) == (call.tool_call_id, "ok")
        assert step.metrics is not None and step.metrics.prompt_tokens == 10 * turn


def test_without_the_session_each_turn_comes_from_its_json_events(tmp_path: Path) -> None:
    write_resumed_run(tmp_path)
    shutil.rmtree(tmp_path / ".pi-ale")
    trajectory = PiCliHarness().parse_trajectory(recorded_context(tmp_path))

    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "native_session_unavailable", "session_id": SESSION_ID}
    ]
    assert [(step.source, step.message) for step in trajectory.steps] == RESUMED_STEPS
    for turn, step in enumerate(trajectory.steps[1::3], 1):
        (call,) = step.tool_calls or ()
        assert call.tool_call_id == f"call-{turn}"
        assert step.observation.results[0].source_call_id == call.tool_call_id  # type: ignore[union-attr]
