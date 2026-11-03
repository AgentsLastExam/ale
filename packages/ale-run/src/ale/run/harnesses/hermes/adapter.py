"""Hermes Agent configuration and invocation."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel

from ale.core.errors import AgentError, ConfigError, HarnessLimitError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses._npm import (
    agent_home,
    agent_path,
    bash_command,
    is_windows,
    npm_env,
    reported_version,
)
from ale.run.harnesses.base import (
    LOG_ROOT,
    CliCommand,
    CliHarness,
    NativeResult,
    install_skills,
)
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import HermesSettings

DEFAULT_CLI_VERSION = "0.22.5"
#: The v0.22.5 tag of NousResearch/hermes-agent.
COMMIT = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
#: The official installer of that commit, for Linux and for Windows.
_INSTALLERS = f"https://raw.githubusercontent.com/NousResearch/hermes-agent/{COMMIT}/scripts"
#: ``HERMES_HOME``; the installer must place the checkout inside it, or the first run
#: installs the whole dependency environment again into the home it finds.
STATE = trajectory.STATE
#: Hermes' transport for each Gateway dialect, and the base URL suffix it expects.
_TRANSPORTS = {
    "anthropic": ("anthropic_messages", ""),
    "openai-chat-completions": ("chat_completions", "/v1"),
    "openai-responses": ("codex_responses", "/v1"),
}
_TURN_LIMIT = re.compile(r"max(?:imum)?[ _-]?(?:turns|iterations)", re.IGNORECASE)


class HermesHarness(CliHarness):
    """Runs one Hermes Agent query with JSON events, then exports its sessions."""

    name = "hermes"
    package = "github:NousResearch/hermes-agent"
    binary = "hermes"
    default_version = DEFAULT_CLI_VERSION
    settings_type = HermesSettings
    settings: HermesSettings
    logs = (LOG_ROOT, trajectory.EXPORTS, trajectory.CONFIG)

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "openai-chat-completions",
        settings: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if cli_version not in (None, DEFAULT_CLI_VERSION):
            raise ConfigError(f"hermes pins commit {COMMIT} of {DEFAULT_CLI_VERSION} only")
        if gateway_dialect not in _TRANSPORTS:
            raise ConfigError(f"hermes does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        super().__init__(cli_version=cli_version, settings=settings)

    def integrity(self) -> str:
        return f"git:NousResearch/hermes-agent@{COMMIT}"

    async def install(self, sandbox: Sandbox) -> str:
        """The official installer at the pinned commit; dependencies verify against uv.lock."""
        home = await agent_home(sandbox)
        state = f"{agent_path(home)}/{STATE}"
        launcher = shlex.quote(_launcher(home))
        installed = (
            f"{{ [ -x {launcher} ] && {launcher} --version 2>/dev/null | "
            f"grep -q 'local {COMMIT[:12]}'; }}"
        )
        if is_windows(home):
            # Git Bash fetches the PowerShell installer; it takes the same choices.
            run = (
                'script="$(mktemp -d)/install.ps1" && '
                f'curl -fsSL --retry 3 -o "$script" {_INSTALLERS}/install.ps1 && '
                'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$script")" '
                f"-Commit {COMMIT} -HermesHome {shlex.quote(state)} "
                f"-InstallDir {shlex.quote(state + '/hermes-agent')} "
                "-NonInteractive -SkipBrowser -SkipComputerUse"
            )
        else:
            run = (
                f"curl -fsSL --retry 3 {_INSTALLERS}/install.sh | bash -s -- --commit {COMMIT} "
                f"--hermes-home {shlex.quote(state)} --dir {shlex.quote(state + '/hermes-agent')} "
                "--non-interactive --skip-browser --skip-computer-use"
            )
        script = (
            f"set -e; export HERMES_HOME={shlex.quote(state)}; if ! {installed}; then {run}; fi"
        )
        if self.gateway_dialect == "anthropic":
            # The Anthropic SDK is an optional extra of the pinned lockfile.
            script += f"; {launcher} pm install --extra anthropic"
        result = await sandbox.exec(
            bash_command(home, script, login=False),
            env={**await npm_env(sandbox, home), "NO_COLOR": "1"},
            timeout_sec=2400,
            identity=Identity.AGENT,
        )
        if not result.ok:
            raise AgentError(
                f"could not install hermes {self.version()}: "
                f"{(result.stderr or result.stdout)[-800:]}"
            )
        probe = await sandbox.exec(
            bash_command(home, f"{launcher} --version", login=False),
            env={"HERMES_HOME": state},
            timeout_sec=120,
            identity=Identity.AGENT,
        )
        output = f"{probe.stdout}\n{probe.stderr}"
        version = reported_version(output, self.version()) if probe.ok else None
        if version is None or f"local {COMMIT[:12]}" not in output:
            raise AgentError(
                f"hermes --version does not name {COMMIT[:12]}: {output.strip()[:200]!r}"
            )
        return version

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        home = agent_path(session.home)
        skills_root = home / STATE / "skills"
        await sandbox.exec(
            ["mkdir", "-p", str(skills_root), str(home / trajectory.EXPORTS)],
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
            servers[resolved.name] = native

        transport, suffix = _TRANSPORTS[self.gateway_dialect]
        config: dict[str, Any] = {
            # The schema version the pinned installer seeds, so no migration runs.
            "_config_version": 51,
            "model": {"provider": "ale", "default": session.model},
            "providers": {
                "ale": {
                    "api": session.gateway_url + suffix,
                    "key_env": "ALE_GATEWAY_TOKEN",
                    "transport": transport,
                    "default_model": session.model,
                    "discover_models": False,
                }
            },
            # The run never needs Hermes' messaging gateway beside another profile's.
            "gateway": {"standalone": True},
            # No network use the Gateway does not carry: updates, the model catalog,
            # telemetry, logins adopted from other agents, and installs mid-run.
            "updates": {"check": False},
            "model_catalog": {"enabled": False},
            "telemetry": {"shared_metrics": {"enabled": False, "send": False}},
            "auth": {"adopt_external_logins": False},
            "security": {"allow_lazy_installs": False},
            "mcp_servers": servers,
        }
        await sandbox.write_file(
            str(home / trajectory.CONFIG),
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True).encode(),
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
        launcher = _launcher(session.home)
        argv = [launcher, "chat", "--query-file", prompt_path, "--format", "stream-json", "--yolo"]
        if native_session_id is not None:
            argv.extend(["--resume", native_session_id])
        if isinstance(self.settings.max_turns, int):
            argv.extend(["--max-turns", str(self.settings.max_turns)])
        if self.settings.reasoning_effort != "default":
            argv.extend(["--reasoning", self.settings.reasoning_effort])
        exports = shlex.quote(str(home / trajectory.EXPORTS))
        # Hermes runs each turn in a background gateway that the first chat starts and
        # that outlives it with that chat's environment, Gateway token included. A later
        # invocation, such as a continuing episode's, stops it so its own starts afresh.
        # Hermes keeps sessions only in SQLite. After each invocation every session of the
        # episode home, delegated ones included, is exported under the session it served,
        # which the invocation's own `result` event names.
        script = (
            f"{shlex.quote(launcher)} gateway stop < /dev/null > /dev/null 2>&1; "
            "events=$(mktemp); { "
            f"{shlex.join(argv)} < /dev/null; "
            'echo $? > "$events.status"; } | tee "$events"; '
            'status=$(cat "$events.status"); '
            "session=$(sed -n "
            '\'s/^{"type": "result", "session_id": "\\([^"]*\\)".*/\\1/p\' '
            '"$events" | tail -n 1); '
            'if [ -n "$session" ]; then '
            f'mkdir -p {exports}/"$session" && '
            f"{shlex.quote(launcher)} sessions export "
            f'{exports}/"$session"/"$(date -u +%Y%m%dT%H%M%S%NZ)".jsonl '
            "< /dev/null > /dev/null; fi; "
            'rm -f "$events" "$events.status"; exit "$status"'
        )
        env = {
            **await npm_env(sandbox, session.home),
            "HERMES_HOME": str(home / STATE),
            "ALE_GATEWAY_TOKEN": session.token,
            "NO_COLOR": "1",
        }
        # Hermes chooses a new session's ID itself, so only a resume asks for one.
        return CliCommand(script, env, native_session_id)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)

    def classify_failure(self, detail: str) -> Exception:
        if isinstance(self.settings.max_turns, int) and _TURN_LIMIT.search(detail):
            return HarnessLimitError("max_turns", self.settings.max_turns, self.settings.max_turns)
        return AgentError(detail)


def _launcher(home: str) -> str:
    """Where the installer publishes the ``hermes`` command for this system."""
    if is_windows(home):
        return f"{agent_path(home)}/{STATE}/bin/hermes.exe"
    return f"{agent_path(home)}/.local/bin/hermes"
