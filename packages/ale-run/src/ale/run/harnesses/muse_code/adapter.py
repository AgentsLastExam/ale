"""Muse Code configuration and invocation."""

from __future__ import annotations

import json
import re
import shlex
import uuid
from pathlib import PurePosixPath
from typing import Any, ClassVar

from ale.core.errors import HarnessLimitError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import OperatingSystem, StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses._npm import agent_path, is_windows, npm_env
from ale.run.harnesses._release import ReleaseAsset, ReleaseAssets
from ale.run.harnesses.base import LOG_ROOT, CliCommand, CliHarness, NativeResult
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import MuseCodeSettings

DEFAULT_CLI_VERSION = "1.4.13-R6996.1"
_RELEASE = (
    "https://lookaside.facebook.com/lookaside/muse/download/"
    f"?channel=muse&version={DEFAULT_CLI_VERSION}&file="
)
#: Variables that would add another agent's personal Skills and rules to the run.
_FOREIGN = ("CODEX_HOME", "CLAUDE_CONFIG_DIR")
_STEP_LIMIT = re.compile(r"did not reach a terminal state within \d+ step")


class MuseCodeHarness(CliHarness):
    """Runs Meta's Muse Code headless (``muse exec --json``) inside the sandbox."""

    name = "muse-code"
    package = "lookaside.facebook.com/lookaside/muse"
    binary = "muse"
    default_version = DEFAULT_CLI_VERSION
    # Meta's release manifest for this version; upstream ships no Windows ARM build.
    release_assets: ClassVar[ReleaseAssets] = {
        (OperatingSystem.LINUX, "x86_64"): ReleaseAsset(
            _RELEASE + "muse-x86-linux",
            "51e97f8de0aa175080e6576cbc055510a95dfe0ededf175c7f6b34fc8e3bf524",
        ),
        (OperatingSystem.LINUX, "aarch64"): ReleaseAsset(
            _RELEASE + "muse-aarch64-linux",
            "c7f278b5f12862f5721c73433976256aa2a8abd433a12da0e484d30b08c024ff",
        ),
        (OperatingSystem.WINDOWS, "x86_64"): ReleaseAsset(
            _RELEASE + "muse-x86-windows.exe",
            "239c993cedc3768a99103a90b2f7e3d560375036104980e30227ffebf9de0303",
        ),
    }
    settings_type = MuseCodeSettings
    settings: MuseCodeSettings
    logs = (LOG_ROOT, trajectory.SESSIONS)

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        config = agent_path(session.home) / ".config/muse"
        skills_root = config / "skills"
        await sandbox.exec(["mkdir", "-p", str(skills_root)], identity=Identity.AGENT)
        for skill in resources.skills:
            target = skills_root / skill.name
            await sandbox.upload_dir(str(skill.path), target, identity=Identity.AGENT)
            if skill.executable_files:
                await sandbox.exec(
                    ["chmod", "+x", *(str(target / path) for path in skill.executable_files)],
                    identity=Identity.AGENT,
                )
        servers: dict[str, dict[str, Any]] = {}
        for resolved in resources.mcp_servers:
            server = resolved.server
            if resolved.name == CUA_DESKTOP_NAME:
                await stage_cua_desktop(sandbox, session.home)
            if isinstance(server, StdioMcpServer):
                native: dict[str, Any] = {
                    "transport": "stdio",
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
                native = {"transport": "streamable_http", "url": server.url}
            servers[resolved.name] = {**native, "enabled": True}
        settings = {
            "schema_version": 1,
            # Muse sends its bearer only to Meta's own endpoint unless the settings name
            # another one and pin bearer authentication; a --base-url flag cannot.
            "endpoint_transport": {"base_url": session.gateway_url + "/v1", "auth": "bearer"},
            "telemetry": {"enabled": False},
            "mcp_servers": servers,
        }
        await sandbox.write_file(
            config / "settings.json",
            (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode(),
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
        argv = [
            str(home / ".local/bin" / ("muse.exe" if is_windows(session.home) else "muse")),
            "exec",
            "--json",
            "--prompt-file",
            prompt_path,
            "--model",
            session.model,
            # Approval and Muse's own sandbox off: the ALE Sandbox is the boundary.
            "--yolo",
            "--no-foreign-personal-context",
            # A new ID starts the session; the same ID continues it.
            "--session-id",
            requested,
        ]
        if self.settings.reasoning_effort != "default":
            argv.extend(["--reasoning-effort", self.settings.reasoning_effort])
        if isinstance(self.settings.max_model_steps, int):
            argv.extend(["--max-model-steps", str(self.settings.max_model_steps)])
        if not self.settings.reminders:
            argv.append("--disable-reminders")
        if not self.settings.web_tools:
            argv.append("--disable-web-tools")
        env = {
            **await npm_env(sandbox, session.home),
            # Muse keeps settings under XDG_CONFIG_HOME and sessions under XDG_DATA_HOME;
            # naming the defaults fixes both inside the episode home on every system.
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "META_API_KEY": session.token,
            "NO_COLOR": "1",
        }
        script = f"unset {' '.join(_FOREIGN)}; {shlex.join(argv)}"
        return CliCommand(script, env, requested)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)

    def classify_failure(self, detail: str) -> Exception:
        steps = self.settings.max_model_steps
        if isinstance(steps, int) and _STEP_LIMIT.search(detail):
            return HarnessLimitError("max_model_steps", steps, steps)
        return super().classify_failure(detail)
