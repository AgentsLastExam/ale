from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from tests.unit.test_cli_harnesses import FakeSandbox, resources, session

from ale.core.config import load_run_config
from ale.core.errors import AgentError, ConfigError
from ale.core.harness import EffectiveAgentResources, ResolvedMcpServer, TrajectoryParseContext
from ale.core.sandbox import ExecResult
from ale.core.taskspec import OperatingSystem, StreamableHttpMcpServer
from ale.core.testkit import AutonomousHarnessConformance
from ale.run.cli.tasks import PRESET_DIR, _harness
from ale.run.harnesses.zcode import ZCodeHarness
from ale.run.harnesses.zcode import adapter as zcode_adapter
from ale.run.recording import BlobStore
from ale.run.secrets import provider_credentials, provider_endpoint

pytestmark = pytest.mark.unit

SESSION_ID = "session-1"
CHILD_ID = "child-1"
PROVIDER = "/home/agent/.zcode-ale/provider.json"
CONFIG = "/home/agent/.zcode/cli/config.json"
DB = ".zcode/cli/db/db.sqlite"
PROMPTS = ("delegate", "second")


def test_preset_is_complete_and_speaks_messages_to_zai() -> None:
    config = load_run_config(preset_path=PRESET_DIR / "zcode.toml")
    harness = _harness(config)

    assert isinstance(harness, ZCodeHarness)
    assert harness.gateway_dialect == "anthropic"
    assert config.gateway.provider == "zai"
    assert config.agent.version == harness.default_version
    assert set(config.agent.settings) == set(harness.settings_type.model_fields)


def test_preset_sends_the_zai_key_to_zai_never_the_anthropic_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ZCode speaks the Anthropic Messages API, but to z.ai: the dialect must not pick the key.
    gateway = load_run_config(preset_path=PRESET_DIR / "zcode.toml").gateway
    monkeypatch.setenv("ALE_ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.delenv("ALE_ZAI_API_KEY", raising=False)
    monkeypatch.delenv("ALE_ZAI_BASE_URL", raising=False)

    assert provider_endpoint(gateway.provider) == (
        "ALE_ZAI_API_KEY",
        "https://api.z.ai/api/anthropic",
    )
    with pytest.raises(ConfigError, match="ALE_ZAI_API_KEY is not set"):
        provider_credentials(
            gateway.provider, base_url=gateway.base_url, key_var=gateway.api_key_env
        )
    monkeypatch.setenv("ALE_ZAI_API_KEY", "zai-key")
    assert provider_credentials(
        gateway.provider, base_url=gateway.base_url, key_var=gateway.api_key_env
    ) == ("zai-key", "https://api.z.ai/api/anthropic")


def test_only_the_pinned_build_and_known_dialects_are_accepted() -> None:
    with pytest.raises(ConfigError, match=r"pins 0\.17\.0 only"):
        ZCodeHarness(cli_version="0.16.0")
    with pytest.raises(ConfigError, match="gateway dialect"):
        ZCodeHarness(gateway_dialect="gemini")
    assert ZCodeHarness().integrity() == ZCodeHarness(cli_version="0.17.0").integrity()
    assert ZCodeHarness().integrity().startswith("sha256:")


@pytest.mark.parametrize(
    "settings",
    [
        {"disallowed": ["Bash"]},
        {"disallowed_tools": ["Bash,Edit"]},
        {"disallowed_tools": ["Bash Edit"]},
        {"disallowed_tools": [""]},
        {"workflow_mode": "on"},
        {"toolsearch_mode": "remote"},
    ],
)
def test_settings_are_strict(settings: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        ZCodeHarness(settings=settings)


@pytest.mark.parametrize(
    ("dialect", "api", "base_url"),
    [
        ("anthropic", "anthropic-messages", "http://gateway"),
        ("openai-chat-completions", "openai-chat-completions", "http://gateway/v1"),
        ("openai-responses", "openai-responses", "http://gateway/v1"),
    ],
)
async def test_configuration_routes_the_model_through_the_gateway(
    tmp_path: Path, dialect: str, api: str, base_url: str
) -> None:
    sandbox = FakeSandbox()
    harness = ZCodeHarness(gateway_dialect=dialect)
    effective = resources(tmp_path)

    harness.validate_resources(effective)
    await harness.install_resources(sandbox, session(), effective)  # type: ignore[arg-type]

    provider = json.loads(sandbox.files[PROVIDER])["config"]
    (rule,) = provider["providerConfigRules"]["providerRules"]
    assert rule["config"]["api"] == {"type": api, "baseUrl": base_url}
    assert rule["config"]["access"] == {"type": "api-key", "apiKey": "token"}
    assert rule["config"]["personalModelIds"] == ["test-model"]
    assert provider["defaultModelSelection"] == {"providerId": "ale", "modelId": "test-model"}
    assert ["chmod", "600", PROVIDER] in sandbox.commands
    assert json.loads(sandbox.files[CONFIG]) == {
        "features": {"mcp": True},
        "mcp": {
            "servers": {
                "task-proof": {
                    "type": "stdio",
                    "command": "python3",
                    "args": ["/home/agent/server.py"],
                    "env": {"PROOF_HOME": "/home/agent"},
                    "enabled": True,
                }
            }
        },
    }
    assert sandbox.uploads == [
        (str(effective.skills[0].path), "/home/agent/.zcode/skills/resource-proof")
    ]


@pytest.mark.parametrize(
    ("dialect", "open_network", "native"),
    [
        ("anthropic", True, True),
        ("anthropic", False, False),
        ("openai-responses", True, False),
    ],
)
async def test_native_web_search_follows_the_task_network(
    dialect: str, open_network: bool, native: bool
) -> None:
    """ZCode grants WebSearch by vendor URL, so the run's model rule decides it."""
    sandbox = FakeSandbox()
    await ZCodeHarness(gateway_dialect=dialect).install_resources(
        sandbox,  # type: ignore[arg-type]
        session(provider_web_tools=open_network),
        EffectiveAgentResources(),
    )
    rules = json.loads(sandbox.files[PROVIDER])["config"]["modelConfigRules"]
    assert rules["providerModelRules"] == [
        {
            "providerId": "ale",
            "modelId": "test-model",
            "config": {"properties": {"supportsNativeWebSearch": native}},
        }
    ]


async def test_without_mcp_servers_the_run_configuration_keeps_native_defaults() -> None:
    sandbox = FakeSandbox()
    await ZCodeHarness().install_resources(sandbox, session(), EffectiveAgentResources())  # type: ignore[arg-type]
    assert json.loads(sandbox.files[CONFIG]) == {}

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
    sandbox = FakeSandbox()
    await ZCodeHarness().install_resources(
        sandbox,  # type: ignore[arg-type]
        session(),
        EffectiveAgentResources(mcp_servers=(remote,), digest="sha256:" + "f" * 64),
    )
    assert json.loads(sandbox.files[CONFIG])["mcp"]["servers"] == {
        "remote": {"type": "http", "url": "https://mcp.example/mcp", "enabled": True}
    }


async def test_command_passes_the_prompt_file_as_its_argument() -> None:
    harness = ZCodeHarness(
        settings={
            "disallowed_tools": ["WebFetch", "Agent"],
            "workflow_mode": "disabled",
            "toolsearch_mode": "off",
        }
    )
    launch = await harness.prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=None,
    )
    assert launch.session_id is None
    script = launch.script
    assert script.startswith("unset ZCODE_STORAGE_DIR ZCODE_SESSION_DB_PATH ")
    assert "cat -- /home/agent/.ale-harness/prompt.md" in script
    assert script.endswith(' --prompt="$prompt"')
    assert "node /home/agent/.local/lib/zcode/zcode.cjs --mode yolo" in script
    assert "--output-format stream-json --cwd /home/agent" in script
    assert "--disallowed-tools WebFetch,Agent --workflow-mode disabled --toolsearch-mode off" in (
        script
    )
    assert "--resume" not in script
    assert launch.env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] == PROVIDER
    assert launch.env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] == (
        "/home/agent/.local/lib/zcode/zcode-builtin.json"
    )
    assert launch.env["ZCODE_MODEL_TELEMETRY_ENABLED"] == "false"
    assert "token" not in json.dumps(launch.env)

    resume = await ZCodeHarness().prepare_command(
        "/home/agent/.ale-harness/prompt.md",
        FakeSandbox(),  # type: ignore[arg-type]
        session(),
        native_session_id=SESSION_ID,
    )
    assert resume.session_id == SESSION_ID
    assert f"--resume {SESSION_ID}" in resume.script


async def test_windows_command_keeps_git_bash_from_rewriting_the_prompt() -> None:
    command = await ZCodeHarness().prepare_command(
        "C:/Users/Agent/.ale-harness/prompt.md",
        FakeSandbox(stdout=r"C:\Windows\System32"),  # type: ignore[arg-type]
        session(home=r"C:\Users\Agent"),
        native_session_id=None,
    )
    assert command.env["MSYS2_ARG_CONV_EXCL"] == "*"
    assert "C:/Users/Agent/.local/lib/zcode/zcode.cjs" in command.script


def _deb(members: dict[str, bytes], tmp_path: Path) -> Path:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:xz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    payload = archive.getvalue()

    def entry(name: str, data: bytes) -> bytes:
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n".encode()
        return header + data + (b"\n" if len(data) % 2 else b"")

    path = tmp_path / "zcode.deb"
    path.write_bytes(
        b"!<arch>\n"
        + entry("debian-binary", b"2.0\n")
        + entry("control.tar.xz", b"x")
        + entry("data.tar.xz", payload)
    )
    return path


def _spec(path: Path, target: Path, files: dict[str, bytes], sha512: str) -> str:
    return json.dumps(
        {
            "url": path.as_uri(),
            "sha512": sha512,
            "target": str(target),
            "files": {
                member: [PurePosixPath(member).name, hashlib.sha256(data).hexdigest()]
                for member, data in files.items()
            },
        }
    )


def test_the_installer_unpacks_only_pinned_files_it_verified(tmp_path: Path) -> None:
    wanted = {
        "./opt/ZCode/resources/glm/zcode.cjs": b"console.log('zcode')\n",
        "./opt/ZCode/resources/config/provider/zcode-builtin.json": b"{}\n",
    }
    deb = _deb({**wanted, "./opt/ZCode/zcode": b"\x7fELF gui"}, tmp_path)
    sha512 = hashlib.sha512(deb.read_bytes()).hexdigest()
    target = tmp_path / "lib"

    def install(spec: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-c", zcode_adapter._INSTALL, spec],
            capture_output=True,
            text=True,
            check=False,
        )

    done = install(_spec(deb, target, wanted, sha512))
    assert done.returncode == 0, done.stderr
    assert sorted(path.name for path in target.iterdir()) == ["zcode-builtin.json", "zcode.cjs"]
    assert (target / "zcode.cjs").read_bytes() == wanted["./opt/ZCode/resources/glm/zcode.cjs"]

    # Matching files are reused without downloading again.
    deb.unlink()
    assert install(_spec(deb, target, wanted, sha512)).returncode == 0

    shutil.rmtree(target)
    deb = _deb({**wanted, "./opt/ZCode/zcode": b"\x7fELF gui"}, tmp_path)
    tampered = install(_spec(deb, target, wanted, "0" * 128))
    assert tampered.returncode != 0 and "not the pinned one" in tampered.stderr
    other = install(
        _spec(deb, target, {**wanted, "./opt/ZCode/resources/glm/zcode.cjs": b"other"}, sha512)
    )
    assert other.returncode != 0 and "not the pinned build" in other.stderr
    assert not (target / "zcode.cjs").exists()


async def test_install_requires_the_pinned_cli_version() -> None:
    class Sandbox(FakeSandbox):
        async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            if argv and argv[0] == "python3":
                spec = json.loads(argv[3])
                assert spec["url"] == zcode_adapter.INSTALLER_URL
                assert spec["sha512"] == zcode_adapter.INSTALLER_SHA512
                assert spec["target"] == "/home/user/.local/lib/zcode"
                return ExecResult(exit_code=0)
            if "--version" in " ".join(map(str, argv)):
                return ExecResult(exit_code=0, stdout="0.16.9\n")
            return await super().exec(argv, **kwargs)

    sandbox = Sandbox()
    sandbox.request = SimpleNamespace(os=OperatingSystem.LINUX)  # type: ignore[attr-defined]
    with pytest.raises(AgentError, match=r"does not name 0\.17\.0"):
        await ZCodeHarness().install(sandbox)  # type: ignore[arg-type]


async def test_resume_asks_the_store_for_the_exact_top_level_session() -> None:
    sandbox = FakeSandbox()
    assert await ZCodeHarness()._session_exists(sandbox, session(), SESSION_ID)  # type: ignore[arg-type]
    argv = sandbox.commands[-1]
    assert argv[0] == "python3"
    assert argv[-2:] == ["/home/agent/.zcode/cli/db/db.sqlite", SESSION_ID]
    assert "parent_id is null" in argv[2]
    assert not await ZCodeHarness()._session_exists(FakeSandbox(1), session(), SESSION_ID)  # type: ignore[arg-type]


def stdout(response: str) -> str:
    """One invocation's stdout, which ends in its result."""
    return json.dumps({"type": "result", "sessionId": SESSION_ID, "response": response}) + "\n"


def test_result_reading_uses_the_terminal_result_or_the_failed_turn() -> None:
    harness = ZCodeHarness()
    result = harness.read_result(stdout("PARENT-DONE") + stdout("SECOND-DONE"))
    assert result.session_id == SESSION_ID
    assert result.final_message == "SECOND-DONE"
    assert result.error is None
    failed = harness.read_result(
        json.dumps(
            {
                "type": "turn.failed",
                "sessionId": SESSION_ID,
                "payload": {
                    "error": {
                        "code": "invalid_model_request",
                        "message": "Provider rejected the model request.",
                        "underlyingErrorMessage": "400 refused",
                    }
                },
            }
        )
        + "\n"
    )
    assert failed.error == "400 refused; invalid_model_request"
    assert failed.session_id is None


def test_session_files_match_only_the_exact_session() -> None:
    harness = ZCodeHarness()
    assert harness.is_session_file(
        PurePosixPath(f".zcode/cli/artifacts/{SESSION_ID}/call_4-media.txt"), SESSION_ID
    )
    assert harness.is_session_file(
        PurePosixPath(f".zcode/cli/agents/{SESSION_ID}/agent_x/metadata.json"), SESSION_ID
    )
    assert not harness.is_session_file(PurePosixPath(".zcode/cli/db/db.sqlite"), SESSION_ID)
    assert not harness.is_session_file(
        PurePosixPath(f".zcode/cli/agents/{SESSION_ID}x/a/metadata.json"), SESSION_ID
    )


def context(logs: Path, tmp_path: Path, **updates: object) -> TrajectoryParseContext:
    values: dict[str, object] = {
        "episode_id": "episode",
        "trajectory_id": "trajectory",
        "instruction": PROMPTS[0],
        "logs_dir": logs,
        "model": "scripted-model",
        "agent_version": "0.17.0",
        "blobs": BlobStore(tmp_path / "episode"),
        "session_id": SESSION_ID,
    }
    return TrajectoryParseContext(**(values | updates))  # type: ignore[arg-type]


def write_run(logs: Path, *outputs: str) -> None:
    """One recorded invocation per prompt, each with its own part of stdout."""
    lines: list[str] = []
    offset = 0
    for prompt, output in zip(PROMPTS, outputs, strict=True):
        lines.append(json.dumps({"prompt": prompt, "stdout_offset": offset}) + "\n")
        offset += len(output.encode())
    (logs / ".ale-harness").mkdir(parents=True, exist_ok=True)
    (logs / ".ale-harness/invocations.jsonl").write_text("".join(lines))
    (logs / ".ale-harness/stdout.log").write_text("".join(outputs))


Message = tuple[dict[str, object], list[dict[str, object]]]


def user(text: str, **metadata: object) -> Message:
    return {"role": "user", "metadata": metadata}, [{"type": "text", "text": text}]


def assistant(*parts: dict[str, object]) -> Message:
    return {"role": "assistant"}, list(parts)


def text_part(text: str) -> dict[str, object]:
    return {"type": "text", "text": text}


def tool(
    call_id: str, name: str, args: dict[str, object], output: str, **state: object
) -> dict[str, object]:
    """A tool part holding the call's completed outcome."""
    outcome = {"status": "completed", "input": args, "output": output, **state}
    return {"type": "tool", "callID": call_id, "tool": name, "state": outcome}


def child_messages() -> list[Message]:
    """A subagent's conversation: the delegated task, then one command."""
    return [
        user("run child"),
        assistant(
            tool("call-3", "Bash", {"command": "echo hi"}, "hi"),
            {"type": "step-finish", "tokens": {"input": 7}},
        ),
    ]


def add_messages(store: sqlite3.Connection, session_id: str, *messages: Message) -> None:
    """Append messages, each with its parts, to a session in ZCode's store."""
    (count,) = store.execute(
        "select count(*) from message where session_id = ?", (session_id,)
    ).fetchone()
    for sequence, (data, parts) in enumerate(messages, count):
        message_id = f"{session_id}-{sequence}"
        store.execute(
            "insert into message values (?, ?, ?, ?)",
            (message_id, session_id, sequence, json.dumps(data)),
        )
        store.executemany(
            "insert into part values (?, ?, ?, ?)",
            [
                (f"{message_id}-{index}", message_id, index, json.dumps(part))
                for index, part in enumerate(parts)
            ],
        )


def write_spawn(logs: Path, parent: str, call_id: str, child: str) -> None:
    """The metadata naming the session and call that spawned ``child``."""
    path = logs / f".zcode/cli/agents/{parent}/agent-{child}/metadata.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"parentSessionId": parent, "parentToolUseId": call_id, "childSessionId": child})
    )


def zcode_logs(tmp_path: Path) -> Path:
    """Two turns; the first spawns a subagent session and then reads an image."""
    logs = tmp_path / "logs"
    write_run(logs, stdout("PARENT-DONE"), stdout("SECOND-DONE"))
    (logs / DB).parent.mkdir(parents=True)
    image = {"mime": "image/png", "url": f"zcode-artifact://{SESSION_ID}/image-1"}
    store = sqlite3.connect(logs / DB)
    with store:
        store.executescript(
            "create table session (id text, parent_id text);"
            "create table message (id text, session_id text, sequence integer, data text);"
            "create table part (id text, message_id text, sequence integer, data text);"
        )
        store.executemany(
            "insert into session values (?, ?)", [(SESSION_ID, None), (CHILD_ID, SESSION_ID)]
        )
        add_messages(
            store,
            SESSION_ID,
            user(PROMPTS[0]),
            user("agent types", source="agent_listing_delta", visibility="model-only"),
            assistant(tool("call-1", "Agent", {"prompt": "run child"}, "CHILD-DONE")),
            assistant(
                tool("call-2", "Read", {"file_path": "a.png"}, "1x1 pixels", attachments=[image])
            ),
            assistant(text_part("PARENT-DONE"), {"type": "step-finish", "tokens": {"input": 5}}),
            user(PROMPTS[1]),
            assistant(text_part("SECOND-DONE")),
        )
        add_messages(store, CHILD_ID, *child_messages())
    store.close()
    write_spawn(logs, SESSION_ID, "call-1", CHILD_ID)
    artifact = logs / f".zcode/cli/artifacts/{SESSION_ID}/call-2-image-1.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("data:image/png;base64," + base64.b64encode(b"png").decode())
    return logs


def test_parser_follows_the_store_and_embeds_its_subagent(tmp_path: Path) -> None:
    parse = context(zcode_logs(tmp_path), tmp_path)
    AutonomousHarnessConformance.check_parser_is_pure(ZCodeHarness(), parse)
    trajectory = ZCodeHarness().parse_trajectory(parse)

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
    (child,) = trajectory.subagent_trajectories or ()
    assert result.subagent_trajectory_ref is not None
    assert result.subagent_trajectory_ref[0].trajectory_id == child.trajectory_id
    assert child.trajectory_id == f"trajectory-subagent-{CHILD_ID}"
    assert [step.source for step in child.steps] == ["user", "agent"]
    assert child.steps[0].message == "run child"
    assert child.steps[1].observation.results[0].content == "hi"  # type: ignore[union-attr]

    (image_result,) = trajectory.steps[2].observation.results  # type: ignore[union-attr]
    assert isinstance(image_result.content, list)
    image = image_result.content[1]
    assert image.type == "image" and image.source is not None
    assert (tmp_path / "episode" / image.source.path).is_file()
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 5


def test_unlinked_subagents_and_missing_attachments_are_reported(tmp_path: Path) -> None:
    logs = zcode_logs(tmp_path)
    shutil.rmtree(logs / ".zcode/cli/agents")
    shutil.rmtree(logs / ".zcode/cli/artifacts")

    trajectory = ZCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert trajectory.subagent_trajectories is None
    issues = trajectory.extra["ale"]["parse_issues"]  # type: ignore[index]
    assert {"reason": "unlinked_subagent", "session_id": CHILD_ID} in issues
    assert any(issue["reason"] == "missing_attachment" for issue in issues)
    image_result = trajectory.steps[2].observation.results[0]  # type: ignore[union-attr]
    assert "[attachment unavailable: zcode-artifact://" in str(image_result.content)


GRANDCHILD_ID = "child-2"


def add_grandchild(logs: Path, *, spawned: bool) -> None:
    """A session below the subagent, with the subagent's own conversation.

    ``spawned`` also has the subagent spawn it as the root spawned the subagent: the call
    in a message of its own, and the metadata naming that call and the new session.
    """
    store = sqlite3.connect(logs / DB)
    with store:
        store.execute("insert into session values (?, ?)", (GRANDCHILD_ID, CHILD_ID))
        add_messages(store, GRANDCHILD_ID, *child_messages())
        if spawned:
            spawn = tool("call-9", "Agent", {"prompt": "run child"}, "CHILD-DONE")
            add_messages(store, CHILD_ID, assistant(spawn))
    store.close()
    if spawned:
        write_spawn(logs, CHILD_ID, "call-9", GRANDCHILD_ID)


def test_a_subagent_spawned_by_a_subagent_is_embedded_in_it(tmp_path: Path) -> None:
    logs = zcode_logs(tmp_path)
    add_grandchild(logs, spawned=True)

    trajectory = ZCodeHarness().parse_trajectory(context(logs, tmp_path, incomplete=True))

    assert "parse_issues" not in trajectory.extra["ale"]  # type: ignore[index]
    (helper,) = trajectory.subagent_trajectories or ()
    (nested,) = helper.subagent_trajectories or ()
    assert nested.trajectory_id == f"trajectory-subagent-{CHILD_ID}-subagent-{GRANDCHILD_ID}"
    spawned = next(
        step
        for step in helper.steps
        if [call.tool_call_id for call in step.tool_calls or ()] == ["call-9"]
    )
    (result,) = spawned.observation.results  # type: ignore[union-attr]
    assert result.subagent_trajectory_ref is not None
    assert result.subagent_trajectory_ref[0].trajectory_id == nested.trajectory_id
    # An interrupted run leaves every session in it incomplete.
    for subagent in (helper, nested):
        assert subagent.extra == {"ale": {"incomplete": True, "incomplete_reason": "interrupted"}}


def test_an_unlinked_session_below_a_subagent_is_reported_on_the_root(tmp_path: Path) -> None:
    logs = zcode_logs(tmp_path)
    add_grandchild(logs, spawned=False)

    trajectory = ZCodeHarness().parse_trajectory(context(logs, tmp_path))

    (helper,) = trajectory.subagent_trajectories or ()
    assert helper.subagent_trajectories is None
    assert trajectory.extra["ale"]["parse_issues"] == [  # type: ignore[index]
        {"reason": "unlinked_subagent", "session_id": GRANDCHILD_ID}
    ]


def test_a_failed_turn_is_recorded_at_its_end(tmp_path: Path) -> None:
    logs = zcode_logs(tmp_path)
    failed = {
        "type": "turn.failed",
        "sessionId": SESSION_ID,
        "payload": {"error": {"message": "Provider rejected the model request."}},
    }
    write_run(logs, stdout("PARENT-DONE"), json.dumps(failed) + "\n")

    trajectory = ZCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert (trajectory.steps[-1].source, trajectory.steps[-1].message) == (
        "system",
        "Provider rejected the model request.",
    )


def test_without_the_store_each_turn_comes_from_its_prompt_and_stdout(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    write_run(logs, stdout("PARENT-DONE"), stdout("SECOND-DONE"))

    trajectory = ZCodeHarness().parse_trajectory(context(logs, tmp_path))

    assert [(step.source, step.message) for step in trajectory.steps] == [
        ("user", "delegate"),
        ("agent", "PARENT-DONE"),
        ("user", "second"),
        ("agent", "SECOND-DONE"),
    ]
    ale = trajectory.extra["ale"]  # type: ignore[index]
    assert {"reason": "native_session_unavailable", "session_id": SESSION_ID} in ale["parse_issues"]
