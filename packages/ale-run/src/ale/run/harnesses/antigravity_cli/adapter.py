"""Antigravity CLI installation, configuration and invocation."""

from __future__ import annotations

import json
import shlex
from pathlib import PurePosixPath
from typing import Any

from ale.core.errors import AgentUnsupportedError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import OperatingSystem, StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.harnesses._npm import agent_path, is_windows, npm_env
from ale.run.harnesses._release import ReleaseAsset
from ale.run.harnesses.base import LOG_ROOT, CliCommand, CliHarness, NativeResult
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import AntigravityCliSettings

DEFAULT_CLI_VERSION = "1.4.7"
_RELEASE = (
    "https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.7-5900205422411776"
)
#: Google's 1.4.7 builds, from its updater manifests (`/manifests/<os>_<arch>.json`), which
#: name only the latest build. Linux ships a tar.gz holding the one executable.
RELEASE_ASSETS = {
    (OperatingSystem.LINUX, "x86_64"): ReleaseAsset(
        f"{_RELEASE}/linux-x64/cli_linux_x64.tar.gz",
        "67fd4e70be739b1f8b18f90b511da8cd2a7aef24e888e5837402e025bf7fdbfc",
        member="antigravity",
        member_sha256="40349135371a8ed1f1af1e9f8b741de99174ae9fce6380b96c2d1f543480af7a",
    ),
    (OperatingSystem.LINUX, "aarch64"): ReleaseAsset(
        f"{_RELEASE}/linux-arm/cli_linux_arm64.tar.gz",
        "b273c791b7647f680598fa1dbdf9c8086bdc42374b2276fa70e900bf8120e1d5",
        member="antigravity",
        member_sha256="484f6bacb0828a4d6c2768ce94d02a3cc347e72e67572d6b97ab0ff9025474fb",
    ),
    (OperatingSystem.WINDOWS, "x86_64"): ReleaseAsset(
        f"{_RELEASE}/windows-x64/cli_windows_x64.exe",
        "22588b6d5a6f6d2d66091c73dfb9255edc7bdafe9aff9dd20d958ce116496695",
    ),
    (OperatingSystem.WINDOWS, "aarch64"): ReleaseAsset(
        f"{_RELEASE}/windows-arm/cli_windows_arm64.exe",
        "eafcb0ccd9afd60c78983dc7d64d859635bb065bce1bb05817b0422f334d1b1e",
    ),
}

#: The Gemini API models agy 1.4.7 offers in API-key mode (`agy models`): the run's model
#: is what the Gateway sends upstream, agy selects it by its catalog family and effort.
_MODELS: dict[str, tuple[str, frozenset[str]]] = {
    "gemini-4-argon": ("gemini-4-argon", frozenset({"low", "medium", "high"})),
    "gemini-3.8-flash": ("gemini-3.8-flash", frozenset({"low", "medium", "high"})),
    "gemini-3.7-flash": ("gemini-3.7-flash", frozenset({"low", "medium", "high"})),
    "gemini-3.6-flash": ("gemini-3.6-flash", frozenset({"low", "medium", "high"})),
    "gemini-3.1-pro-preview": ("gemini-3.1-pro", frozenset({"low", "high"})),
}

# Wraps the recorded prompt file as the one stream-json input message agy documents, so the
# prompt reaches the CLI on stdin and never through argv or shell quoting.
_REQUEST = (
    "import json, sys\n"
    "text = open(sys.argv[1], encoding='utf-8', newline='').read()\n"
    "with open(sys.argv[2], 'w', encoding='utf-8', newline='\\n') as out:\n"
    "    out.write(json.dumps({'event': 'user', 'message': {'content': text}}) + '\\n')\n"
)
REQUEST_PATH = f"{LOG_ROOT}/request.jsonl"
#: Credentials and backends agy would otherwise prefer over the Gateway's API-key mode.
_AMBIENT = ("GOOGLE_API_KEY", "AGY_ADC_AUTH", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT")


class AntigravityCliHarness(CliHarness):
    """Runs Google's Antigravity CLI headless, with Gemini API-key mode aimed at the Gateway."""

    name = "antigravity-cli"
    package = "https://antigravity.google/cli"
    binary = "agy"
    default_version = DEFAULT_CLI_VERSION
    release_assets = RELEASE_ASSETS
    settings_type = AntigravityCliSettings
    settings: AntigravityCliSettings
    logs = (
        LOG_ROOT,
        trajectory.BRAIN,
        trajectory.CONVERSATIONS,
        ".gemini/antigravity-cli/log",
    )

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        self._check_model(session.model)
        gemini = agent_path(session.home) / ".gemini"
        skills_root = gemini / "antigravity-cli" / "skills"
        # As the agent, so agy can create its own state beside the files written here.
        await sandbox.exec(
            ["mkdir", "-p", str(skills_root), str(gemini / "config")], identity=Identity.AGENT
        )
        # API-key mode, documented: the key comes from GEMINI_API_KEY at launch.
        await sandbox.write_file(
            gemini / "antigravity-cli" / "settings.json",
            (json.dumps({"modelProvider": "gemini"}, indent=2) + "\n").encode(),
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
        servers: dict[str, dict[str, Any]] = {}
        for resolved in resources.mcp_servers:
            server = resolved.server
            if resolved.name == CUA_DESKTOP_NAME:
                await stage_cua_desktop(sandbox, session.home)
            if isinstance(server, StdioMcpServer):
                entry: dict[str, Any] = {
                    "command": server.command.replace("{home}", session.home),
                    "args": [arg.replace("{home}", session.home) for arg in server.args],
                }
                if server.environment:
                    entry["env"] = {
                        key: value.replace("{home}", session.home)
                        for key, value in sorted(server.environment.items())
                    }
                if server.cwd:
                    entry["cwd"] = server.cwd.replace("{home}", session.home)
            else:
                entry = {"serverUrl": server.url}
            servers[resolved.name] = entry
        await sandbox.write_file(
            gemini / "config" / "mcp_config.json",
            (json.dumps({"mcpServers": servers}, indent=2, ensure_ascii=False) + "\n").encode(),
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
        request_path = str(home / REQUEST_PATH)
        argv = [
            _binary(session.home),
            "--dangerously-skip-permissions",
            "--print-timeout=0s",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            _MODELS[session.model][0] if session.model in _MODELS else session.model,
        ]
        if self.settings.effort != "default":
            argv.extend(["--effort", self.settings.effort])
        if native_session_id is not None:
            argv.extend(["--conversation", native_session_id])
        python = "python" if is_windows(session.home) else "python3"
        script = (
            f"unset {' '.join(_AMBIENT)}; "
            f"{shlex.join([python, '-Ic', _REQUEST, prompt_path, request_path])} && "
            f"{shlex.join(argv)} < {shlex.quote(request_path)}"
        )
        env = {
            **await npm_env(sandbox, session.home),
            "HOME": str(home),
            "GEMINI_API_KEY": session.token,
            "GOOGLE_GEMINI_BASE_URL": session.gateway_url,
            "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
        }
        return CliCommand(script, env, native_session_id)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)

    def _check_model(self, model: str) -> None:
        """Fail before launch unless agy's catalog serves the model at the chosen effort."""
        if model not in _MODELS:
            raise AgentUnsupportedError(
                f"antigravity-cli {self.version()} serves {model!r} under no catalog model; "
                f"use one of {', '.join(sorted(_MODELS))}"
            )
        efforts = _MODELS[model][1]
        if self.settings.effort != "default" and self.settings.effort not in efforts:
            raise AgentUnsupportedError(
                f"agy offers {model} at effort {', '.join(sorted(efforts))}, "
                f"not {self.settings.effort}"
            )


def _binary(home: str) -> str:
    return f"{agent_path(home)}/.local/bin/agy" + (".exe" if is_windows(home) else "")
