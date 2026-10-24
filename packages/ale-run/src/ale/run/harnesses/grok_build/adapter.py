"""Native CLI configuration and invocation."""

from __future__ import annotations

import json
import re
import shlex
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
from ale.run.agent_resources import continuation_fingerprint
from ale.run.harnesses._diagnostics import failure_detail
from ale.run.harnesses._npm import (
    agent_home,
    agent_path,
    bash_command,
    is_windows,
    npm_command,
    npm_env,
)
from ale.run.harnesses.base import CliCommand, CliHarness, NativeLogs, NativeResult
from ale.run.harnesses.evidence import evidence_scope
from ale.run.subscription import classify_subscription_error
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from .settings import GrokBuildSettings
from .trajectory import _native_error, _stream_final_message, _terminal_session_id, parse_trajectory

DEFAULT_CLI_VERSION = "0.2.112"
_HEADLESS_DISABLED_TOOLS = ("ask_user_question", "enter_plan_mode", "exit_plan_mode")
_API_BACKENDS = {
    "anthropic": "messages",
    "openai-chat-completions": "chat_completions",
    "openai-responses": "responses",
}


class GrokBuildHarness(CliHarness):
    name = "grok-build"

    supported_os = (OperatingSystem.LINUX, OperatingSystem.WINDOWS)

    resume_support = ResumeSupport.NATIVE

    __slots__ = ("cli_version", "gateway_dialect", "settings")

    native_logs = (NativeLogs(".grok-ale/sessions", ("**/updates.jsonl",)),)

    async def prepare_command(
        self,
        instruction_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand:
        home = agent_path(session.home)
        grok_home = home / ".grok-ale"
        if native_session_id is not None:
            state = await sandbox.exec(
                bash_command(
                    session.home,
                    f"find {shlex.quote(str(grok_home / 'sessions'))} -type d "
                    f"-name {shlex.quote(native_session_id)} -print -quit | grep -q .",
                    login=False,
                ),
                identity=Identity.AGENT,
            )
            if not state.ok:
                raise NativeContinuationError(
                    f"native Grok session {native_session_id!r} is absent"
                )
        requested = native_session_id or str(uuid.uuid4())
        binary = (
            home / ".grok-build-install/bin" / ("grok.exe" if is_windows(session.home) else "grok")
        )
        argv = [
            str(binary),
            "--prompt-file",
            instruction_path,
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
        script = "unset XAI_API_KEY GROK_CLI_CHAT_PROXY_BASE_URL; " + shlex.join(argv)
        env = {
            **await npm_env(sandbox, session.home),
            "GROK_HOME": str(grok_home),
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
        return CliCommand(tuple(bash_command(session.home, script, login=False)), env, requested)

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
        return NativeResult(
            _terminal_session_id(result.stdout), _stream_final_message(result.stdout)
        )

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "openai-responses",
        settings: GrokBuildSettings | dict[str, Any] | None = None,
    ) -> None:
        self.cli_version = cli_version
        if gateway_dialect not in _API_BACKENDS:
            raise ConfigError(f"grok-build does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        try:
            self.settings = (
                settings
                if isinstance(settings, GrokBuildSettings)
                else GrokBuildSettings.model_validate(settings or {})
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid grok-build settings: {exc}") from exc

    def version(self) -> str:
        return self.cli_version or DEFAULT_CLI_VERSION

    def integrity(self) -> str:
        return f"npm:@xai-official/grok@{self.version()}"

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        for resolved in resources.mcp_servers:
            if not isinstance(resolved.server, StdioMcpServer | StreamableHttpMcpServer):
                raise ConfigError(f"unsupported MCP transport for {resolved.name}")

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

    def _fingerprint(self, session: HarnessSession) -> str:
        return continuation_fingerprint(
            harness=self.name,
            model=session.model,
            settings={
                **self.settings.model_dump(mode="json"),
                "gateway_dialect": self.gateway_dialect,
            },
            resources_digest=session.resources_digest,
            authentication=session.authentication,
            profile_slot_id=session.profile_slot_id,
        )

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return evidence_scope(parse_trajectory(context), context)


def _toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_key(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _version(output: str) -> str | None:
    match = re.search(r"\d+(?:\.\d+)+", output)
    return match.group(0) if match else None
