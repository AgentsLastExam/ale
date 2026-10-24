"""Native CLI configuration and invocation."""

from __future__ import annotations

import json
import shlex
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
from ale.run.harnesses._npm import agent_path, bash_command, ensure_npm_cli, npm_env
from ale.run.harnesses.base import FIXED_PROMPT, CliCommand, CliHarness, NativeLogs, NativeResult
from ale.run.harnesses.evidence import evidence_scope
from ale.run.subscription import classify_subscription_error
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from .settings import CodexCliSettings
from .trajectory import _final_message, _native_error, _thread_id, parse_trajectory

DEFAULT_CLI_VERSION = "0.146.0"


class CodexCliHarness(CliHarness):
    name = "codex-cli"

    supported_os = (OperatingSystem.LINUX, OperatingSystem.WINDOWS)

    resume_support = ResumeSupport.NATIVE

    __slots__ = ("cli_version", "settings")

    native_logs = (NativeLogs(".codex-ale/sessions", ("**/*.jsonl",)),)

    async def prepare_command(
        self,
        instruction_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand:
        home = agent_path(session.home)
        if native_session_id is not None:
            state = await sandbox.exec(
                bash_command(
                    session.home,
                    f"find {shlex.quote(str(home / '.codex-ale/sessions'))} -type f "
                    f"-name {shlex.quote('*-' + native_session_id + '.jsonl')} "
                    "-print -quit | grep -q .",
                    login=False,
                ),
                identity=Identity.AGENT,
            )
            if not state.ok:
                raise NativeContinuationError(
                    f"native Codex thread {native_session_id!r} is absent"
                )
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
        argv.append(FIXED_PROMPT)
        script = "unset OPENAI_API_KEY OPENAI_BASE_URL CODEX_ACCESS_TOKEN; " + shlex.join(argv)
        env = {
            **await npm_env(sandbox, session.home),
            "CODEX_HOME": str(home / ".codex-ale"),
            "NO_COLOR": "1",
            "ALE_GATEWAY_TOKEN": session.token,
        }
        return CliCommand(
            tuple(bash_command(session.home, script, login=False)), env, native_session_id
        )

    def read_result(self, result: ExecResult, session: HarnessSession) -> NativeResult:
        native_error = _native_error(result.stdout)
        if result.exit_code != 0 or native_error:
            detail = failure_detail(
                self.name,
                result.exit_code,
                native=native_error or "",
                stderr=result.stderr,
                stdout=result.stdout,
                token=session.token,
            )
            if session.authentication == "subscription":
                raise classify_subscription_error(self.name, detail)
            raise AgentError(detail)
        return NativeResult(_thread_id(result.stdout), _final_message(result.stdout))

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        settings: CodexCliSettings | dict[str, Any] | None = None,
    ) -> None:
        self.cli_version = cli_version
        try:
            self.settings = (
                settings
                if isinstance(settings, CodexCliSettings)
                else CodexCliSettings.model_validate(settings or {})
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid codex-cli settings: {exc}") from exc

    def version(self) -> str:
        return self.cli_version or DEFAULT_CLI_VERSION

    def integrity(self) -> str:
        return f"npm:@openai/codex@{self.version()}"

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        for resolved in resources.mcp_servers:
            if not isinstance(resolved.server, StdioMcpServer | StreamableHttpMcpServer):
                raise ConfigError(f"unsupported MCP transport for {resolved.name}")

    async def install(self, sandbox: Sandbox) -> str:
        return await ensure_npm_cli(
            sandbox,
            package="@openai/codex",
            binary="codex",
            version=self.version(),
        )

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

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return evidence_scope(parse_trajectory(context), context)
