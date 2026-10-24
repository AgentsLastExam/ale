"""Native CLI configuration and invocation."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from pydantic import ValidationError

from ale.core.errors import AgentError, ConfigError, NativeContinuationError
from ale.core.harness import (
    EffectiveAgentResources,
    HarnessSession,
    ResumeSupport,
    TrajectoryParseContext,
)
from ale.core.sandbox import ExecResult, Identity, Sandbox
from ale.core.taskspec import OperatingSystem, StdioMcpServer, StreamableHttpMcpServer
from ale.core.trajectory import (
    AtifTrajectory,
)
from ale.run.harnesses._diagnostics import failure_detail
from ale.run.harnesses._npm import agent_path, ensure_npm_cli, npm_env
from ale.run.harnesses.base import CliCommand, CliHarness, NativeLogs, NativeResult
from ale.run.harnesses.evidence import evidence_scope
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from .settings import OpenClawCliSettings
from .trajectory import _envelope_message, _json_object, _native_error, parse_trajectory

DEFAULT_CLI_VERSION = "2026.7.1"


class OpenClawCliHarness(CliHarness):
    name = "openclaw-cli"

    supported_os = (OperatingSystem.LINUX, OperatingSystem.WINDOWS)

    resume_support = ResumeSupport.NATIVE

    __slots__ = ("cli_version", "settings")

    native_logs = (NativeLogs(".openclaw-ale/agents/*/sessions"),)

    async def prepare_command(
        self,
        instruction_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand:
        home = agent_path(session.home)
        state = home / ".openclaw-ale"
        if native_session_id is not None:
            native_path = state / "agents/main/sessions" / f"{native_session_id}.jsonl"
            result = await sandbox.exec(["test", "-f", str(native_path)], identity=Identity.AGENT)
            if not result.ok:
                raise NativeContinuationError(
                    f"native OpenClaw session {native_session_id!r} is absent"
                )
        requested = native_session_id or str(uuid.uuid4())
        argv = [
            "openclaw",
            "agent",
            "--local",
            "--agent",
            "main",
            "--session-id",
            requested,
            "--message-file",
            instruction_path,
            "--model",
            f"{self.settings.provider}/{session.model}",
            "--json",
        ]
        if self.settings.thinking != "default":
            argv.extend(["--thinking", self.settings.thinking])
        if self.settings.timeout_seconds == "unlimited":
            argv.extend(["--timeout", "0"])
        elif isinstance(self.settings.timeout_seconds, int):
            argv.extend(["--timeout", str(self.settings.timeout_seconds)])
        env = {
            **await npm_env(sandbox, session.home),
            "OPENCLAW_HOME": str(state),
            "OPENCLAW_STATE_DIR": str(state),
            "OPENCLAW_CONFIG_PATH": str(state / "openclaw.json"),
            "OPENCLAW_WORKSPACE_DIR": str(state / "workspace"),
            "ALE_GATEWAY_TOKEN": session.token,
            "NO_COLOR": "1",
        }
        return CliCommand(tuple(argv), env, requested)

    def read_result(self, result: ExecResult, session: HarnessSession) -> NativeResult:
        envelope = _json_object(result.stdout)
        native_error = _native_error(envelope) if envelope is not None else None
        if result.exit_code != 0 or native_error or envelope is None:
            raise AgentError(
                failure_detail(
                    self.name,
                    result.exit_code,
                    native=native_error
                    or ("Invalid native result JSON" if result.exit_code == 0 else ""),
                    stderr=result.stderr,
                    stdout=result.stdout,
                    token=session.token,
                )
            )
        session_id = ((envelope.get("meta") or {}).get("agentMeta") or {}).get("sessionId")
        return NativeResult(
            session_id if isinstance(session_id, str) else None, _envelope_message(envelope)
        )

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        settings: OpenClawCliSettings | dict[str, Any] | None = None,
    ) -> None:
        self.cli_version = cli_version
        try:
            self.settings = (
                settings
                if isinstance(settings, OpenClawCliSettings)
                else OpenClawCliSettings.model_validate(settings or {})
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid openclaw-cli settings: {exc}") from exc
        if self.settings.tools_allow and self.settings.tool_profile != "full":
            raise ConfigError("OpenClaw tools_allow requires tool_profile='full'")

    def version(self) -> str:
        return self.cli_version or DEFAULT_CLI_VERSION

    def integrity(self) -> str:
        return f"npm:openclaw@{self.version()}"

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        for resolved in resources.mcp_servers:
            if not isinstance(resolved.server, StdioMcpServer | StreamableHttpMcpServer):
                raise ConfigError(f"unsupported MCP transport for {resolved.name}")

    async def install(self, sandbox: Sandbox) -> str:
        node = await sandbox.exec(["node", "--version"], timeout_sec=30, identity=Identity.AGENT)
        if not node.ok or not _supported_node(node.stdout):
            raise AgentError(
                "OpenClaw 2026.7.1 requires Node >=22.22.3; rebuild the ALE base image"
            )
        return await ensure_npm_cli(
            sandbox,
            package="openclaw",
            binary="openclaw",
            version=self.version(),
        )

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        state = agent_path(session.home) / ".openclaw-ale"
        workspace = state / "workspace"
        skills_root = workspace / "skills"
        await sandbox.exec(
            ["mkdir", "-p", str(skills_root), str(state / "agents/main/sessions")],
            identity=Identity.AGENT,
        )
        for skill in resources.skills:
            target = skills_root / skill.name
            await sandbox.upload_dir(str(skill.path), target, identity=Identity.AGENT)
            if skill.executable_files:
                await sandbox.exec(
                    ["chmod", "+x", *(str(target / path) for path in skill.executable_files)],
                    identity=Identity.AGENT,
                )

        model_ref = f"{self.settings.provider}/{session.model}"
        model_entry: dict[str, Any] = {"agentRuntime": {"id": "openclaw"}}
        if self.settings.model_params:
            model_entry["params"] = self.settings.model_params
        provider_model: dict[str, Any] = {
            "id": session.model,
            "name": session.model,
            "input": ["text", "image"],
            "agentRuntime": {"id": "openclaw"},
        }
        if self.settings.thinking != "default":
            provider_model["reasoning"] = self.settings.thinking != "off"
        tools: dict[str, Any] = {
            "profile": self.settings.tool_profile,
            "deny": list(self.settings.tools_deny),
        }
        if self.settings.tools_allow:
            tools["allow"] = list(self.settings.tools_allow)
        config: dict[str, Any] = {
            "agents": {
                "defaults": {
                    "workspace": str(workspace),
                    "skipBootstrap": True,
                    "model": {"primary": model_ref},
                    "models": {model_ref: model_entry},
                }
            },
            "models": {
                "pricing": {"enabled": False},
                "providers": {
                    self.settings.provider: {
                        "baseUrl": session.gateway_url + "/v1",
                        "apiKey": "${ALE_GATEWAY_TOKEN}",
                        "api": "openai-responses",
                        "models": [provider_model],
                    }
                },
            },
            "skills": {"allowBundled": []},
            "tools": tools,
            "gateway": {"mode": "local", "bind": "loopback"},
            "mcp": {"servers": {}},
        }
        if isinstance(self.settings.timeout_seconds, int):
            config["agents"]["defaults"]["timeoutSeconds"] = self.settings.timeout_seconds
        for resolved in resources.mcp_servers:
            server = resolved.server
            if resolved.name == CUA_DESKTOP_NAME:
                await stage_cua_desktop(sandbox, session.home)
            if isinstance(server, StdioMcpServer):
                native: dict[str, Any] = {
                    "command": server.command.replace("{home}", session.home),
                    "args": [arg.replace("{home}", session.home) for arg in server.args],
                }
                if server.cwd:
                    native["cwd"] = server.cwd.replace("{home}", session.home)
                if server.environment:
                    native["env"] = {
                        key: value.replace("{home}", session.home)
                        for key, value in server.environment.items()
                    }
            else:
                native = {"url": server.url, "transport": "streamable-http"}
            config["mcp"]["servers"][resolved.name] = native
        await sandbox.write_file(
            state / "openclaw.json",
            (json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
            identity=Identity.AGENT,
        )
        await sandbox.write_file(
            state / "exec-approvals.json",
            (
                json.dumps(
                    {
                        "version": 1,
                        "defaults": {
                            "security": "full",
                            "ask": "off",
                            "askFallback": "full",
                        },
                        "socket": {},
                        "agents": {},
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode(),
            identity=Identity.AGENT,
        )

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return evidence_scope(parse_trajectory(context), context)


def _supported_node(output: str) -> bool:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", output)
    if not match:
        return False
    version = tuple(int(part) for part in match.groups())
    return (
        version >= (25, 9, 0)
        or (24, 15, 0) <= version < (25, 0, 0)
        or (22, 22, 3) <= version < (23, 0, 0)
    )
