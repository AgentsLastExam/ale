"""Kimi Code configuration and invocation."""

from __future__ import annotations

import json
import shlex
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel

from ale.core.errors import ConfigError, HarnessLimitError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses._npm import agent_path, is_windows, npm_env
from ale.run.harnesses.base import (
    LOG_ROOT,
    PROMPT_ARGUMENT_WINDOWS_ENV,
    CliCommand,
    CliHarness,
    NativeResult,
    install_skills,
    prompt_argument,
)
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import KimiCodeSettings

DEFAULT_CLI_VERSION = "2.3.2"
#: Each Gateway dialect's native provider type, and whether its SDK appends ``/v1`` itself.
_PROVIDERS = {
    "anthropic": ("anthropic", ""),
    "openai-chat-completions": ("kimi", "/v1"),
    "openai-responses": ("openai_responses", "/v1"),
}
#: Variables that would replace the configured model, its endpoint, or a configured limit.
_OVERRIDES = (
    "KIMI_MODEL_NAME",
    "KIMI_MODEL_API_KEY",
    "KIMI_MODEL_BASE_URL",
    "KIMI_MODEL_PROVIDER_TYPE",
    "KIMI_MODEL_THINKING_EFFORT",
    "KIMI_LOOP_MAX_STEPS_PER_TURN",
)


class KimiCodeHarness(CliHarness):
    """Runs Kimi Code's print mode (``kimi --prompt``) inside the sandbox."""

    name = "kimi-code"
    package = "@moonshot-ai/kimi-code"
    binary = "kimi"
    default_version = DEFAULT_CLI_VERSION
    settings_type = KimiCodeSettings
    settings: KimiCodeSettings
    logs = (LOG_ROOT, trajectory.SESSIONS)

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "openai-chat-completions",
        settings: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if gateway_dialect not in _PROVIDERS:
            raise ConfigError(f"kimi-code does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        super().__init__(cli_version=cli_version, settings=settings)

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        kimi_home = agent_path(session.home) / trajectory.HOME
        skills_root = kimi_home / "skills"
        await sandbox.exec(["mkdir", "-p", str(skills_root)], identity=Identity.AGENT)
        await install_skills(sandbox, session, resources, skills_root)

        provider_type, suffix = _PROVIDERS[self.gateway_dialect]
        model = _toml(session.model)
        lines = [
            f"default_model = {model}",
            "telemetry = false",
            "",
            "[providers.ale]",
            f"type = {_toml(provider_type)}",
            f"base_url = {_toml(session.gateway_url + suffix)}",
            'api_key_env = "ALE_GATEWAY_TOKEN"',
            "",
            f"[models.{model}]",
            'provider = "ale"',
            f"model = {model}",
            f"max_context_size = {self.settings.max_context_size}",
        ]
        if self.settings.capabilities:
            rendered = ", ".join(_toml(value) for value in self.settings.capabilities)
            lines.append(f"capabilities = [{rendered}]")
        if self.settings.thinking_effort == "off":
            lines.extend(["", "[thinking]", "enabled = false"])
        elif self.settings.thinking_effort != "default":
            lines.extend(
                [
                    "",
                    "[thinking]",
                    "enabled = true",
                    f"effort = {_toml(self.settings.thinking_effort)}",
                ]
            )
        if self.settings.max_steps_per_turn != "default":
            # Kimi reads 0 as no step ceiling.
            steps = (
                0
                if self.settings.max_steps_per_turn == "unlimited"
                else self.settings.max_steps_per_turn
            )
            lines.extend(["", "[loop_control]", f"max_steps_per_turn = {steps}"])
        await sandbox.write_file(
            kimi_home / "config.toml",
            ("\n".join(lines) + "\n").encode(),
            identity=Identity.AGENT,
        )

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
                # An entry with a URL and no transport is a Streamable HTTP server.
                native = {"url": server.url}
            servers[resolved.name] = native
        await sandbox.write_file(
            kimi_home / "mcp.json",
            (json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2) + "\n").encode(),
            identity=Identity.AGENT,
        )

    async def prepare_command(
        self,
        prompt_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand:
        kimi_home = agent_path(session.home) / trajectory.HOME
        argv = [
            "kimi",
            "--output-format",
            "stream-json",
            # Replaces discovery of the operator's and the project's Skill directories.
            "--skills-dir",
            str(kimi_home / "skills"),
        ]
        if native_session_id is not None:
            argv.extend(["--session", native_session_id])
        # Print mode takes the prompt only as an argument.
        read_prompt, prompt = prompt_argument(prompt_path)
        script = f"unset {' '.join(_OVERRIDES)}; {read_prompt}{shlex.join(argv)} --prompt={prompt}"
        env = {
            **await npm_env(sandbox, session.home),
            "KIMI_CODE_HOME": str(kimi_home),
            "KIMI_CODE_NO_AUTO_UPDATE": "1",
            "NO_COLOR": "1",
            "ALE_GATEWAY_TOKEN": session.token,
        }
        if is_windows(session.home):
            env.update(PROMPT_ARGUMENT_WINDOWS_ENV)
            env["KIMI_SHELL_PATH"] = "C:/Program Files/Git/bin/bash.exe"
        # Kimi chooses a new session's ID itself, so only a resume asks for one.
        return CliCommand(script, env, native_session_id)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)

    def classify_failure(self, detail: str) -> Exception:
        steps = self.settings.max_steps_per_turn
        if isinstance(steps, int) and "loop.max_steps_exceeded" in detail:
            return HarnessLimitError("max_steps_per_turn", steps, steps)
        return super().classify_failure(detail)


def _toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
