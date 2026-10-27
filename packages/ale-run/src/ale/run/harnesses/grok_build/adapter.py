"""Grok Build configuration and invocation."""

from __future__ import annotations

import json
import re
import shlex
import uuid
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel

from ale.core.errors import AgentError, ConfigError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses._npm import (
    agent_home,
    agent_path,
    bash_command,
    is_windows,
    npm_command,
    npm_env,
)
from ale.run.harnesses.base import LOG_ROOT, CliCommand, CliHarness, NativeResult
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import GrokBuildSettings

DEFAULT_CLI_VERSION = "0.2.112"
_HEADLESS_DISABLED_TOOLS = ("ask_user_question", "enter_plan_mode", "exit_plan_mode")
_API_BACKENDS = {
    "anthropic": "messages",
    "openai-chat-completions": "chat_completions",
    "openai-responses": "responses",
}


class GrokBuildHarness(CliHarness):
    """Runs Grok Build's headless mode inside the sandbox."""

    name = "grok-build"
    package = "@xai-official/grok"
    binary = "grok"
    default_version = DEFAULT_CLI_VERSION
    settings_type = GrokBuildSettings
    settings: GrokBuildSettings
    logs = (LOG_ROOT, ".grok-ale/sessions")

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "openai-responses",
        settings: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if gateway_dialect not in _API_BACKENDS:
            raise ConfigError(f"grok-build does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        super().__init__(cli_version=cli_version, settings=settings)

    async def install(self, sandbox: Sandbox) -> str:
        home = await agent_home(sandbox)
        install_home = f"{home}/.grok-build-install"
        env = {
            **await npm_env(sandbox, home),
            "GROK_HOME": install_home,
            "GROK_MANAGED_BY_NPM": "1",
        }
        binary = f"{install_home}/bin/grok" + (".exe" if is_windows(home) else "")
        probe = await sandbox.exec(
            bash_command(
                home,
                f"test -x {shlex.quote(binary)} && {shlex.quote(binary)} --version",
                login=False,
            ),
            env=env,
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        installed = _version(probe.stdout + probe.stderr) if probe.ok else None
        if installed != self.version():
            result = await sandbox.exec(
                npm_command(
                    home,
                    "install",
                    "-g",
                    "--force",
                    "--prefix",
                    f"{home}/.local",
                    f"@xai-official/grok@{self.version()}",
                ),
                env=env,
                timeout_sec=1200,
                identity=Identity.AGENT,
            )
            if not result.ok:
                raise AgentError(
                    f"could not install Grok Build {self.version()}: "
                    f"{(result.stderr or result.stdout)[-800:]}"
                )
            probe = await sandbox.exec(
                [binary, "--version"], env=env, timeout_sec=60, identity=Identity.AGENT
            )
            installed = _version(probe.stdout + probe.stderr) if probe.ok else None
        if installed != self.version():
            raise AgentError(
                f"Grok Build reports {installed or 'no version'} after installing {self.version()}"
            )
        return installed

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        grok_home = agent_path(session.home) / ".grok-ale"
        skills_root = grok_home / "skills"
        await sandbox.exec(["mkdir", "-p", str(skills_root)], identity=Identity.AGENT)
        for skill in resources.skills:
            target = skills_root / skill.name
            await sandbox.upload_dir(str(skill.path), target, identity=Identity.AGENT)
            if skill.executable_files:
                await sandbox.exec(
                    ["chmod", "+x", *(str(target / path) for path in skill.executable_files)],
                    identity=Identity.AGENT,
                )

        lines = [
            "[models]",
            f"default = {_toml(session.model)}",
            "",
            f"[model.{_toml(session.model)}]",
            f"model = {_toml(session.model)}",
            f"base_url = {_toml(session.gateway_url + '/v1')}",
            'name = "ALE Gateway"',
            'env_key = "ALE_GATEWAY_TOKEN"',
            f"api_backend = {_toml(_API_BACKENDS[self.gateway_dialect])}",
            "supports_reasoning_effort = true",
            "",
            "[harness]",
            "disable_codebase_upload = true",
            "",
        ]
        lines.extend(
            [
                "[features]",
                "telemetry = false",
                "",
                "[telemetry]",
                "trace_upload = false",
                "mixpanel_enabled = false",
                "",
                "[cli]",
                "auto_update = false",
                "",
                "[compat.cursor]",
                "skills = false",
                "rules = false",
                "agents = false",
                "mcps = false",
                "hooks = false",
                "",
                "[compat.claude]",
                "skills = false",
                "rules = false",
                "agents = false",
                "mcps = false",
                "hooks = false",
                "",
            ]
        )
        for resolved in resources.mcp_servers:
            server = resolved.server
            if resolved.name == CUA_DESKTOP_NAME:
                await stage_cua_desktop(sandbox, session.home)
            lines.extend([f"[mcp_servers.{_toml_key(resolved.name)}]"])
            if isinstance(server, StdioMcpServer):
                lines.append(f"command = {_toml(server.command.replace('{home}', session.home))}")
                args = [arg.replace("{home}", session.home) for arg in server.args]
                lines.append(f"args = [{', '.join(_toml(arg) for arg in args)}]")
                if server.cwd:
                    lines.append(f"cwd = {_toml(server.cwd.replace('{home}', session.home))}")
                if server.environment:
                    rendered = ", ".join(
                        f"{_toml_key(key)} = {_toml(value.replace('{home}', session.home))}"
                        for key, value in sorted(server.environment.items())
                    )
                    lines.append(f"env = {{ {rendered} }}")
            else:
                lines.append(f"url = {_toml(server.url)}")
            lines.append("")
        await sandbox.write_file(
            grok_home / "config.toml",
            ("\n".join(lines) + "\n").encode(),
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
        home = agent_path(session.home)
        requested = native_session_id or str(uuid.uuid4())
        binary = (
            home / ".grok-build-install/bin" / ("grok.exe" if is_windows(session.home) else "grok")
        )
        argv = [
            str(binary),
            "--prompt-file",
            prompt_path,
            "--cwd",
            session.home,
            "--model",
            session.model,
            "--output-format",
            "streaming-json",
            "--sandbox",
            "off",
            "--no-plan",
            "--always-approve",
            "--no-auto-update",
            "--disallowed-tools",
            ",".join(dict.fromkeys((*_HEADLESS_DISABLED_TOOLS, *self.settings.disabled_tools))),
            "--resume" if native_session_id is not None else "--session-id",
            requested,
        ]
        if self.settings.reasoning_effort != "default":
            argv.extend(["--reasoning-effort", self.settings.reasoning_effort])
        if isinstance(self.settings.max_turns, int):
            argv.extend(["--max-turns", str(self.settings.max_turns)])
        env = {
            **await npm_env(sandbox, session.home),
            "GROK_HOME": str(home / ".grok-ale"),
            "GROK_MANAGED_BY_NPM": "1",
            "GROK_SANDBOX": "off",
            "GROK_DISABLE_AUTOUPDATER": "1",
            "GROK_TELEMETRY_ENABLED": "0",
            "GROK_WORKSPACE_DATA_COLLECTION_DISABLED": "1",
            "ALE_GATEWAY_TOKEN": session.token,
        }
        for source in ("CURSOR", "CLAUDE"):
            for resource in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"):
                env[f"GROK_{source}_{resource}_ENABLED"] = "0"
        script = "unset XAI_API_KEY GROK_CLI_CHAT_PROXY_BASE_URL; " + shlex.join(argv)
        return CliCommand(script, env, requested)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)


def _toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_key(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _version(output: str) -> str | None:
    match = re.search(r"\d+(?:\.\d+)+", output)
    return match.group(0) if match else None
