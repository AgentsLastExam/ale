"""Native CLI configuration and invocation."""

from __future__ import annotations

import json
import shlex
import uuid
from typing import Any

from pydantic import ValidationError

from ale.core.errors import (
    AgentError,
    AgentRefusalError,
    ConfigError,
    HarnessLimitError,
    NativeContinuationError,
)
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
from ale.run.harnesses._npm import agent_home, agent_path, bash_command, npm_command, npm_env
from ale.run.harnesses.base import FIXED_PROMPT, CliCommand, CliHarness, NativeLogs, NativeResult
from ale.run.harnesses.evidence import evidence_scope
from ale.run.subscription import classify_subscription_error
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from .settings import ClaudeCodeSettings
from .trajectory import _final_message, _native_error, _native_session_id, parse_trajectory

DEFAULT_CLI_VERSION = "2.1.227"
CLI_PREFIX = ".local"
UPSTREAM_IDLE_TIMEOUT_SEC = 600
ERROR_PATTERNS: tuple[tuple[str, type[Exception]], ...] = (
    ("rate_limit", AgentError),
    ("I can't help", AgentRefusalError),
    ("I cannot help", AgentRefusalError),
)


class ClaudeCodeHarness(CliHarness):
    """Runs the Claude Code CLI inside the sandbox."""

    name = "claude-code"

    supported_os = (OperatingSystem.LINUX, OperatingSystem.WINDOWS)

    resume_support = ResumeSupport.NATIVE

    __slots__ = ("cli_version", "settings")

    native_logs = (NativeLogs(".claude-config/projects"),)

    async def prepare_command(
        self,
        instruction_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand:
        env = {**self._env(session), **await npm_env(sandbox, session.home)}
        if native_session_id is not None:
            state = await sandbox.exec(
                bash_command(
                    session.home,
                    'find "$CLAUDE_CONFIG_DIR/projects" -type f '
                    f"-name {shlex.quote(native_session_id + '.jsonl')} -print -quit | grep -q .",
                    login=False,
                ),
                env=env,
                identity=Identity.AGENT,
            )
            if not state.ok:
                raise NativeContinuationError(
                    f"native Claude session {native_session_id!r} is absent"
                )
        requested = native_session_id or str(uuid.uuid4())
        await sandbox.exec(
            bash_command(
                session.home,
                'mkdir -p "$CLAUDE_CONFIG_DIR"/debug "$CLAUDE_CONFIG_DIR"/projects '
                '"$CLAUDE_CONFIG_DIR"/shell-snapshots "$CLAUDE_CONFIG_DIR"/statsig '
                '"$CLAUDE_CONFIG_DIR"/todos',
                login=False,
            ),
            env=env,
            identity=Identity.AGENT,
        )
        config_path = agent_path(session.home) / ".claude-config/mcp.json"
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
        script = (
            f"{clear_auth}printf '%s' {shlex.quote(FIXED_PROMPT)} | "
            f"claude --verbose --output-format=stream-json {self._flags()} "
            f"--strict-mcp-config --mcp-config {shlex.quote(str(config_path))} "
            f"{selector} {shlex.quote(requested)} --print"
        )
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
            raise self._classify(detail, result.exit_code)
        return NativeResult(_native_session_id(result.stdout), _final_message(result.stdout))

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        settings: ClaudeCodeSettings | dict[str, Any] | None = None,
    ) -> None:
        self.cli_version = cli_version
        try:
            self.settings = (
                settings
                if isinstance(settings, ClaudeCodeSettings)
                else ClaudeCodeSettings.model_validate(settings or {})
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid claude-code settings: {exc}") from exc

    def version(self) -> str:
        return self.cli_version or DEFAULT_CLI_VERSION

    def integrity(self) -> str:
        """What pins the binary. Always a version, never "whatever the image had"."""
        return f"npm:{self.cli_version or DEFAULT_CLI_VERSION}"

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        return None

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        root = agent_path(session.home) / ".claude-config" / "skills"
        await sandbox.exec(["mkdir", "-p", str(root)], identity=Identity.AGENT)
        for skill in resources.skills:
            target = root / skill.name
            await sandbox.upload_dir(str(skill.path), target, identity=Identity.AGENT)
            if skill.executable_files:
                await sandbox.exec(
                    ["chmod", "+x", *(str(target / path) for path in skill.executable_files)],
                    identity=Identity.AGENT,
                )
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

        config_path = agent_path(session.home) / ".claude-config" / "mcp.json"
        await sandbox.write_file(
            config_path,
            json.dumps({"mcpServers": mcp_servers}, sort_keys=True).encode(),
            identity=Identity.AGENT,
        )

    async def install(self, sandbox: Sandbox) -> str:
        """Install the pinned CLI under the agent's ~/.local unless that version exists."""
        # Resolved rather than assumed: the home directory belongs to the image, which
        # declares the account but not where it lives.
        home = await agent_home(sandbox)
        prefix = f"{home}/{CLI_PREFIX}"
        env = await npm_env(sandbox, home)

        wanted = self.cli_version or DEFAULT_CLI_VERSION
        installed = await self._installed_version(sandbox, home, env)

        if installed != wanted:
            spec = f"@anthropic-ai/claude-code@{wanted}"
            # As the agent: it is the agent that runs this binary, and an install into
            # root's prefix is one the unprivileged account may not be able to execute.
            # `--force` so a same-version residue under the prefix is overwritten cleanly.
            result = await sandbox.exec(
                npm_command(home, "install", "-g", "--force", "--prefix", prefix, spec),
                env=env,
                timeout_sec=900,  # a cold install on an image with no cache is minutes
                identity=Identity.AGENT,
            )
            if not result.ok:
                detail = (result.stderr or result.stdout)[-500:]
                raise AgentError(f"could not install {spec}: {detail}")
            installed = await self._installed_version(sandbox, home, env)

        if installed != wanted:
            raise AgentError(
                f"the claude CLI reports {installed or 'nothing'} after installing "
                f"{wanted}; the run would not be measuring the agent it says it is"
            )
        return installed

    async def _installed_version(
        self, sandbox: Sandbox, home: str, env: dict[str, str]
    ) -> str | None:
        """What ``claude --version`` says, or ``None`` when there is no claude."""
        # Through a shell, so "there is no claude" is an exit code rather than an
        # exception: the guest service raises when it cannot find a binary at all, and
        # the missing case is the ordinary one here, not an error.
        probe = await sandbox.exec(
            bash_command(
                home, "command -v claude >/dev/null 2>&1 && claude --version", login=False
            ),
            env=env,
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        if not probe.ok or not probe.stdout.strip():
            return None
        # "2.1.220 (Claude Code)" — the first field is the version.
        return probe.stdout.strip().split()[0]

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
        }
        if session.authentication == "subscription":
            env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
        env.update(
            {
                "ANTHROPIC_BASE_URL": session.gateway_url,
                "ANTHROPIC_AUTH_TOKEN": session.token,
            }
        )
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
        if isinstance(self.settings.max_budget_usd, float):
            parts.extend(("--max-budget-usd", str(self.settings.max_budget_usd)))
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

    def _classify(self, stderr: str, exit_code: int) -> Exception:
        # Gateway refusals are attributed by the Environment from the Gateway session.
        lowered = stderr.lower()
        if isinstance(self.settings.max_turns, int) and any(
            marker in lowered for marker in ("max turns", "error_max_turns")
        ):
            return HarnessLimitError(
                "max_turns",
                self.settings.max_turns,
                self.settings.max_turns,
            )
        if isinstance(self.settings.max_budget_usd, float) and any(
            marker in lowered for marker in ("max budget", "error_max_budget_usd")
        ):
            return HarnessLimitError(
                "max_budget_usd",
                self.settings.max_budget_usd,
                self.settings.max_budget_usd,
            )
        for needle, error_type in ERROR_PATTERNS:
            if needle.lower() in stderr.lower():
                return error_type(stderr)
        return AgentError(stderr)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return evidence_scope(parse_trajectory(context), context)
