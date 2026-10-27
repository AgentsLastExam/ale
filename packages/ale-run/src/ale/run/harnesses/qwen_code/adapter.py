"""Qwen Code configuration and invocation."""

from __future__ import annotations

import json
import shlex
import uuid
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel

from ale.core.errors import ConfigError, HarnessLimitError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses._npm import agent_path, npm_env, run_posix
from ale.run.harnesses.base import LOG_ROOT, CliCommand, CliHarness, NativeResult
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import QwenCodeSettings

DEFAULT_CLI_VERSION = "0.26.1"
#: Each Gateway dialect's native protocol, the provider group that declares its model, the
#: URL suffix its client expects, and the OpenAI wire API.
_PROVIDERS: dict[str, tuple[str, str, str, str | None]] = {
    "openai-chat-completions": ("openai", "openai", "/v1", None),
    "openai-responses": ("openai-responses", "openai", "/v1", "responses"),
    "anthropic": ("anthropic", "anthropic", "", None),
}
#: Variables that would route the model elsewhere or replace the configured model.
_OVERRIDES = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
)


class QwenCodeHarness(CliHarness):
    """Runs Qwen Code's headless mode, reading the prompt from stdin, inside the sandbox."""

    name = "qwen-code"
    package = "@qwen-code/qwen-code"
    binary = "qwen"
    default_version = DEFAULT_CLI_VERSION
    settings_type = QwenCodeSettings
    settings: QwenCodeSettings
    logs = (LOG_ROOT, trajectory.PROJECTS)

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "openai-chat-completions",
        settings: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if gateway_dialect not in _PROVIDERS:
            raise ConfigError(f"qwen-code does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        super().__init__(cli_version=cli_version, settings=settings)

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        qwen_home = agent_path(session.home) / trajectory.HOME
        skills_root = qwen_home / "skills"
        await run_posix(sandbox, session.home, "mkdir", "-p", skills_root)
        for skill in resources.skills:
            target = skills_root / skill.name
            await sandbox.upload_dir(str(skill.path), target, identity=Identity.AGENT)
            if skill.executable_files:
                executables = (target / path for path in skill.executable_files)
                await run_posix(sandbox, session.home, "chmod", "+x", *executables)

        protocol, provider, suffix, wire_api = _PROVIDERS[self.gateway_dialect]
        route: dict[str, Any] = {
            "id": session.model,
            "envKey": "ALE_GATEWAY_TOKEN",
            "baseUrl": session.gateway_url + suffix,
        }
        if wire_api is not None:
            route["wireApi"] = wire_api
        generation: dict[str, Any] = {}
        if isinstance(self.settings.context_window_size, int):
            generation["contextWindowSize"] = self.settings.context_window_size
        if self.settings.modalities != "default":
            generation["modalities"] = {name: True for name in self.settings.modalities}
        if generation:
            route["generationConfig"] = generation
        model: dict[str, Any] = {"name": session.model}
        if self.settings.max_session_turns != "default":
            # Qwen reads -1 as no turn ceiling.
            model["maxSessionTurns"] = (
                -1
                if self.settings.max_session_turns == "unlimited"
                else self.settings.max_session_turns
            )
        if self.settings.reasoning_effort != "default":
            model["reasoningEffort"] = self.settings.reasoning_effort
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
                native = {"httpUrl": server.url}
            servers[resolved.name] = native
        settings = {
            "security": {"auth": {"selectedType": protocol, "enforcedType": protocol}},
            "model": model,
            "modelProviders": {provider: [route]},
            "mcpServers": servers,
            "privacy": {"usageStatisticsEnabled": False},
            "telemetry": {"enabled": False},
            "general": {"enableAutoUpdate": False},
        }
        # System settings override a workspace's own .qwen/settings.json; the system
        # defaults file replaces any machine-wide one.
        await sandbox.write_file(
            qwen_home / "system-settings.json",
            (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode(),
            identity=Identity.AGENT,
        )
        await sandbox.write_file(
            qwen_home / "system-defaults.json", b"{}\n", identity=Identity.AGENT
        )

    async def prepare_command(
        self,
        prompt_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand:
        qwen_home = agent_path(session.home) / trajectory.HOME
        requested = native_session_id or str(uuid.uuid4())
        argv = ["qwen", "--output-format", "stream-json", "--approval-mode", "yolo"]
        if native_session_id is not None:
            argv.append(f"--resume={native_session_id}")
        else:
            argv.extend(["--session-id", requested])
        # Headless mode reads the prompt from stdin, which is not a terminal here.
        script = f"unset {' '.join(_OVERRIDES)}; {shlex.join(argv)} < {shlex.quote(prompt_path)}"
        env = {
            **await npm_env(sandbox, session.home),
            "QWEN_HOME": str(qwen_home),
            "QWEN_RUNTIME_DIR": str(qwen_home),
            "QWEN_CODE_SYSTEM_SETTINGS_PATH": str(qwen_home / "system-settings.json"),
            "QWEN_CODE_SYSTEM_DEFAULTS_PATH": str(qwen_home / "system-defaults.json"),
            "QWEN_CODE_MODELS_DEV_REFRESH": "off",
            "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
            # Qwen otherwise relaunches itself with a heap of half the host's memory, which
            # a Sandbox's memory limit does not bound; Node's own default respects it.
            "QWEN_CODE_NO_RELAUNCH": "1",
            "NO_COLOR": "1",
            "ALE_GATEWAY_TOKEN": session.token,
        }
        return CliCommand(script, env, requested)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)

    def classify_failure(self, detail: str) -> Exception:
        turns = self.settings.max_session_turns
        if isinstance(turns, int) and "Reached max session turns" in detail:
            return HarnessLimitError("max_session_turns", turns, turns)
        return super().classify_failure(detail)
