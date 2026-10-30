"""Pi coding agent configuration and invocation."""

from __future__ import annotations

import json
import shlex
import uuid
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel

from ale.core.errors import ConfigError
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
from .settings import PiCliSettings

DEFAULT_CLI_VERSION = "1.4.0"
#: ``PI_CODING_AGENT_DIR``: models, settings, MCP servers and Skills.
AGENT_DIR = ".pi-ale/agent"
#: The custom provider every model call goes through.
_PROVIDER = "ale"
#: Pi's API type for each Gateway dialect. The Anthropic client appends ``/v1/messages``
#: to its base URL itself; the OpenAI clients append only the endpoint.
_APIS = {
    "anthropic": ("anthropic-messages", ""),
    "openai-chat-completions": ("openai-completions", "/v1"),
    "openai-responses": ("openai-responses", "/v1"),
}


class PiCliHarness(CliHarness):
    """Runs pi's JSON mode with the prompt on stdin and one exact native session."""

    name = "pi-cli"
    package = "@earendil-works/pi-coding-agent"
    binary = "pi"
    default_version = DEFAULT_CLI_VERSION
    settings_type = PiCliSettings
    settings: PiCliSettings
    logs = (LOG_ROOT, trajectory.SESSIONS)

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "openai-chat-completions",
        settings: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if gateway_dialect not in _APIS:
            raise ConfigError(f"pi-cli does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        super().__init__(cli_version=cli_version, settings=settings)

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        home = agent_path(session.home)
        agent_dir = home / AGENT_DIR
        skills_root = agent_dir / "skills"
        await sandbox.exec(
            ["mkdir", "-p", str(skills_root), str(home / trajectory.SESSIONS)],
            identity=Identity.AGENT,
        )
        await install_skills(sandbox, session, resources, skills_root)

        servers: dict[str, dict[str, Any]] = {}
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
                native = {"url": server.url}
            # Declared to the model like built-in tools. Pi's default, codemode, reaches
            # them only from a script and does not record their results in the session.
            native["exposure"] = "direct"
            servers[resolved.name] = native
        await _write_json(sandbox, agent_dir / "mcp.json", {"mcpServers": servers})

        api, suffix = _APIS[self.gateway_dialect]
        model: dict[str, Any] = {"id": session.model, "input": ["text", "image"]}
        if self.settings.thinking not in {"default", "off"}:
            model["reasoning"] = True
        if isinstance(self.settings.context_window, int):
            model["contextWindow"] = self.settings.context_window
        if isinstance(self.settings.max_output_tokens, int):
            model["maxTokens"] = self.settings.max_output_tokens
        await _write_json(
            sandbox,
            agent_dir / "models.json",
            {
                "providers": {
                    _PROVIDER: {
                        "baseUrl": session.gateway_url + suffix,
                        "api": api,
                        # Interpolated from the environment; the file keeps the reference.
                        "apiKey": "$ALE_GATEWAY_TOKEN",
                        "models": [model],
                    }
                }
            },
        )
        await _write_json(
            sandbox,
            agent_dir / "settings.json",
            {
                "defaultProvider": _PROVIDER,
                "defaultModel": session.model,
                "quietStartup": True,
                "enableInstallTelemetry": False,
            },
        )

    async def prepare_command(
        self,
        prompt_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand:
        home = agent_path(session.home)
        # `--session-id` opens the exact session, or creates it under this ID.
        requested = native_session_id or str(uuid.uuid4())
        argv = [
            "pi",
            "--mode",
            "json",
            "--provider",
            _PROVIDER,
            "--model",
            session.model,
            "--session-dir",
            str(home / trajectory.SESSIONS),
            "--session-id",
            requested,
        ]
        if self.settings.thinking != "default":
            argv.extend(["--thinking", self.settings.thinking])
        if self.settings.disabled_tools:
            argv.extend(["--exclude-tools", ",".join(self.settings.disabled_tools)])
        env = {
            **await npm_env(sandbox, session.home),
            "PI_CODING_AGENT_DIR": str(home / AGENT_DIR),
            # No startup network use: version checks, catalog refreshes and telemetry.
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            "ALE_GATEWAY_TOKEN": session.token,
            "NO_COLOR": "1",
        }
        return CliCommand(f"{shlex.join(argv)} < {shlex.quote(prompt_path)}", env, requested)

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
