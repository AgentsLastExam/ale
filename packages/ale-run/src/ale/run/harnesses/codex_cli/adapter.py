"""Codex CLI configuration and invocation."""

from __future__ import annotations

import json
import shlex
from pathlib import PurePosixPath

from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import OperatingSystem, StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses._npm import agent_path, npm_env
from ale.run.harnesses.base import LOG_ROOT, CliCommand, CliHarness, NativeResult
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import CodexCliSettings

DEFAULT_CLI_VERSION = "0.146.0"


class CodexCliHarness(CliHarness):
    """Runs Codex CLI's non-interactive ``exec`` mode inside the sandbox."""

    name = "codex-cli"
    package = "@openai/codex"
    binary = "codex"
    default_version = DEFAULT_CLI_VERSION
    settings_type = CodexCliSettings
    settings: CodexCliSettings
    supported_os = (OperatingSystem.LINUX, OperatingSystem.WINDOWS, OperatingSystem.MACOS)
    logs = (LOG_ROOT, ".codex-ale/sessions")

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        codex_home = agent_path(session.home) / ".codex-ale"
        skills_root = codex_home / "skills"
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
            'model_provider = "ale"',
            f"model = {json.dumps(session.model)}",
            "check_for_update_on_startup = false",
            'approval_policy = "never"',
            'sandbox_mode = "danger-full-access"',
        ]
        if self.settings.reasoning_effort != "default":
            lines.append(f"model_reasoning_effort = {json.dumps(self.settings.reasoning_effort)}")
        lines.extend(
            [
                "",
                "[model_providers.ale]",
                'name = "ALE Gateway"',
                f"base_url = {json.dumps(session.gateway_url + '/v1')}",
                'env_key = "ALE_GATEWAY_TOKEN"',
                'wire_api = "responses"',
                "",
            ]
        )
        for resolved in resources.mcp_servers:
            server = resolved.server
            if resolved.name == CUA_DESKTOP_NAME:
                await stage_cua_desktop(sandbox, session.home)
            lines.append(f"[mcp_servers.{json.dumps(resolved.name)}]")
            if isinstance(server, StdioMcpServer):
                if resolved.name == CUA_DESKTOP_NAME and session.os is OperatingSystem.MACOS:
                    # Codex filters the MCP child environment. Forward only the two
                    # nonsecret locations supplied by the macOS Sandbox provider.
                    lines.append('env_vars = ["ALE_CUA_DRIVER", "ALE_CUA_DRIVER_SOCKET"]')
                lines.append(
                    f"command = {json.dumps(server.command.replace('{home}', session.home))}"
                )
                args = [arg.replace("{home}", session.home) for arg in server.args]
                lines.append(f"args = [{', '.join(json.dumps(arg) for arg in args)}]")
                if server.cwd:
                    lines.append(f"cwd = {json.dumps(server.cwd.replace('{home}', session.home))}")
                if server.environment:
                    rendered = ", ".join(
                        f"{json.dumps(key)} = {json.dumps(value.replace('{home}', session.home))}"
                        for key, value in sorted(server.environment.items())
                    )
                    lines.append(f"env = {{ {rendered} }}")
            else:
                lines.append(f"url = {json.dumps(server.url)}")
            lines.append("")
        await sandbox.write_file(
            codex_home / "config.toml",
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
        argv = [
            "codex",
            "exec",
            "--json",
            "--model",
            session.model,
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
        if self.settings.web_search:
            argv.append("--search")
        if native_session_id is not None:
            argv.extend(["resume", native_session_id])
        # `-` reads the prompt from stdin.
        argv.append("-")
        script = (
            "unset OPENAI_API_KEY OPENAI_BASE_URL CODEX_ACCESS_TOKEN; "
            f"{shlex.join(argv)} < {shlex.quote(prompt_path)}"
        )
        env = {
            **await npm_env(sandbox, session.home),
            "CODEX_HOME": str(agent_path(session.home) / ".codex-ale"),
            "NO_COLOR": "1",
            "ALE_GATEWAY_TOKEN": session.token,
        }
        # Codex chooses a new thread's ID itself, so only a resume asks for one.
        return CliCommand(script, env, native_session_id)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)
