"""OpenClaw CLI configuration and invocation."""

from __future__ import annotations

import json
import re
import shlex
import uuid
from pathlib import PurePosixPath
from typing import Any

from ale.core.errors import AgentError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses._npm import agent_path, npm_env
from ale.run.harnesses.base import LOG_ROOT, CliCommand, CliHarness, NativeResult
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import OpenClawCliSettings

DEFAULT_CLI_VERSION = "2026.7.1"


class OpenClawCliHarness(CliHarness):
    """Runs OpenClaw's local ``agent`` command inside the sandbox, always as agent ``main``."""

    name = "openclaw-cli"
    package = "openclaw"
    binary = "openclaw"
    default_version = DEFAULT_CLI_VERSION
    settings_type = OpenClawCliSettings
    settings: OpenClawCliSettings
    logs = (LOG_ROOT, trajectory.SESSIONS)

    async def install(self, sandbox: Sandbox) -> str:
        node = await sandbox.exec(["node", "--version"], timeout_sec=30, identity=Identity.AGENT)
        if not node.ok or not _supported_node(node.stdout):
            raise AgentError(
                "OpenClaw 2026.7.1 requires Node >=22.22.3; rebuild the ALE base image"
            )
        return await super().install(sandbox)

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        state = agent_path(session.home) / ".openclaw-ale"
        workspace = state / "workspace"
        skills_root = workspace / "skills"
        await sandbox.exec(
            ["mkdir", "-p", str(skills_root), str(agent_path(session.home) / trajectory.SESSIONS)],
            identity=Identity.AGENT,
        )
        for skill in resources.skills:
            target = skills_root / skill.name
            await sandbox.upload_dir(str(skill.path), target, identity=Identity.AGENT)
            if skill.executable_files:
                await sandbox.exec(
                    ["chmod", "+x", *(str(target / path) for path in skill.executable_files)],
                    identity=Identity.AGENT,
                )

        model_ref = f"{self.settings.provider}/{session.model}"
        model_entry: dict[str, Any] = {"agentRuntime": {"id": "openclaw"}}
        if self.settings.model_params:
            model_entry["params"] = self.settings.model_params
        provider_model: dict[str, Any] = {
            "id": session.model,
            "name": session.model,
            "input": ["text", "image"],
            "agentRuntime": {"id": "openclaw"},
        }
        if self.settings.thinking != "default":
            provider_model["reasoning"] = self.settings.thinking != "off"
        tools: dict[str, Any] = {
            "profile": self.settings.tool_profile,
            "deny": list(self.settings.tools_deny),
        }
        if self.settings.tools_allow:
            tools["allow"] = list(self.settings.tools_allow)
        config: dict[str, Any] = {
            "agents": {
                "defaults": {
                    "workspace": str(workspace),
                    "skipBootstrap": True,
                    "model": {"primary": model_ref},
                    "models": {model_ref: model_entry},
                }
            },
            "models": {
                # OpenClaw would otherwise fetch price data; ALE reports tokens only.
                "pricing": {"enabled": False},
                "providers": {
                    self.settings.provider: {
                        "baseUrl": session.gateway_url + "/v1",
                        "apiKey": "${ALE_GATEWAY_TOKEN}",
                        "api": "openai-responses",
                        "models": [provider_model],
                    }
                },
            },
            "skills": {"allowBundled": []},
            "tools": tools,
            "gateway": {"mode": "local", "bind": "loopback"},
            "mcp": {"servers": {}},
        }
        if isinstance(self.settings.timeout_seconds, int):
            config["agents"]["defaults"]["timeoutSeconds"] = self.settings.timeout_seconds
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
                native = {"url": server.url, "transport": "streamable-http"}
            config["mcp"]["servers"][resolved.name] = native
        await sandbox.write_file(
            state / "openclaw.json",
            (json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
            identity=Identity.AGENT,
        )
        await sandbox.write_file(
            state / "exec-approvals.json",
            (
                json.dumps(
                    {
                        "version": 1,
                        "defaults": {
                            "security": "full",
                            "ask": "off",
                            "askFallback": "full",
                        },
                        "socket": {},
                        "agents": {},
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode(),
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
        state = agent_path(session.home) / ".openclaw-ale"
        requested = native_session_id or str(uuid.uuid4())
        argv = [
            "openclaw",
            "agent",
            "--local",
            "--agent",
            "main",
            "--session-id",
            requested,
            "--message-file",
            prompt_path,
            "--model",
            f"{self.settings.provider}/{session.model}",
            "--json",
        ]
        if self.settings.thinking != "default":
            argv.extend(["--thinking", self.settings.thinking])
        if self.settings.timeout_seconds == "unlimited":
            argv.extend(["--timeout", "0"])
        elif isinstance(self.settings.timeout_seconds, int):
            argv.extend(["--timeout", str(self.settings.timeout_seconds)])
        env = {
            **await npm_env(sandbox, session.home),
            "OPENCLAW_HOME": str(state),
            "OPENCLAW_STATE_DIR": str(state),
            "OPENCLAW_CONFIG_PATH": str(state / "openclaw.json"),
            "OPENCLAW_WORKSPACE_DIR": str(state / "workspace"),
            "ALE_GATEWAY_TOKEN": session.token,
            "NO_COLOR": "1",
            # Run the CLI in this process. Its respawning launcher would add a second
            # Node process and report any signal death of the CLI, such as an OOM kill,
            # as a bare exit 1.
            "OPENCLAW_NO_RESPAWN": "1",
        }
        return CliCommand(shlex.join(argv), env, requested)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)


def _supported_node(output: str) -> bool:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", output)
    if not match:
        return False
    version = tuple(int(part) for part in match.groups())
    return (
        version >= (25, 9, 0)
        or (24, 15, 0) <= version < (25, 0, 0)
        or (22, 22, 3) <= version < (23, 0, 0)
    )
