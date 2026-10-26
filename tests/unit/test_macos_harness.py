from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.errors import AgentError
from ale.core.harness import EffectiveAgentResources, HarnessSession, ResolvedMcpServer
from ale.core.sandbox import ExecResult, Identity, SandboxFile
from ale.core.taskspec import OperatingSystem, StdioMcpServer
from ale.run.environments.standard import StandardEnvironment
from ale.run.harnesses._npm import npm_env
from ale.run.harnesses.builtin import NopHarness, OracleHarness, ScriptedPolicyHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.harnesses.codex_cli import CodexCliHarness
from ale.run.harnesses.computer_use import ComputerUseHarness
from ale.run.harnesses.grok_build import GrokBuildHarness
from ale.run.harnesses.openclaw_cli import OpenClawCliHarness
from ale.run.tools import resolved_cua_desktop

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("operating_system", [OperatingSystem.MACOS, OperatingSystem.LINUX])
async def test_staged_mcp_command_is_executable_after_upload(tmp_path, operating_system) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "server.sh").write_text("#!/bin/sh\nprintf ready")
    (source / "data.txt").write_text("data")

    async def upload(source_dir, target, **kwargs) -> None:
        assert kwargs["identity"] is Identity.AGENT
        shutil.copytree(source_dir, target)
        # guestd uploads regular files as 0644, regardless of their host mode.
        for path in Path(target).iterdir():
            path.chmod(0o644)

    async def execute(argv, **kwargs) -> ExecResult:
        assert kwargs["identity"] is Identity.AGENT
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        return ExecResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)

    ctx = SimpleNamespace(
        spec=SimpleNamespace(os=operating_system),
        home=str(tmp_path / "guest"),
        agent_resources=EffectiveAgentResources(
            mcp_servers=(
                ResolvedMcpServer(
                    name="local-test",
                    server=StdioMcpServer(
                        name="local-test", transport="stdio", command="{mcp}/server.sh"
                    ),
                    staged_files=source,
                    source_layers=("task",),
                    declared_sources=("tools/test.toml",),
                    digest="sha256:" + "0" * 64,
                    reportable=True,
                ),
            )
        ),
    )
    sandbox = SimpleNamespace(upload_dir=upload, exec=execute)
    environment = StandardEnvironment(NopHarness())
    ctx.agent_resources = await environment._stage_mcp_files(ctx, sandbox)
    await environment._validate_stdio_mcp(ctx, sandbox)
    command = ctx.agent_resources.mcp_servers[0].server.command
    result = await execute([command], identity=Identity.AGENT)
    assert result.ok and result.stdout == "ready"
    assert (Path(command).parent / "data.txt").stat().st_mode & 0o111 == 0


@pytest.mark.parametrize(
    "harness",
    [
        CodexCliHarness(),
        ClaudeCodeHarness(),
        GrokBuildHarness(),
        OpenClawCliHarness(),
        ComputerUseHarness(),
        NopHarness(),
        OracleHarness(),
        ScriptedPolicyHarness(),
    ],
)
def test_macos_harnesses_accept_the_guest_system(harness) -> None:
    harness.validate_os(OperatingSystem.MACOS)


async def test_macos_npm_path_includes_apple_silicon_runtime() -> None:
    async def execute(argv, **kwargs: object) -> ExecResult:
        assert argv == ["sh", "-c", 'printf %s "$PATH"']
        assert kwargs["identity"] is Identity.AGENT
        return ExecResult(exit_code=0, stdout="/opt/homebrew/bin:/custom/bin:/usr/bin:/bin")

    env = await npm_env(SimpleNamespace(exec=execute), "/Users/agent")
    assert "/opt/homebrew/bin" in env["PATH"].split(":")
    assert env["PATH"].split(":")[0] == "/Users/agent/.local/bin"
    assert "/custom/bin" in env["PATH"].split(":")


async def test_macos_oracle_uses_agent_account_and_posix_path() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    async def execute(argv: list[str], **kwargs: object) -> ExecResult:
        calls.append((argv, kwargs))
        return ExecResult(exit_code=0, stdout="done")

    session = HarnessSession(
        episode_id="episode",
        gateway_url="http://gateway",
        token="episode-token",
        model="test",
        sandbox_id="sandbox",
        resources_digest="sha256:" + "a" * 64,
        home="/Users/agent",
        os=OperatingSystem.MACOS,
    )
    await OracleHarness().launch("work", SimpleNamespace(exec=execute), session, timeout_sec=10)
    assert calls[0][0] == ["bash", "/Users/agent/.ale-oracle/run.sh"]
    assert calls[0][1]["identity"] is Identity.AGENT


@pytest.mark.parametrize(
    "harness", [CodexCliHarness(), ClaudeCodeHarness(), GrokBuildHarness(), OpenClawCliHarness()]
)
async def test_macos_native_launch_uses_episode_gateway_and_agent_identity(harness) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    files: dict[str, bytes] = {}

    async def execute(argv: list[str], **kwargs: object) -> ExecResult:
        calls.append((argv, kwargs))
        if kwargs.get("timeout_sec") == 10:
            return ExecResult(exit_code=1, stderr="fixture launch stopped")
        return ExecResult(exit_code=0, stdout="/opt/homebrew/bin:/usr/bin:/bin")

    async def write(path, data: bytes, **kwargs: object) -> None:
        assert kwargs["identity"] is Identity.AGENT
        files[str(path)] = data

    async def read(path, **kwargs: object) -> bytes:
        return files.get(str(path), b"")

    async def list_files(path) -> tuple[SandboxFile, ...]:
        return tuple(
            SandboxFile(path=name, size=len(data))
            for name, data in files.items()
            if name.startswith(str(path) + "/")
        )

    session = HarnessSession(
        episode_id="episode",
        gateway_url="http://episode-gateway:8080",
        token="episode-token",
        model="test",
        sandbox_id="sandbox",
        resources_digest="sha256:" + "a" * 64,
        home="/Users/agent",
        os=OperatingSystem.MACOS,
    )
    sandbox = SimpleNamespace(exec=execute, write_file=write, read_file=read, list_files=list_files)
    await harness.install_resources(sandbox, session, EffectiveAgentResources())
    with pytest.raises(AgentError, match="fixture launch stopped"):
        await harness.launch("work", sandbox, session, timeout_sec=10)
    argv, options = next((argv, opts) for argv, opts in calls if opts.get("timeout_sec") == 10)
    assert argv[:2] == ["sh", "-c"]
    assert files["/Users/agent/.ale-harness/prompt.md"] == b"work"
    assert "/Users/agent/.ale-harness/stdout.log" in argv[2]
    assert options["identity"] is Identity.AGENT
    assert options["cwd"] == "/Users/agent"
    env = options["env"]
    assert "/opt/homebrew/bin" in env["PATH"].split(":")
    if isinstance(harness, CodexCliHarness):
        assert env["CODEX_HOME"] == "/Users/agent/.codex-ale"
        assert env["ALE_GATEWAY_TOKEN"] == session.token
        assert session.gateway_url.encode() in files["/Users/agent/.codex-ale/config.toml"]
    elif isinstance(harness, ClaudeCodeHarness):
        assert env["CLAUDE_CONFIG_DIR"] == "/Users/agent/.claude-config"
        assert env["ANTHROPIC_BASE_URL"] == session.gateway_url
        assert env["ANTHROPIC_AUTH_TOKEN"] == session.token
    elif isinstance(harness, GrokBuildHarness):
        assert env["GROK_HOME"] == "/Users/agent/.grok-ale"
        assert env["ALE_GATEWAY_TOKEN"] == session.token
        config = tomllib.loads(files["/Users/agent/.grok-ale/config.toml"].decode())
        assert config["model"]["test"]["base_url"] == session.gateway_url + "/v1"
        assert "/Users/agent/.grok-build-install/bin/grok" in argv[2]
    else:
        assert env["OPENCLAW_HOME"] == "/Users/agent/.openclaw-ale"
        assert env["ALE_GATEWAY_TOKEN"] == session.token
        config = json.loads(files["/Users/agent/.openclaw-ale/openclaw.json"])
        provider = config["models"]["providers"][harness.settings.provider]
        assert provider["baseUrl"] == session.gateway_url + "/v1"
        assert provider["apiKey"] == "${ALE_GATEWAY_TOKEN}"


@pytest.mark.parametrize("operating_system", [OperatingSystem.MACOS, OperatingSystem.LINUX])
@pytest.mark.parametrize("harness", [CodexCliHarness(), OpenClawCliHarness()])
async def test_builtin_desktop_explicitly_inherits_only_macos_driver_settings(
    operating_system: OperatingSystem,
    harness,
) -> None:
    files: dict[str, bytes] = {}

    async def write(path, data: bytes, **kwargs: object) -> None:
        files[str(path)] = data

    async def execute(argv, **kwargs: object) -> ExecResult:
        return ExecResult(exit_code=0)

    sandbox = SimpleNamespace(
        exec=execute, write_file=write, request=SimpleNamespace(os=operating_system)
    )
    session = HarnessSession(
        episode_id="episode",
        gateway_url="http://gateway",
        token="episode-token",
        model="test",
        sandbox_id="sandbox",
        resources_digest="sha256:" + "a" * 64,
        home="/Users/agent",
        os=operating_system,
    )
    resources = EffectiveAgentResources(mcp_servers=(resolved_cua_desktop(operating_system),))
    await harness.install_resources(sandbox, session, resources)
    if isinstance(harness, CodexCliHarness):
        config = tomllib.loads(files["/Users/agent/.codex-ale/config.toml"].decode())
        desktop = config["mcp_servers"]["cua-desktop"]
        if operating_system is OperatingSystem.MACOS:
            assert desktop["env_vars"] == ["ALE_CUA_DRIVER", "ALE_CUA_DRIVER_SOCKET"]
        else:
            assert "env_vars" not in desktop
    else:
        config = json.loads(files["/Users/agent/.openclaw-ale/openclaw.json"])
        desktop = config["mcp"]["servers"]["cua-desktop"]
        if operating_system is OperatingSystem.MACOS:
            assert desktop["env"] == {
                "ALE_CUA_DRIVER": "${ALE_CUA_DRIVER}",
                "ALE_CUA_DRIVER_SOCKET": "${ALE_CUA_DRIVER_SOCKET}",
            }
        else:
            assert "env" not in desktop
