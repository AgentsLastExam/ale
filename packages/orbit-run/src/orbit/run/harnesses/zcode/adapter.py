"""ZCode configuration, installation and invocation."""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel

from orbit.core.errors import ConfigError
from orbit.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from orbit.core.sandbox import Identity, Sandbox
from orbit.core.taskspec import StdioMcpServer
from orbit.core.trajectory import AtifTrajectory
from orbit.run.harnesses._agent import (
    agent_env,
    agent_home,
    agent_path,
    agent_python,
    is_windows,
)
from orbit.run.harnesses._install import (
    PROGRAMS,
    Archive,
    installed,
    node_path,
    node_runtime,
    sandbox_platform,
    unpack,
)
from orbit.run.harnesses.base import (
    LOG_ROOT,
    PROMPT_ARGUMENT_WINDOWS_ENV,
    CliCommand,
    CliHarness,
    NativeResult,
    http_headers,
    install_skills,
    prompt_argument,
)
from orbit.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import InputFormat, ZCodeSettings

#: The CLI version inside ZCode's desktop release 3.16.3, its only official distribution.
DEFAULT_CLI_VERSION = "0.17.0"
RELEASE = "3.16.3"
#: The official Linux installer, pinned by the sha512 its release's `latest.yml` publishes.
#: The CLI is plain JavaScript, byte-identical in every platform's installer, so this one
#: payload serves every system.
INSTALLER_URL = (
    f"https://cdn-zcode.z.ai/zcode/electron/releases/{RELEASE}/linux-x64/"
    f"ZCode-{RELEASE}-linux-x64.deb"
)
INSTALLER_SHA512 = (
    "5af39bd81e0d9d174d14800ab8c27e4b4bc24feb907d57e98853cbb2b686f0a4"
    "021d35d046cbc2e7c3a44f3efdb277db9a60a401eb57522fd1277f5b286f93f2"
)
#: The files Orbit runs, by their path in the installer's data archive.
FILES = {
    "./opt/ZCode/resources/glm/zcode.cjs": (
        "zcode.cjs",
        "97490d0f06a7a225d54d47817acf4b4859cdb268b59f2490caaeda982bef063a",
    ),
    "./opt/ZCode/resources/config/provider/zcode-builtin.json": (
        "zcode-builtin.json",
        "361b778f89496a37540d23a9d309efe6cb8fc97d557119cefbffc67df2f93998",
    ),
}
#: The installer as one archive, of which only those files are unpacked, each checked.
INSTALLER = Archive(
    f"sha512:{INSTALLER_SHA512}",
    "members",
    url=INSTALLER_URL,
    members=tuple((member, name, digest) for member, (name, digest) in FILES.items()),
)
#: ZCode's API type for each Gateway dialect, and the URL suffix its client expects.
_APIS = {
    "openai-chat-completions": ("openai-chat-completions", "/v1"),
    "openai-responses": ("openai-responses", "/v1"),
    "anthropic": ("anthropic-messages", ""),
}
#: Variables that would move ZCode's data or session store, or switch on OTLP export.
_OVERRIDES = (
    "ZCODE_STORAGE_DIR",
    "ZCODE_SESSION_DB_PATH",
    "ZCODE_DATA_BASE_DIR",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
)


class ZCodeHarness(CliHarness):
    """Runs the ZCode CLI headless on Orbit's Node.js, from ZCode's desktop release."""

    name = "zcode"
    package = f"cdn-zcode.z.ai/zcode/electron/releases/{RELEASE}"
    binary = "zcode"
    mcp_sse = False  # no SSE transport is documented for ZCode's configuration
    default_version = DEFAULT_CLI_VERSION
    node = "24.15.0"
    settings_type = ZCodeSettings
    settings: ZCodeSettings
    logs = (LOG_ROOT, trajectory.DB_DIR, trajectory.ARTIFACTS, trajectory.AGENTS)
    credentials = (f"{trajectory.PRIVATE}/provider.json",)

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "anthropic",
        settings: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if cli_version not in (None, DEFAULT_CLI_VERSION):
            # The digests pin one build; another version would run unverified bytes.
            raise ConfigError(f"zcode pins {DEFAULT_CLI_VERSION} only, not {cli_version}")
        if gateway_dialect not in _APIS:
            raise ConfigError(f"zcode does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        super().__init__(cli_version=cli_version, settings=settings)

    def integrity(self) -> str:
        """``sha256:`` of the files Orbit runs, named with their own digests."""
        lines = sorted(f"{name} {digest}" for name, digest in FILES.values())
        files = "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()
        return f"{files} node@{self.node}"

    async def place(
        self, sandbox: Sandbox, directory: str, *, identity: Identity, fetch: bool
    ) -> list[str]:
        home = await agent_home(sandbox)
        platform = await sandbox_platform(sandbox, home)
        program = f"{self.binary}/{self.version()}"
        argv = [node_path(directory, self.node, platform), f"{directory}/{program}/zcode.cjs"]
        if await installed(sandbox, home, argv, self.version(), identity=identity):
            return argv
        node = node_runtime(self.node, platform)
        await unpack(
            sandbox,
            [node.moved(f"node/{self.node}"), INSTALLER.moved(program)],
            directory,
            identity=identity,
            fetch=fetch,
            executables=[f"node/{self.node}/{node.members[0][1]}"],
        )
        return argv

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        home = agent_path(session.home)
        skills_root = home / ".zcode/skills"
        private = home / trajectory.PRIVATE
        await sandbox.exec(
            ["mkdir", "-p", str(skills_root), str(home / ".zcode/cli"), str(private)],
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
                    "type": "stdio",
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
                native = {"type": "http", "url": server.url, **http_headers(resolved)}
            servers[resolved.name] = {**native, "enabled": True}
        config: dict[str, Any] = {}
        if servers:
            config = {"features": {"mcp": True}, "mcp": {"servers": servers}}
        await sandbox.write_file(
            home / ".zcode/cli/config.json",
            (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode(),
            identity=Identity.AGENT,
        )

        api, suffix = _APIS[self.gateway_dialect]
        provider = {
            "providerId": "orbit",
            "providerName": "Orbit Gateway",
            "config": {
                "group": "standard-personal",
                # ZCode reads only a literal key here; it is the episode token, readable
                # by the agent account alone.
                "access": {"type": "api-key", "apiKey": session.token},
                "api": {"type": api, "baseUrl": session.gateway_url + suffix},
                "personalModelIds": [session.model],
            },
        }
        # WebSearch runs the provider's web_search server tool, which ZCode encodes only for
        # Anthropic Messages and offers only to models whose rule says so.
        properties: dict[str, Any] = {
            "supportsNativeWebSearch": session.provider_web_tools and api == "anthropic-messages"
        }
        if isinstance(self.settings.input_format, InputFormat):
            properties["inputFormat"] = self.settings.input_format.native
        # A partial rule, which ZCode lays over what its catalog says of the model.
        rule = {
            "providerId": "orbit",
            "modelId": session.model,
            "config": {"properties": properties},
        }
        personal = {
            "schemaVersion": 1,
            "config": {
                "providerOrder": ["orbit"],
                "providerConfigRules": {"providerRules": [provider]},
                "modelConfigRules": {
                    "providerModelRules": [rule],
                    "manualProviderModelRules": [],
                },
                "defaultModelSelection": {"providerId": "orbit", "modelId": session.model},
            },
        }
        await sandbox.write_file(
            private / "provider.json",
            (json.dumps(personal, ensure_ascii=False, indent=2) + "\n").encode(),
            identity=Identity.AGENT,
        )
        await sandbox.exec(
            ["chmod", "600", str(private / "provider.json")], identity=Identity.AGENT
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
        argv = [
            str(home / ".local/bin/zcode"),
            "--mode",
            "yolo",
            "--output-format",
            "stream-json",
            "--cwd",
            session.start_dir,
        ]
        if native_session_id is not None:
            argv.extend(["--resume", native_session_id])
        if self.settings.disallowed_tools:
            argv.extend(["--disallowed-tools", ",".join(self.settings.disallowed_tools)])
        if self.settings.workflow_mode != "default":
            argv.extend(["--workflow-mode", self.settings.workflow_mode])
        if self.settings.toolsearch_mode != "default":
            argv.extend(["--toolsearch-mode", self.settings.toolsearch_mode])
        # Headless ZCode takes the prompt only as an argument.
        read_prompt, prompt = prompt_argument(prompt_path)
        script = f"unset {' '.join(_OVERRIDES)}; {read_prompt}{shlex.join(argv)} --prompt={prompt}"
        env = {
            **await agent_env(sandbox, session.home),
            "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": str(
                home / PROGRAMS / self.binary / self.version() / "zcode-builtin.json"
            ),
            "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": str(home / trajectory.PRIVATE / "provider.json"),
            "ZCODE_MODEL_TELEMETRY_ENABLED": "false",
            "NO_COLOR": "1",
        }
        if is_windows(session.home):
            env.update(PROMPT_ARGUMENT_WINDOWS_ENV)
        # ZCode chooses a new session's ID itself, so only a resume asks for one.
        return CliCommand(script, env, native_session_id)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)

    async def _session_exists(
        self, sandbox: Sandbox, session: HarnessSession, session_id: str
    ) -> bool:
        # A session lives in ZCode's SQLite store; its own files appear only for
        # attachments or subagents, so the store is asked for the exact top-level ID.
        script = (
            "import pathlib, sqlite3, sys\n"
            "uri = pathlib.Path(sys.argv[1]).as_uri() + '?mode=ro'\n"
            "row = sqlite3.connect(uri, uri=True).execute(\n"
            "    'select 1 from session where id = ? and parent_id is null', (sys.argv[2],)\n"
            ").fetchone()\n"
            "sys.exit(0 if row else 1)\n"
        )
        result = await sandbox.exec(
            [
                agent_python(session.os),
                "-c",
                script,
                str(agent_path(session.home) / trajectory.DB),
                session_id,
            ],
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        return result.ok
