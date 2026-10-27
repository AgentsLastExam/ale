"""OpenHands CLI configuration and invocation."""

from __future__ import annotations

import json
import shlex
import uuid
from pathlib import PurePosixPath
from typing import Any, ClassVar

from pydantic import BaseModel

from ale.core.errors import ConfigError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import OperatingSystem, StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses._npm import agent_path, npm_env, run_posix
from ale.run.harnesses._release import ReleaseAsset, ReleaseAssets
from ale.run.harnesses.base import LOG_ROOT, CliCommand, CliHarness, NativeResult
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import OpenHandsCliSettings

DEFAULT_CLI_VERSION = "1.16.0"
_RELEASE = "https://github.com/OpenHands/OpenHands-CLI/releases/download/1.16.0"
#: ``OPENHANDS_PERSISTENCE_DIR``: MCP servers and conversations.
STATE = trajectory.STATE
#: The SDK clones OpenHands' public skills from a moving branch into this cache at every
#: start. An empty repository without a remote there yields none, deterministically.
_PUBLIC_SKILLS = ".openhands/cache/skills/public-skills"
#: The LiteLLM provider and base URL suffix for each Gateway dialect.
_ROUTES = {
    "anthropic": ("anthropic", ""),
    "openai-chat-completions": ("openai", "/v1"),
    "openai-responses": ("openai", "/v1"),
}
#: The pinned SDK sends a model over Responses exactly when its name contains one of these.
_RESPONSES_MODELS = ("gpt-5", "codex-mini-latest")


class OpenHandsCliHarness(CliHarness):
    """Runs the OpenHands CLI headless with JSON events and one exact conversation."""

    name = "openhands-cli"
    package = "github:OpenHands/OpenHands-CLI"
    binary = "openhands"
    default_version = DEFAULT_CLI_VERSION
    release_assets: ClassVar[ReleaseAssets] = {
        (OperatingSystem.LINUX, "x86_64"): ReleaseAsset(
            f"{_RELEASE}/openhands-linux-x86_64",
            "cb04ee2da91c698733d5201c55cbc08d81dccc9d64b666275abf68a4e0c590e3",
        ),
        (OperatingSystem.LINUX, "aarch64"): ReleaseAsset(
            f"{_RELEASE}/openhands-linux-arm64",
            "67c5cfb94e5fd4c4120eb0360b0f23337da31f64a70e8496bcf008e4caeea6af",
        ),
    }
    # Upstream ships no Windows build; it supports Windows only through WSL.
    supported_os = (OperatingSystem.LINUX,)
    settings_type = OpenHandsCliSettings
    settings: OpenHandsCliSettings
    logs = (LOG_ROOT, trajectory.CONVERSATIONS, trajectory.MCP_CONFIG)

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "anthropic",
        settings: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if gateway_dialect not in _ROUTES:
            raise ConfigError(f"openhands-cli does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        super().__init__(cli_version=cli_version, settings=settings)

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        self._route(session.model)
        home = agent_path(session.home)
        # User Skills are found below the home directory, not the persistence directory.
        skills_root = home / ".openhands/skills"
        public = shlex.quote(str(home / _PUBLIC_SKILLS))
        prepared = await sandbox.exec(
            [
                "sh",
                "-c",
                f"mkdir -p {shlex.quote(str(skills_root))} {shlex.quote(str(home / STATE))} "
                f"{public} && git -C {public} init -q && git -C {public} -c user.name=ale "
                "-c user.email=ale@localhost commit -q --allow-empty -m empty",
            ],
            identity=Identity.AGENT,
        )
        if not prepared.ok:
            raise ConfigError(f"could not prepare the OpenHands home: {prepared.stderr.strip()}")
        for skill in resources.skills:
            target = skills_root / skill.name
            await sandbox.upload_dir(str(skill.path), target, identity=Identity.AGENT)
            if skill.executable_files:
                executables = (target / path for path in skill.executable_files)
                await run_posix(sandbox, session.home, "chmod", "+x", *executables)

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
                native = {"transport": "http", "url": server.url}
            servers[resolved.name] = native
        await sandbox.write_file(
            str(home / trajectory.MCP_CONFIG),
            (json.dumps({"mcpServers": servers}, sort_keys=True, indent=2) + "\n").encode(),
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
        # `--resume` continues that conversation, or starts one under this ID.
        requested = native_session_id or uuid.uuid4().hex
        argv = [
            "openhands",
            "--headless",
            "--json",
            "--override-with-envs",
            "--resume",
            requested,
            "--file",
            prompt_path,
        ]
        provider, suffix = self._route(session.model)
        env = {
            **await npm_env(sandbox, session.home),
            "OPENHANDS_PERSISTENCE_DIR": str(home / STATE),
            # The CLI's agent, with these three as its model; nothing else is persisted.
            "LLM_MODEL": f"{provider}/{session.model}",
            "LLM_BASE_URL": session.gateway_url + suffix,
            "LLM_API_KEY": session.token,
            # LiteLLM's bundled model map, so importing it fetches nothing from GitHub.
            # Nothing in the run reads its prices.
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "NO_COLOR": "1",
        }
        return CliCommand(f"{shlex.join(argv)} < /dev/null", env, requested)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)

    def _route(self, model: str) -> tuple[str, str]:
        """The LiteLLM route for the Gateway dialect, if the pinned SDK sends it there."""
        responses = any(name in model.lower() for name in _RESPONSES_MODELS)
        if self.gateway_dialect != "anthropic" and responses != (
            self.gateway_dialect == "openai-responses"
        ):
            wire = "OpenAI Responses" if responses else "OpenAI Chat Completions"
            raise ConfigError(
                f"openhands-cli {self.version()} sends {model!r} over {wire}; "
                f"gateway.dialect {self.gateway_dialect!r} does not match"
            )
        return _ROUTES[self.gateway_dialect]
