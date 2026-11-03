"""Factory Droid configuration and invocation."""

from __future__ import annotations

import json
import shlex
import uuid
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel

from ale.core.errors import AgentUnsupportedError, ConfigError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses._npm import agent_path, npm_env
from ale.run.harnesses.base import (
    LOG_ROOT,
    CliCommand,
    CliHarness,
    NativeResult,
    install_skills,
)
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import DroidSettings

DEFAULT_CLI_VERSION = "0.249.0"
#: Droid keeps all of its state in ``<FACTORY_HOME_OVERRIDE>/.factory``.
STATE = trajectory.STATE
#: The custom model's display name; Droid addresses it as ``custom:<name, dashed>-<index>``.
_DISPLAY_NAME = "ALE Gateway"
_MODEL_ID = "custom:ALE-Gateway-0"
#: The BYOK provider type that speaks each Gateway dialect. Droid appends ``/v1/messages``
#: to an Anthropic base URL itself, and ``/chat/completions`` or ``/responses`` to the others.
_PROVIDERS = {
    "anthropic": ("anthropic", ""),
    "openai-chat-completions": ("generic-chat-completion-api", "/v1"),
    "openai-responses": ("openai", "/v1"),
}
#: Task subagents run through Factory's daemon, which requires a signed-in Factory user;
#: without one every call fails with "Could not reach the daemon".
_FACTORY_ACCOUNT_TOOLS = ("Task",)


class DroidHarness(CliHarness):
    """Runs ``droid exec`` against one BYOK custom model that is the episode Gateway."""

    name = "droid"
    package = "droid"
    binary = "droid"
    default_version = DEFAULT_CLI_VERSION
    settings_type = DroidSettings
    settings: DroidSettings
    logs = (LOG_ROOT, trajectory.SESSIONS)

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "anthropic",
        settings: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if gateway_dialect not in _PROVIDERS:
            raise ConfigError(f"droid does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        super().__init__(cli_version=cli_version, settings=settings)

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        for resolved in resources.mcp_servers:
            if isinstance(resolved.server, StdioMcpServer) and resolved.server.cwd:
                raise AgentUnsupportedError(
                    f"droid stdio MCP servers have no working directory: {resolved.name}"
                )

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        state = agent_path(session.home) / STATE
        skills_root = state / "skills"
        await sandbox.exec(["mkdir", "-p", str(skills_root)], identity=Identity.AGENT)
        await install_skills(sandbox, session, resources, skills_root)

        servers: dict[str, dict[str, Any]] = {}
        for resolved in resources.mcp_servers:
            server = resolved.server
            if resolved.name == CUA_DESKTOP_NAME:
                await stage_cua_desktop(sandbox, session.home)
            if isinstance(server, StdioMcpServer):
                native: dict[str, Any] = {
                    "type": "stdio",
                    "command": server.command.replace("{home}", session.home),
                    "args": [arg.replace("{home}", session.home) for arg in server.args],
                }
                if server.environment:
                    native["env"] = {
                        key: value.replace("{home}", session.home)
                        for key, value in server.environment.items()
                    }
            else:
                # Remote MCP authentication is never copied from the Host, so there is
                # nothing for an OAuth flow to sign in with.
                native = {"type": "http", "url": server.url, "oauth": False}
            servers[resolved.name] = native
        await _write_json(sandbox, state / "mcp.json", {"mcpServers": servers})

        provider, suffix = _PROVIDERS[self.gateway_dialect]
        model: dict[str, Any] = {
            "model": session.model,
            "displayName": _DISPLAY_NAME,
            "baseUrl": session.gateway_url + suffix,
            # Resolved from the environment for each request; the file keeps the reference.
            "apiKey": "${ALE_GATEWAY_TOKEN}",
            "provider": provider,
        }
        if isinstance(self.settings.max_output_tokens, int):
            model["maxOutputTokens"] = self.settings.max_output_tokens
        await _write_json(
            sandbox,
            state / "settings.json",
            {"customModels": [model], "cloudSessionSync": False},
        )

    async def prepare_command(
        self,
        prompt_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand:
        # `-s` resumes the session with this ID, or starts a new one under it.
        requested = native_session_id or str(uuid.uuid4())
        removed = dict.fromkeys((*_FACTORY_ACCOUNT_TOOLS, *self.settings.disabled_tools))
        argv = [
            "droid",
            "exec",
            "--output-format",
            "stream-json",
            "--skip-permissions-unsafe",
            "--cwd",
            session.start_dir,
            "--model",
            _MODEL_ID,
            "--remove-tools",
            ",".join(removed),
            "--session-id",
            requested,
            "--file",
            prompt_path,
        ]
        env = {
            **await npm_env(sandbox, session.home),
            "FACTORY_HOME_OVERRIDE": str(agent_path(session.home) / PurePosixPath(STATE).parent),
            "FACTORY_DROID_AUTO_UPDATE_ENABLED": "false",
            "OTEL_CUSTOMER_ENABLED": "false",
            "ALE_GATEWAY_TOKEN": session.token,
            "NO_COLOR": "1",
        }
        return CliCommand("unset FACTORY_API_KEY; " + shlex.join(argv), env, requested)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)


async def _write_json(sandbox: Sandbox, path: PurePosixPath, value: dict[str, Any]) -> None:
    await sandbox.write_file(
        str(path),
        (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
        identity=Identity.AGENT,
    )
