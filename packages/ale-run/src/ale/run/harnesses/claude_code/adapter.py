"""Claude Code configuration and invocation."""

from __future__ import annotations

import json
import shlex
import uuid
from pathlib import PurePosixPath

from ale.core.errors import AgentError, AgentRefusalError, ConfigError, HarnessLimitError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import StdioMcpServer, StreamableHttpMcpServer
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
from .settings import ClaudeCodeSettings

DEFAULT_CLI_VERSION = "2.1.227"
UPSTREAM_IDLE_TIMEOUT_SEC = 600
#: Error signatures worth naming, so a failure is attributed rather than lumped into
#: "the agent exited non-zero".
ERROR_PATTERNS: tuple[tuple[str, type[Exception]], ...] = (
    ("rate_limit", AgentError),
    ("I can't help", AgentRefusalError),
    ("I cannot help", AgentRefusalError),
)


class ClaudeCodeHarness(CliHarness):
    """Runs the Claude Code CLI inside the sandbox."""

    name = "claude-code"
    package = "@anthropic-ai/claude-code"
    binary = "claude"
    default_version = DEFAULT_CLI_VERSION
    settings_type = ClaudeCodeSettings
    settings: ClaudeCodeSettings
    logs = (LOG_ROOT, ".claude-config/projects")

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        config_dir = agent_path(session.home) / ".claude-config"
        # The CLI expects its configuration directory to exist, with these subdirectories
        # in place. It creates neither, and the failures are opaque when they are missing.
        await sandbox.exec(
            [
                "mkdir",
                "-p",
                *(
                    str(config_dir / name)
                    for name in ("debug", "projects", "shell-snapshots", "statsig", "todos")
                ),
                str(config_dir / "skills"),
            ],
            identity=Identity.AGENT,
        )
        await install_skills(sandbox, session, resources, config_dir / "skills")
        mcp_servers: dict[str, dict[str, object]] = {}
        for resolved in resources.mcp_servers:
            server = resolved.server
            if resolved.name == CUA_DESKTOP_NAME:
                await stage_cua_desktop(sandbox, session.home)
            if isinstance(server, StdioMcpServer):
                native: dict[str, object] = {
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
            elif isinstance(server, StreamableHttpMcpServer):
                native = {"type": "http", "url": server.url}
            else:  # pragma: no cover - the core discriminated union is exhaustive
                raise ConfigError(f"unsupported MCP transport for {resolved.name}")
            mcp_servers[resolved.name] = native
        await sandbox.write_file(
            config_dir / "mcp.json",
            json.dumps({"mcpServers": mcp_servers}, sort_keys=True).encode(),
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
        requested = native_session_id or str(uuid.uuid4())
        selector = "--resume" if native_session_id is not None else "--session-id"
        clear_auth = ""
        if session.authentication == "subscription":
            names = "ANTHROPIC_API_KEY "
            if not session.gateway_url:
                names += "ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL "
            clear_auth = (
                f"unset {names}CLAUDE_CODE_USE_BEDROCK "
                "CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY; "
            )
        config_path = agent_path(session.home) / ".claude-config/mcp.json"
        script = (
            f"{clear_auth}claude --verbose --output-format=stream-json {self._flags()} "
            f"--strict-mcp-config --mcp-config {shlex.quote(str(config_path))} "
            f"{selector} {shlex.quote(requested)} --print < {shlex.quote(prompt_path)}"
        )
        env = {**self._env(session), **await npm_env(sandbox, session.home)}
        return CliCommand(script, env, requested)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)

    def classify_failure(self, detail: str) -> Exception:
        # Gateway refusals are attributed by the episode protocol from the Gateway session.
        lowered = detail.lower()
        if isinstance(self.settings.max_turns, int) and any(
            marker in lowered for marker in ("max turns", "error_max_turns")
        ):
            return HarnessLimitError("max_turns", self.settings.max_turns, self.settings.max_turns)
        for needle, error_type in ERROR_PATTERNS:
            if needle.lower() in lowered:
                return error_type(detail)
        return AgentError(detail)

    def _env(self, session: HarnessSession) -> dict[str, str]:
        """Point the agent's own SDK at the gateway.

        No patching, no wrapper: the agent believes it is talking to a provider, and every
        call is metered and recorded regardless of what it does.

        The alias variables are the part that is easy to miss. Claude Code does not use
        one model — it reaches for a small one to summarise, name a session or run a
        subagent, and asks for it by alias. Against Anthropic those aliases resolve. Against
        anything else they are model names the endpoint has never heard of, so the run dies
        partway through on a request nobody made deliberately. Pointing every alias at the
        one model a run declared is what makes a third-party endpoint usable at all.
        Borrowed from Harbor, which learned it the same way.
        """
        config_dir = agent_path(session.home) / ".claude-config"
        env = {
            "ANTHROPIC_MODEL": session.model,
            # Telemetry has nowhere to go under a deny-all policy, and an agent retrying a
            # blocked request is an agent spending its budget on nothing.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # Without this the CLI refuses `bypassPermissions` outright in some
            # environments; it is the switch that says the isolation is the sandbox's job.
            "IS_SANDBOX": "1",
            # Two five-minute idle timers sit between the CLI and a quiet stream, and a
            # tool_use input the provider buffers until complete (about 24k tokens at
            # 80 tok/s) is quiet for longer than that. Bun's HTTP client drops the socket
            # after 300 s without a byte, which the CLI turns off only when it talks to
            # api.anthropic.com; the CLI's own stream watchdog aborts at 300 s and ends the
            # run. The first re-issues the request while the abandoned call keeps running
            # and being billed; the second surfaces as `API Error: Stream idle timeout`.
            # Match the gateway's upstream ceiling: nothing waits longer than the gateway.
            "BUN_CONFIG_HTTP_IDLE_TIMEOUT": str(UPSTREAM_IDLE_TIMEOUT_SEC),
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS": str(UPSTREAM_IDLE_TIMEOUT_SEC * 1000),
            # Somewhere writable that belongs to the agent. Left unset, the CLI writes to a
            # home directory it may not own.
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "ANTHROPIC_BASE_URL": session.gateway_url,
            "ANTHROPIC_AUTH_TOKEN": session.token,
        }
        if session.authentication == "subscription":
            env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
        if session.authentication == "api-key":
            env["ANTHROPIC_API_KEY"] = session.token
        for alias in (
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env[alias] = session.model
        return env

    def _flags(self) -> str:
        parts: list[str] = []
        if isinstance(self.settings.max_turns, int):
            parts.extend(("--max-turns", str(self.settings.max_turns)))
        if self.settings.permission_mode != "default":
            mode = (
                "default"
                if self.settings.permission_mode == "manual"
                else self.settings.permission_mode
            )
            parts.extend(("--permission-mode", shlex.quote(mode)))
        if self.settings.allowed_tools:
            parts.append("--allowedTools")
            parts.extend(shlex.quote(tool) for tool in self.settings.allowed_tools)
        if self.settings.disallowed_tools:
            parts.append("--disallowedTools")
            parts.extend(shlex.quote(tool) for tool in self.settings.disallowed_tools)
        if self.settings.append_system_prompt != "default":
            parts.extend(
                ("--append-system-prompt", shlex.quote(self.settings.append_system_prompt))
            )
        if self.settings.effort != "default":
            parts.extend(("--effort", self.settings.effort))
        return " ".join(parts)
