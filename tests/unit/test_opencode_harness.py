from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from tests.unit.test_cli_harnesses import FakeSandbox, resources, session

from ale.core.config import load_run_config
from ale.core.errors import AgentError, AgentUnsupportedError, ConfigError
from ale.core.harness import EffectiveAgentResources, ResolvedMcpServer, TrajectoryParseContext
from ale.core.sandbox import ExecResult
from ale.core.taskspec import OperatingSystem, StdioMcpServer, StreamableHttpMcpServer
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.harnesses.base import INVOCATIONS_PATH, STDOUT_PATH
from ale.run.harnesses.opencode import OpenCodeHarness
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit

STATE = "/home/agent/.opencode-ale"
SESSION_ID = "ses_0b7c5f1e6f394f0a9d476a0f0c8f3a11"
CHILD_ID = "ses_child"


def test_preset_is_complete_and_follows_the_gateway_dialect() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "opencode.toml")
    harness = _harness(config)
    assert isinstance(harness, OpenCodeHarness)
    assert harness.gateway_dialect == config.gateway.dialect == "openai-chat-completions"
    assert config.gateway.provider == "openai"
    assert harness.integrity() == "npm:@opencode/cli@2.3.2"
    assert harness.settings.web_search == "off"
    gemini = _harness(
        load_run_config(
            preset_path=PRESET_DIR / "opencode.toml", overrides=['gateway.dialect="gemini"']
        )
    )
    assert isinstance(gemini, OpenCodeHarness) and gemini.gateway_dialect == "gemini"


@pytest.mark.parametrize(
    "settings",
    [
        {"reasoning_effort": "extreme"},
        {"context_window": 0},
        {"max_output_tokens": -1},
        {"max_output_tokens": True},
        {"max_steps": "unlimited"},
        {"input": ["image"]},
        {"input": ["text", "text"]},
        {"input": ["text", "3d"]},
        {"web_search": "google"},
        {"websearch": False},
    ],
)
def test_settings_are_strict(settings: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        OpenCodeHarness(settings=settings)


def test_unknown_dialect_fails() -> None:
    with pytest.raises(ConfigError, match="dialect"):
        OpenCodeHarness(gateway_dialect="ollama")


@pytest.mark.parametrize(
    ("dialect", "package", "base_url"),
    [
        ("openai-chat-completions", "@opencode/ai/providers/openai-compatible", "/v1"),
        ("openai-responses", "@opencode/ai/providers/openai-compatible/responses", "/v1"),
        ("anthropic", "@opencode/ai/providers/anthropic-compatible", "/v1"),
        ("gemini", "@opencode/ai/providers/google", "/v1beta"),
    ],
)
async def test_the_gateway_is_the_only_usable_provider(
    dialect: str, package: str, base_url: str
) -> None:
    sandbox = FakeSandbox()
    harness = OpenCodeHarness(
        gateway_dialect=dialect,
        settings={
            "reasoning_effort": "high",
            "context_window": 400000,
            "max_output_tokens": 64000,
            "input": ["text", "image", "pdf"],
            "max_steps": 40,
            "web_search": "tavily",
        },
    )
    await harness.install_resources(sandbox, session(), EffectiveAgentResources())  # type: ignore[arg-type]

    config = json.loads(sandbox.files[f"{STATE}/ale.json"])
    assert config == {
        "$schema": "https://opencode.ai/config.json",
        "model": "ale/test-model",
        "update": "disable",
        "share": "disabled",
        "providers": {
            "ale": {
                "name": "ALE Gateway",
                "package": package,
                "env": ["ALE_GATEWAY_TOKEN"],
                "settings": {"baseURL": "http://gateway" + base_url},
                "models": {
                    "test-model": {
                        "capabilities": {
                            "reasoning": {"supported": True, "effort": ["high"]},
                            "input": ["text", "image", "pdf"],
                        },
                        "limit": {"context": 400000, "output": 64000},
                    }
                },
            }
        },
        "policies": [
            {"action": "provider.use", "resource": "*", "effect": "deny"},
            {"action": "provider.use", "resource": "ale", "effect": "allow"},
        ],
        "plugins": [
            "-opencode.provider.ollama",
            "-opencode.provider.lmstudio",
            "-opencode.provider.vllm",
        ],
        "websearch": {"provider": "tavily"},
        "mcp": {"servers": {}},
        "agents": {"build": {"steps": 40}},
    }
    assert all(b'"token"' not in data for data in sandbox.files.values())


async def test_defaults_leave_the_model_to_opencode_and_remove_websearch() -> None:
    sandbox = FakeSandbox()
    await OpenCodeHarness().install_resources(sandbox, session(), EffectiveAgentResources())  # type: ignore[arg-type]
    config = json.loads(sandbox.files[f"{STATE}/ale.json"])
    assert config["providers"]["ale"]["models"] == {"test-model": {}}
    assert config["websearch"] is False
    assert "agents" not in config


async def test_a_model_opencode_cannot_name_fails_before_launch() -> None:
    with pytest.raises(AgentUnsupportedError, match="#"):
        await OpenCodeHarness().install_resources(
            FakeSandbox(),  # type: ignore[arg-type]
            session(model="org/model#v2"),
            EffectiveAgentResources(),
        )


async def test_skills_and_mcp_servers_are_direct_native_tools(tmp_path: Path) -> None:
    effective = resources(tmp_path)
    remote = ResolvedMcpServer(
        name="remote",
        server=StreamableHttpMcpServer(
            name="remote", transport="streamable-http", url="https://mcp.test/mcp"
        ),
        source_layers=("run",),
        declared_sources=("remote",),
        digest="sha256:" + "e" * 64,
        reportable=True,
        headers={"X-Proof": "yes"},
    )
    effective = effective.model_copy(update={"mcp_servers": (*effective.mcp_servers, remote)})
    sandbox = FakeSandbox()
    await OpenCodeHarness().install_resources(sandbox, session(), effective)  # type: ignore[arg-type]

    assert sandbox.uploads == [(str(tmp_path / "resource-proof"), f"{STATE}/skills/resource-proof")]
    assert json.loads(sandbox.files[f"{STATE}/ale.json"])["mcp"] == {
        "servers": {
            "task-proof": {
                "type": "local",
                "command": ["python3", "/home/agent/server.py"],
                "environment": {"PROOF_HOME": "/home/agent"},
                "codemode": False,
            },
            "remote": {
                "type": "remote",
                "url": "https://mcp.test/mcp",
                "oauth": False,
                "headers": {"X-Proof": "yes"},
                "codemode": False,
            },
        }
    }


@pytest.mark.parametrize("native_session_id", [None, SESSION_ID])
async def test_command_reads_stdin_into_one_exact_session(native_session_id: str | None) -> None:
    harness = OpenCodeHarness(settings={"reasoning_effort": "low"})
    command = await harness.prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=native_session_id,
    )
    run = next(part for part in command.script.split("; ") if part.startswith("opencode run"))
    invocation, prompt = run.split(" < ")
    assert prompt == "/home/agent/.ale-harness/prompt.md"
    argv = shlex.split(invocation)
    assert argv[:2] == ["opencode", "run"]
    assert {"--standalone", "--auto"} <= set(argv)
    options = {flag: argv[argv.index(flag) + 1] for flag in ("--format", "--model", "--session")}
    assert options == {
        "--format": "json",
        "--model": "ale/test-model#low",
        "--session": command.session_id,
    }
    if native_session_id is None:
        assert command.session_id is not None
        assert command.session_id.startswith("ses_") and len(command.session_id) == 36
    else:
        assert command.session_id == native_session_id
    assert command.env["OPENCODE_CONFIG_DIR"] == STATE
    assert command.env["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    assert command.env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert command.env["ALE_GATEWAY_TOKEN"] == "token"
    assert "OPENCODE_CONFIG_CONTENT" not in command.env


#: Stands in for `opencode`: `run` prints one text event; `session export <id>` prints the
#: export file of that ID beside it, or fails when there is none.
FAKE_OPENCODE = """#!/bin/sh
if [ "$1" = run ]; then
    cat > /dev/null
    printf '%s\\n' '{"type":"text","sessionID":"ses_root","part":{"id":"p","text":"done"}}'
    exit "${FAKE_STATUS:-0}"
fi
[ "$1 $2" = "session export" ] && [ -n "$OPENCODE_CONFIG_CONTENT" ] || exit 9
cat "$(dirname "$0")/$3.json"
"""


def _export(session_id: str, *, parent: str | None = None, spawns: tuple[str, ...] = ()) -> str:
    calls = [
        {
            "type": "tool",
            "id": f"call-{child}",
            "name": "subagent",
            "state": {"status": "completed", "input": {}, "metadata": {"sessionID": child}},
        }
        for child in spawns
    ]
    info = {"id": session_id, **({"parentID": parent} if parent else {})}
    return json.dumps({"info": info, "messages": [{"type": "assistant", "content": calls}]})


@pytest.mark.parametrize("status", [0, 3])
async def test_every_invocation_exports_its_session_and_each_one_it_spawned(
    tmp_path: Path, status: int
) -> None:
    home = tmp_path / "home"
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "opencode").write_text(FAKE_OPENCODE)
    (fake / "opencode").chmod(0o755)
    (fake / "ses_root.json").write_text(_export("ses_root", spawns=("ses_a", "ses_b")))
    (fake / "ses_a.json").write_text(_export("ses_a", parent="ses_root", spawns=("ses_c",)))
    (fake / "ses_c.json").write_text(_export("ses_c", parent="ses_a", spawns=("ses_a",)))
    (home / ".opencode-ale").mkdir(parents=True)
    (home / ".opencode-ale/ale.json").write_text('{"model": "ale/m"}')
    (home / "prompt.md").write_text("go")
    command = await OpenCodeHarness().prepare_command(
        str(home / "prompt.md"),
        FakeSandbox(),  # type: ignore[arg-type]
        session(home=str(home)),
        native_session_id="ses_root",
    )
    env = {**command.env, "PATH": f"{fake}:{os.environ['PATH']}", "FAKE_STATUS": str(status)}
    result = subprocess.run(
        ["sh", "-c", command.script], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == status, result.stderr
    assert '"text":"done"' in result.stdout
    (export,) = (home / ".opencode-ale/exports/ses_root").iterdir()
    assert not export.name.startswith(".")
    # ses_b has no export, so it is left out; ses_c's call naming ses_a again is not followed.
    assert sorted(path.name for path in export.iterdir()) == [
        "ses_a.json",
        "ses_c.json",
        "ses_root.json",
    ]


async def test_no_export_is_kept_without_the_session_itself(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "opencode").write_text(FAKE_OPENCODE)
    (fake / "opencode").chmod(0o755)
    (home / ".opencode-ale").mkdir(parents=True)
    (home / ".opencode-ale/ale.json").write_text("{}")
    command = await OpenCodeHarness().prepare_command(
        str(home / "prompt.md"),
        FakeSandbox(),  # type: ignore[arg-type]
        session(home=str(home)),
        native_session_id=None,
    )
    (home / "prompt.md").write_text("go")
    env = {**command.env, "PATH": f"{fake}:{os.environ['PATH']}"}
    subprocess.run(["sh", "-c", command.script], env=env, check=True, capture_output=True)
    assert list((home / ".opencode-ale/exports" / str(command.session_id)).iterdir()) == []


def events(*items: dict[str, object]) -> str:
    return "".join(json.dumps(item) + "\n" for item in items)


def text(message_id: str, part_id: str, value: str) -> dict[str, object]:
    return {
        "type": "text",
        "sessionID": SESSION_ID,
        "part": {"id": part_id, "messageID": message_id, "type": "text", "text": value},
    }


def test_result_joins_each_text_part_and_reads_the_last_one() -> None:
    harness = OpenCodeHarness()
    done = harness.read_result(
        events(
            text("msg_1", "prt_1", "first"),
            text("msg_2", "prt_2", "Fin"),
            text("msg_2", "prt_2", "ished."),
        )
    )
    assert (done.session_id, done.final_message, done.error) == (SESSION_ID, "Finished.", None)
    failed = harness.read_result(
        events(
            text("msg_1", "prt_1", "partial"),
            {
                "type": "error",
                "sessionID": SESSION_ID,
                "error": {"type": "provider.no-route", "message": "Variant unavailable"},
            },
        )
    )
    assert failed.error == "Variant unavailable; provider.no-route"
    assert harness.read_result("").session_id is None


async def test_a_terminal_error_event_fails_the_run_whatever_the_exit_code() -> None:
    sandbox = FakeSandbox()
    sandbox.native_stdout = events(
        {"type": "error", "sessionID": "", "error": {"type": "unknown", "message": "boom"}}
    ).encode()
    with pytest.raises(AgentError, match="boom"):
        await OpenCodeHarness().launch("go", sandbox, session(), timeout_sec=60)  # type: ignore[arg-type]


def test_a_retried_model_call_does_not_fail_the_invocation() -> None:
    retried = OpenCodeHarness().read_result(
        events(
            {
                "type": "retry",
                "sessionID": SESSION_ID,
                "messageID": "msg_1",
                "attempt": 1,
                "error": {"type": "provider", "message": "upstream 503"},
            },
            text("msg_1", "prt_1", "Recovered."),
        )
    )
    assert (retried.final_message, retried.error) == ("Recovered.", None)


class InstallSandbox(FakeSandbox):
    """Answers installation: OpenCode is installed, and names its paths."""

    def __init__(self, paths: str) -> None:
        super().__init__()
        self.paths = paths
        self.request = SimpleNamespace(os=OperatingSystem.LINUX)

    async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        command = " ".join(str(part) for part in argv)
        self.commands.append([str(part) for part in argv])
        self.environments.append(kwargs.get("env") or {})
        if argv[:2] == ["sh", "-c"] and "HOME" in command:
            return ExecResult(exit_code=0, stdout="/home/user")
        if argv[:2] == ["sh", "-c"] and "PATH" in command:
            return ExecResult(exit_code=0, stdout="/usr/bin:/bin")
        if command.endswith("opencode --version"):
            return ExecResult(exit_code=0, stdout="opencode v2.3.2\n")
        if command.endswith("opencode debug paths"):
            return ExecResult(exit_code=0, stdout=self.paths)
        if command.endswith("uname -m"):
            return ExecResult(exit_code=0, stdout="x86_64\n")
        if argv[-1] == "--version":
            return ExecResult(exit_code=0, stdout="ripgrep 15.1.0\n")
        return ExecResult(exit_code=0)


async def test_ripgrep_goes_where_opencode_looks_before_downloading_it() -> None:
    sandbox = InstallSandbox(
        "data   /home/user/.local/share/opencode\nbin    /x/cache/opencode/bin\n"
    )
    assert await OpenCodeHarness().install(sandbox) == "2.3.2"  # type: ignore[arg-type]
    (install,) = [
        command for command in sandbox.commands if command[1:2] == ["-c"] and len(command) > 4
    ]
    assert install[3:7] == [
        "https://github.com/BurntSushi/ripgrep/releases/download/15.1.0/"
        "ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz",
        "1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599",
        "/x/cache/opencode/bin/rg",
        "ripgrep-15.1.0-x86_64-unknown-linux-musl/rg",
    ]
    for paths in ("data   /somewhere\n", "bin\n"):
        with pytest.raises(AgentError, match="no bin directory"):
            await OpenCodeHarness().install(InstallSandbox(paths))  # type: ignore[arg-type]
    debug = next(index for index, command in enumerate(sandbox.commands) if "debug" in command[-1])
    assert sandbox.environments[debug]["OPENCODE_DISABLE_AUTOUPDATE"] == "1"


def test_session_files_are_the_exports_of_exactly_that_session() -> None:
    harness = OpenCodeHarness()
    exports = PurePosixPath(".opencode-ale/exports")
    name = f"{SESSION_ID}.json"
    assert harness.is_session_file(
        exports / SESSION_ID / "20261121T000000000000Z" / name, SESSION_ID
    )
    assert not harness.is_session_file(exports / SESSION_ID / ".20261121T0Z" / name, SESSION_ID)
    assert not harness.is_session_file(
        exports / SESSION_ID / "20261121T0Z" / f"{CHILD_ID}.json", SESSION_ID
    )
    assert not harness.is_session_file(exports / SESSION_ID / name, SESSION_ID)
    assert not harness.is_session_file(exports / "ses_other" / "t" / name, SESSION_ID)


# --- the parser ---


def context(root: Path, **updates: object) -> TrajectoryParseContext:
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "opencode-trajectory",
        "instruction": "Use the tools.",
        "logs_dir": root,
        "model": "scripted-model",
        "agent_version": "2.3.2",
        "blobs": BlobStore(root / "blobs"),
        "session_id": SESSION_ID,
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def record(root: Path, *prompts: str, stdout: list[str] | None = None) -> None:
    """Each invocation's prompt, and its own stdout."""
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


def export(root: Path, stamp: str, *sessions: dict[str, object], raw: str | None = None) -> None:
    directory = root / ".opencode-ale/exports" / SESSION_ID / stamp
    directory.mkdir(parents=True, exist_ok=True)
    for data in sessions:
        (directory / f"{data['info']['id']}.json").write_text(json.dumps(data))  # type: ignore[index]
    if raw is not None:
        (directory / f"{SESSION_ID}.json").write_text(raw)


def configure(root: Path, *servers: str) -> None:
    (root / ".opencode-ale").mkdir(parents=True, exist_ok=True)
    (root / ".opencode-ale/ale.json").write_text(
        json.dumps({"mcp": {"servers": {name: {"type": "local"} for name in servers}}})
    )


def user(prompt: str) -> dict[str, object]:
    return {"type": "user", "id": "msg_u", "text": prompt, "time": {"created": 1795241923522}}


def assistant(*content: dict[str, object], **fields: object) -> dict[str, object]:
    return {
        "type": "assistant",
        "id": "msg_a",
        "model": {"id": "scripted-model", "providerID": "ale"},
        "content": list(content),
        "time": {"created": 1795241923550},
        **fields,
    }


def tool(call_id: str, name: str, state: dict[str, object]) -> dict[str, object]:
    return {"type": "tool", "id": call_id, "name": name, "state": {"input": {}, **state}}


def test_an_export_converts_in_native_order(tmp_path: Path) -> None:
    configure(tmp_path, "task.proof")
    record(tmp_path, "Use the tools.")
    image = base64.b64encode(b"\x89PNG\r\n\x1a\nproof").decode()
    export(
        tmp_path,
        "20261121T000000000000Z",
        {
            "info": {"id": SESSION_ID},
            "messages": [
                {"type": "model-switched", "id": "msg_m", "time": {"created": 1}},
                user("Use the tools."),
                assistant(
                    {"type": "reasoning", "text": "plan"},
                    {"type": "text", "text": "Running."},
                    tool(
                        "call_shell",
                        "shell",
                        {
                            "status": "completed",
                            "input": {"command": "ls"},
                            "content": [{"type": "text", "text": "a.txt\n"}],
                        },
                    ),
                    tool(
                        "call_mcp",
                        "task_proof_shot",
                        {
                            "status": "completed",
                            "content": [
                                {
                                    "type": "file",
                                    "uri": f"data:image/png;base64,{image}",
                                    "mime": "image/png",
                                }
                            ],
                        },
                    ),
                    tool(
                        "call_bad",
                        "read",
                        {"status": "error", "error": {"type": "unknown", "message": "no file"}},
                    ),
                    tool("call_open", "write", {"status": "running", "metadata": {}}),
                    tokens={
                        "input": 11,
                        "output": 7,
                        "reasoning": 4,
                        "cache": {"read": 3, "write": 2},
                    },
                ),
                {"type": "synthetic", "id": "msg_s", "text": "<notice/>", "time": {"created": 2}},
                assistant({"type": "text", "text": "Done."}),
                {"type": "idle", "id": "msg_i", "outcome": "succeeded", "time": {"created": 3}},
            ],
        },
    )
    harness = OpenCodeHarness()
    trajectory = harness.parse_trajectory(context(tmp_path))

    assert trajectory == harness.parse_trajectory(context(tmp_path))
    assert not trajectory.extra
    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Use the tools."),
        ("agent", "Running."),
        ("system", "<notice/>"),
        ("agent", "Done."),
    ]
    step = trajectory.steps[1]
    assert step.timestamp == "2026-11-21T06:18:43.550000+00:00"
    assert step.model_name == "scripted-model" and step.reasoning_content == "plan"
    assert step.metrics is not None
    # OpenCode counts cached input and reasoning apart from input and output.
    assert (step.metrics.prompt_tokens, step.metrics.completion_tokens) == (16, 11)
    assert step.metrics.cached_tokens == 3
    calls = {call.tool_call_id: call for call in step.tool_calls or ()}
    assert list(calls) == ["call_shell", "call_mcp", "call_bad", "call_open"]
    assert calls["call_mcp"].extra == {"ale": {"mcp": {"server": "task.proof", "tool": "shot"}}}
    assert calls["call_shell"].extra is None
    results = {result.source_call_id: result for result in step.observation.results}  # type: ignore[union-attr]
    # A call that never settled has no result.
    assert list(results) == ["call_shell", "call_mcp", "call_bad"]
    assert results["call_shell"].content == "a.txt\n"
    (picture,) = results["call_mcp"].content  # type: ignore[misc]
    assert picture.type == "image" and picture.source.media_type == "image/png"  # type: ignore[union-attr]
    assert results["call_bad"].content == "no file; unknown"
    assert results["call_bad"].extra == {"ale": {"is_error": True}}


def test_a_launch_and_its_resume_convert_with_their_failures(tmp_path: Path) -> None:
    # OpenCode fails a run that asked for input it could not get, though the session's
    # turn succeeded.
    cancelled = events(
        {
            "type": "error",
            "sessionID": SESSION_ID,
            "error": {"type": "unknown", "message": "Cancelled a request for input"},
        }
    )
    failed = events(
        {"type": "error", "sessionID": SESSION_ID, "error": {"type": "x", "message": "quota"}}
    )
    record(tmp_path, "First.", "Second.", stdout=[cancelled, failed])
    export(
        tmp_path,
        "20261121T000000000000Z",
        {
            "info": {"id": SESSION_ID},
            "messages": [
                user("First."),
                assistant({"type": "text", "text": "One."}),
                {"type": "idle", "id": "i1", "outcome": "succeeded", "time": {"created": 1}},
                user("Second."),
                assistant(error={"type": "provider", "message": "upstream 500"}),
                {
                    "type": "idle",
                    "id": "i2",
                    "outcome": "failed",
                    "error": {"type": "provider", "message": "upstream 500"},
                    "time": {"created": 2},
                },
            ],
        },
    )
    trajectory = OpenCodeHarness().parse_trajectory(context(tmp_path))

    # A turn's own record of its failure stands in for stdout's.
    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "First."),
        ("agent", "One."),
        ("system", "Cancelled a request for input; unknown"),
        ("user", "Second."),
        ("agent", ""),
        ("system", "upstream 500; provider"),
    ]
    assert not trajectory.extra


def spawn(call_id: str, child: str) -> dict[str, object]:
    return tool(
        call_id,
        "subagent",
        {
            "status": "completed",
            "content": [{"type": "text", "text": f"working (sessionID: {child})"}],
            "metadata": {"sessionID": child},
        },
    )


def test_subagents_embed_in_their_spawners_and_strays_are_named(tmp_path: Path) -> None:
    record(tmp_path, "Delegate.")
    export(
        tmp_path,
        "20261121T000000000000Z",
        {
            "info": {"id": SESSION_ID},
            "messages": [
                user("Delegate."),
                # The second call names a session another one spawned.
                assistant(spawn("call_sub", CHILD_ID), spawn("call_stray", "ses_stray")),
            ],
        },
        {
            "info": {"id": CHILD_ID, "parentID": SESSION_ID, "title": "child"},
            "messages": [
                user("Child task."),
                assistant(spawn("call_grand", "ses_grand")),
                assistant({"type": "text", "text": "Child done."}),
            ],
        },
        {
            "info": {"id": "ses_grand", "parentID": CHILD_ID},
            "messages": [user("Grandchild task.")],
        },
        {"info": {"id": "ses_stray", "parentID": "ses_elsewhere"}, "messages": []},
    )
    other = tmp_path / ".opencode-ale/exports/ses_other/20261121T000000000000Z"
    other.mkdir(parents=True)
    (other / "ses_other.json").write_text('{"info": {}, "messages": []}')
    trajectory = OpenCodeHarness().parse_trajectory(context(tmp_path))

    (child,) = trajectory.subagent_trajectories or ()
    assert child.trajectory_id == f"opencode-trajectory-subagent-{CHILD_ID}"
    assert child.session_id == CHILD_ID
    assert [step.message for step in child.steps] == ["Child task.", "", "Child done."]
    (grandchild,) = child.subagent_trajectories or ()
    assert grandchild.trajectory_id == f"{child.trajectory_id}-subagent-ses_grand"
    assert [step.message for step in grandchild.steps] == ["Grandchild task."]
    (grand_result,) = child.steps[1].observation.results  # type: ignore[union-attr]
    assert [ref.trajectory_id for ref in grand_result.subagent_trajectory_ref or ()] == [
        grandchild.trajectory_id
    ]
    spawned, stray = trajectory.steps[1].observation.results  # type: ignore[union-attr]
    assert [ref.trajectory_id for ref in spawned.subagent_trajectory_ref or ()] == [
        child.trajectory_id
    ]
    assert stray.subagent_trajectory_ref is None
    assert trajectory.extra == {
        "ale": {
            "parse_issues": [
                {"reason": "unlinked_subagent", "session_id": "ses_stray"},
                {
                    "reason": "unconverted_native_sessions",
                    "count": 1,
                    "sources": [
                        ".opencode-ale/exports/ses_other/20261121T000000000000Z/ses_other.json"
                    ],
                },
            ]
        }
    }


def test_every_kind_of_native_message_has_its_step(tmp_path: Path) -> None:
    record(tmp_path, "Go.")
    export(
        tmp_path,
        "20261121T000000000000Z",
        {
            "info": {"id": SESSION_ID},
            "messages": [
                user("Go."),
                {"type": "skill", "id": "k", "text": "Skill body.", "time": {"created": 1}},
                {"type": "system", "id": "y", "text": "Updated.", "time": {"created": 1}},
                {
                    "type": "shell",
                    "id": "h",
                    "command": "ls",
                    "output": "a.txt",
                    "time": {"created": 1},
                },
                {"type": "compaction", "id": "c", "status": "completed", "summary": "Earlier."},
                {
                    "type": "compaction",
                    "id": "f",
                    "status": "failed",
                    "error": {"type": "x", "message": "too long"},
                },
                {"type": "agent-switched", "id": "s", "agent": "plan"},
                {"type": "hologram", "id": "z"},
                {"type": "idle", "id": "i", "outcome": "interrupted"},
            ],
        },
    )
    trajectory = OpenCodeHarness().parse_trajectory(context(tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "Go."),
        ("system", "Skill body."),
        ("system", "Updated."),
        ("system", "$ ls\na.txt"),
        ("system", "Earlier."),
        ("system", "compaction failed: too long; x"),
        ("system", "turn interrupted"),
    ]
    assert trajectory.extra == {
        "ale": {
            "parse_issues": [{"reason": "unknown_message_type", "type": "hologram", "count": 1}]
        }
    }


def test_a_malformed_export_falls_back_to_the_previous_one(tmp_path: Path) -> None:
    record(tmp_path, "First.")
    export(
        tmp_path,
        "20261121T000000000000Z",
        {"info": {"id": SESSION_ID}, "messages": [user("First.")]},
    )
    export(tmp_path, "20261121T000100000000Z", raw='{"info": ')
    trajectory = OpenCodeHarness().parse_trajectory(context(tmp_path))

    assert [step.message for step in trajectory.steps] == ["First."]
    (issue,) = trajectory.extra["ale"]["parse_issues"]  # type: ignore[index]
    assert issue["reason"] == "malformed_json"
    assert issue["source"].endswith(f"20261121T000100000000Z/{SESSION_ID}.json")


def test_a_turn_the_export_lacks_comes_from_its_json_events(tmp_path: Path) -> None:
    second = events(
        {"type": "step_start", "sessionID": SESSION_ID, "part": {"id": "s", "messageID": "m1"}},
        {
            "type": "tool_use",
            "sessionID": SESSION_ID,
            "part": {
                "messageID": "m1",
                "id": "call_1",
                "tool": "shell",
                "state": {"status": "completed", "input": {"command": "ls"}, "output": "a.txt"},
            },
        },
        {
            "type": "tool_use",
            "sessionID": SESSION_ID,
            "part": {
                "messageID": "m1",
                "id": "call_2",
                "tool": "read",
                "state": {"status": "error", "input": {}, "error": "no file"},
            },
        },
        {
            "type": "step_finish",
            "sessionID": SESSION_ID,
            "part": {"messageID": "m1", "tokens": {"input": 4, "output": 2}},
        },
        text("m2", "p2", "All "),
        text("m2", "p2", "done."),
    )
    record(tmp_path, "First.", "Second.", stdout=["", second + "not json\n"])
    export(
        tmp_path,
        "20261121T000000000000Z",
        {"info": {"id": SESSION_ID}, "messages": [user("First.")]},
    )
    trajectory = OpenCodeHarness().parse_trajectory(context(tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "First."),
        ("user", "Second."),
        ("agent", ""),
        ("agent", "All done."),
    ]
    call, failed = trajectory.steps[2].tool_calls or ()
    assert (call.tool_call_id, call.arguments) == ("call_1", {"command": "ls"})
    ran, refused = trajectory.steps[2].observation.results  # type: ignore[union-attr]
    assert ran.content == "a.txt"
    assert (refused.source_call_id, refused.content) == (failed.tool_call_id, "no file")
    assert refused.extra == {"ale": {"is_error": True}}
    assert trajectory.steps[2].metrics.prompt_tokens == 4  # type: ignore[union-attr]
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "prompt_not_in_native_session", "invocation": 2},
        {
            "invocation": 2,
            "line": 7,
            "reason": "malformed_json",
            "detail": "Expecting value: line 1 column 1 (char 0)",
        },
    ]


def test_without_an_export_each_turn_is_its_prompt_and_events(tmp_path: Path) -> None:
    record(tmp_path, "First.", stdout=[events(text("m", "p", "Answer."))])
    trajectory = OpenCodeHarness().parse_trajectory(context(tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "First."),
        ("agent", "Answer."),
    ]
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "native_session_unavailable", "session_id": SESSION_ID}
    ]
    assert trajectory.extra["ale"]["limitations"]  # type: ignore[index]


def test_a_prompt_that_never_reached_the_session_keeps_its_place(tmp_path: Path) -> None:
    refused = events(
        {
            "type": "error",
            "sessionID": SESSION_ID,
            "error": {"type": "provider.no-route", "message": "Variant unavailable"},
        }
    )
    record(tmp_path, "First.", "Second.", "Third.", stdout=["", refused, ""])
    export(
        tmp_path,
        "20261121T000000000000Z",
        {
            "info": {"id": SESSION_ID},
            "messages": [
                user("First."),
                assistant({"type": "text", "text": "One."}),
                user("Third."),
                assistant({"type": "text", "text": "Three."}),
            ],
        },
    )
    trajectory = OpenCodeHarness().parse_trajectory(context(tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "First."),
        ("agent", "One."),
        ("user", "Second."),
        ("system", "Variant unavailable; provider.no-route"),
        ("user", "Third."),
        ("agent", "Three."),
    ]
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "prompt_not_in_native_session", "invocation": 2}
    ]


def test_malformed_native_records_become_issues_not_failures(tmp_path: Path) -> None:
    configure(tmp_path)
    (tmp_path / ".opencode-ale/ale.json").write_text('{"mcp": {"servers": 5}}')
    record(tmp_path, "Go.")
    svg = base64.b64encode(b"<svg/>").decode()
    export(
        tmp_path,
        "20261121T000000000000Z",
        {
            "info": {"id": SESSION_ID},
            "messages": [
                {"type": "user", "id": "u", "text": "Go.", "time": 5},
                {"type": [], "id": "x"},
                {"type": "assistant", "id": "a", "content": 5, "time": {"created": 1e20}},
                assistant(
                    tool("call_a", "shot", {"status": [], "content": []}),
                    tool("call_a", "shot", {"status": "completed"}),
                    tool(
                        "call_b",
                        "shot",
                        {
                            "status": "completed",
                            "content": [
                                {"type": "file", "uri": f"data:image/svg+xml;base64,{svg}"},
                            ],
                        },
                    ),
                    spawn("call_gone", "ses_gone"),
                ),
            ],
        },
    )
    trajectory = OpenCodeHarness().parse_trajectory(context(tmp_path))

    step = trajectory.steps[-1]
    assert [call.tool_call_id for call in step.tool_calls or ()] == [
        "call_a",
        "call_b",
        "call_gone",
    ]
    results = {result.source_call_id: result for result in step.observation.results}  # type: ignore[union-attr]
    assert results["call_b"].content == "[file data:image/svg+xml]"
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "duplicate_tool_call", "session_id": SESSION_ID, "id": "call_a"},
        {"reason": "subagent_session_unavailable", "session_id": "ses_gone"},
        {"reason": "unknown_message_type", "type": "", "count": 1},
    ]


def _server(name: str, **fields: object) -> ResolvedMcpServer:
    return ResolvedMcpServer(
        name=name,
        server=StdioMcpServer(name=name, transport="stdio", command="python3", **fields),  # type: ignore[arg-type]
        source_layers=("run",),
        declared_sources=(name,),
        digest="sha256:" + "c" * 64,
        reportable=True,
    )


@pytest.mark.parametrize(
    ("servers", "refusal"),
    [
        ((_server("task.proof"), _server("task_proof")), "names the tools of both task_proof"),
        ((_server("proof", args=("{env:SECRET}",)),), "substitutes"),
        ((_server("proof", environment={"KEY": "{file:/etc/key}"}),), "substitutes"),
    ],
)
def test_servers_opencode_would_misname_or_rewrite_are_refused(
    servers: tuple[ResolvedMcpServer, ...], refusal: str
) -> None:
    with pytest.raises(AgentUnsupportedError, match=refusal):
        OpenCodeHarness().validate_resources(
            EffectiveAgentResources(mcp_servers=servers, digest="sha256:" + "d" * 64)
        )
