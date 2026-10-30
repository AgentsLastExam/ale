"""ForgeCode configuration and invocation."""

from __future__ import annotations

import json
import re
import shlex
import uuid
from pathlib import PurePosixPath
from typing import Any, ClassVar

from pydantic import BaseModel

from ale.core.errors import AgentError, AgentUnsupportedError, ConfigError, HarnessLimitError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import OperatingSystem, StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses._npm import agent_path, npm_env, run_posix
from ale.run.harnesses._release import ReleaseAsset, ReleaseAssets
from ale.run.harnesses.base import LOG_ROOT, CliCommand, CliHarness, NativeResult
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import ForgecodeSettings

DEFAULT_CLI_VERSION = "2.14.0"
_RELEASE = "https://github.com/tailcallhq/forgecode/releases/download/v2.14.0"
#: ``FORGE_CONFIG``: configuration, credentials, the conversation database and Skills.
STATE = trajectory.STATE
#: Forge's own provider ID, URL template, key variable and wire format per Gateway dialect.
#: Forge rewrites the URL of any other Responses provider ID, so these IDs are fixed.
_PROVIDERS = {
    "anthropic": ("anthropic_compatible", "ANTHROPIC_URL", "messages", "Anthropic"),
    "openai-chat-completions": ("openai_compatible", "OPENAI_URL", "chat/completions", "OpenAI"),
    "openai-responses": (
        "openai_responses_compatible",
        "OPENAI_URL",
        "responses",
        "OpenAIResponses",
    ),
}
_REQUEST_LIMIT = re.compile(r"Maximum request \((\d+)\) per turn achieved")


class ForgecodeHarness(CliHarness):
    """Runs ForgeCode's default ``forge`` agent headless, with the prompt on stdin."""

    name = "forgecode"
    package = "github:tailcallhq/forgecode"
    binary = "forge"
    default_version = DEFAULT_CLI_VERSION
    release_assets: ClassVar[ReleaseAssets] = {
        (OperatingSystem.LINUX, "x86_64"): ReleaseAsset(
            f"{_RELEASE}/forge-x86_64-unknown-linux-musl",
            "64832f828bf08961322e302243f352ee7c27e88d76f972574a3628c7b70cb1be",
        ),
        (OperatingSystem.LINUX, "aarch64"): ReleaseAsset(
            f"{_RELEASE}/forge-aarch64-unknown-linux-musl",
            "f1a89a2fea3f88f08eedef72aaf7be4929f47fd3f10bf86eca720aa15433e7da",
        ),
        (OperatingSystem.WINDOWS, "x86_64"): ReleaseAsset(
            f"{_RELEASE}/forge-x86_64-pc-windows-msvc.exe",
            "375c22cbeee8f91f7021cac08643ea562df2822c1e5e2886c07641f2820926c4",
        ),
        (OperatingSystem.WINDOWS, "aarch64"): ReleaseAsset(
            f"{_RELEASE}/forge-aarch64-pc-windows-msvc.exe",
            "6201e8287686fb8b41572ca19edf3773158facf612f746475dd10a7f564d3176",
        ),
    }
    settings_type = ForgecodeSettings
    settings: ForgecodeSettings
    logs = (LOG_ROOT, trajectory.DUMPS, trajectory.MCP_CONFIG)

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "anthropic",
        settings: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if gateway_dialect not in _PROVIDERS:
            raise ConfigError(f"forgecode does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        super().__init__(cli_version=cli_version, settings=settings)

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        for resolved in resources.mcp_servers:
            if isinstance(resolved.server, StdioMcpServer) and resolved.server.cwd:
                raise AgentUnsupportedError(
                    f"forgecode stdio MCP servers have no working directory: {resolved.name}"
                )

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        home = agent_path(session.home)
        state = home / STATE
        skills_root = state / "skills"
        await run_posix(sandbox, session.home, "mkdir", "-p", skills_root, home / trajectory.DUMPS)
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
                    "command": server.command.replace("{home}", session.home),
                    "args": [arg.replace("{home}", session.home) for arg in server.args],
                }
                if server.environment:
                    native["env"] = {
                        key: value.replace("{home}", session.home)
                        for key, value in server.environment.items()
                    }
            else:
                # Remote MCP authentication is never copied from the Host.
                native = {"url": server.url, "oauth": False}
            servers[resolved.name] = native
        await _write(
            sandbox,
            home / trajectory.MCP_CONFIG,
            json.dumps({"mcpServers": servers}, ensure_ascii=False, sort_keys=True, indent=2),
        )

        provider, url_variable, endpoint, response_type = _PROVIDERS[self.gateway_dialect]
        lines = [
            "auto_install_vscode_extension = false",
            "auto_open_dump = false",
        ]
        if isinstance(self.settings.max_output_tokens, int):
            lines.append(f"max_tokens = {self.settings.max_output_tokens}")
        if isinstance(self.settings.max_requests_per_turn, int):
            lines.append(f"max_requests_per_turn = {self.settings.max_requests_per_turn}")
        lines += [
            "",
            "[updates]",
            'frequency = "never"',
            "auto_update = false",
            "",
            "[session]",
            f"provider_id = {_toml(provider)}",
            f"model_id = {_toml(session.model)}",
        ]
        if self.settings.reasoning_effort != "default":
            lines += ["", "[reasoning]", f"effort = {_toml(self.settings.reasoning_effort)}"]
        lines += [
            "",
            "[[providers]]",
            f"id = {_toml(provider)}",
            f"url = {_toml('{{' + url_variable + '}}/' + endpoint)}",
            f"api_key_var = {_toml(url_variable.replace('_URL', '_API_KEY'))}",
            f"response_type = {_toml(response_type)}",
            "",
            # Listing the model keeps forge from asking the Gateway for `/v1/models`.
            "[[providers.models]]",
            f"id = {_toml(session.model)}",
            f"name = {_toml(session.model)}",
            "tools_supported = true",
            "supports_parallel_tool_calls = true",
            'input_modalities = ["text", "image"]',
        ]
        if self.settings.reasoning_effort != "default":
            lines.append(
                f"supports_reasoning = {_toml_bool(self.settings.reasoning_effort != 'none')}"
            )
        await _write(sandbox, state / ".forge.toml", "\n".join(lines))
        # Forge reads keys only from this file; it holds the episode token, never a provider key.
        credentials = state / ".credentials.json"
        await _write(
            sandbox,
            credentials,
            json.dumps(
                [
                    {
                        "id": provider,
                        "auth_details": {"api_key": session.token},
                        "url_params": {url_variable: session.gateway_url + "/v1"},
                    }
                ]
            ),
        )
        await run_posix(sandbox, session.home, "chmod", "600", credentials)

    async def prepare_command(
        self,
        prompt_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand:
        home = agent_path(session.home)
        # `--conversation-id` continues that conversation, or starts one under this ID.
        requested = native_session_id or str(uuid.uuid4())
        dumps = shlex.quote(str(home / trajectory.DUMPS / requested))
        # Forge keeps conversations only in SQLite. After every invocation, a JSON export
        # of the whole conversation becomes the native evidence; `dump` writes into its
        # working directory, so it runs in a scratch directory.
        script = (
            "unset OPENAI_API_KEY OPENAI_URL ANTHROPIC_API_KEY ANTHROPIC_URL; "
            f"forge --conversation-id {shlex.quote(requested)} < {shlex.quote(prompt_path)}; "
            "status=$?; scratch=$(mktemp -d) && "
            # Forge reads a non-terminal stdin to its end first; this one would never end.
            f'(cd "$scratch" && forge conversation dump {shlex.quote(requested)} '
            "< /dev/null > /dev/null) && "
            f"mkdir -p {dumps} && "
            f'for dump in "$scratch"/*-dump.json; do mv -f "$dump" '
            f'{dumps}/"$(date -u +%Y%m%dT%H%M%S%NZ)".json; done; '
            'rm -rf "$scratch"; exit $status'
        )
        env = {
            **await npm_env(sandbox, session.home),
            "FORGE_CONFIG": str(home / STATE),
            # Updates are also off in `.forge.toml`; the variables also outrank a `.env`.
            "FORGE_UPDATES__FREQUENCY": "never",
            "FORGE_UPDATES__AUTO_UPDATE": "false",
            # Forge's own cloud services, reached over gRPC; never through the Gateway.
            "FORGE_SERVICES_URL": "http://127.0.0.1:9/",
            "FORGE_TRACKER": "false",
            "NO_COLOR": "1",
        }
        return CliCommand(script, env, requested)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)

    def classify_failure(self, detail: str) -> Exception:
        limit = _REQUEST_LIMIT.search(detail)
        if limit and isinstance(self.settings.max_requests_per_turn, int):
            return HarnessLimitError(
                "max_requests_per_turn",
                self.settings.max_requests_per_turn,
                int(limit.group(1)),
            )
        return AgentError(detail)


async def _write(sandbox: Sandbox, path: PurePosixPath, text: str) -> None:
    await sandbox.write_file(str(path), (text + "\n").encode(), identity=Identity.AGENT)


def _toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
