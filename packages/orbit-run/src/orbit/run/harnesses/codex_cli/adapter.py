"""Codex CLI configuration and invocation."""

from __future__ import annotations

import hashlib
import json
import shlex
import tarfile
from functools import cached_property
from pathlib import PurePosixPath
from tempfile import TemporaryDirectory

from orbit.core.errors import AgentError, AgentUnsupportedError, ConfigError
from orbit.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from orbit.core.sandbox import Identity, Sandbox
from orbit.core.taskspec import OperatingSystem, StdioMcpServer
from orbit.core.trajectory import AtifTrajectory
from orbit.run.harnesses._npm import (
    agent_home,
    agent_path,
    npm_env,
    reported_version,
    version_mismatch,
)
from orbit.run.harnesses.base import (
    LOG_ROOT,
    CliCommand,
    CliHarness,
    NativeResult,
    install_skills,
)
from orbit.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import CodexCliSettings

DEFAULT_CLI_VERSION = "0.146.0"
NATIVE_PACKAGE_VERSION = "0.170.0"
NATIVE_PACKAGE_SHA256 = "a77ff87be7ba809d715728270a78f5ce3527dc07529416d1ef8929b90b8475eb"

_ACTIVATE_NATIVE_PACKAGE = """
set -eu
package=$1
binary=$2
mkdir -p "$(dirname "$binary")"
chmod +x "$package"/bin/* "$package"/codex-path/rg "$package"/codex-resources/bwrap
ln -sfn "$package/bin/codex" "$binary"
"$binary" --version
"""


class CodexCliHarness(CliHarness):
    """Runs Codex CLI's non-interactive ``exec`` mode inside the sandbox."""

    name = "codex-cli"
    package = "@openai/codex"
    binary = "codex"
    default_version = DEFAULT_CLI_VERSION
    settings_type = CodexCliSettings
    settings: CodexCliSettings
    logs = (LOG_ROOT, ".codex-orbit/sessions", trajectory.TRACES)
    # Its CODEX_HOME, where a login keeps its credentials.
    credentials = (".codex-orbit/auth.json",)

    def validate_os(self, operating_system: OperatingSystem) -> None:
        super().validate_os(operating_system)
        if self.settings.native_package_archive is None:
            return
        if operating_system is not OperatingSystem.LINUX:
            raise AgentUnsupportedError("the pinned native Codex package requires Linux x86-64")
        if self.version() != NATIVE_PACKAGE_VERSION:
            raise ConfigError(f"native_package_archive requires Codex {NATIVE_PACKAGE_VERSION}")
        # Validate and stage the archive once, before preparing any Task images.
        _ = self._native_package_directory

    def integrity(self) -> str:
        if self.settings.native_package_archive is not None:
            return f"sha256:{NATIVE_PACKAGE_SHA256}"
        return super().integrity()

    @cached_property
    def _native_package_directory(self) -> TemporaryDirectory[str]:
        archive = self.settings.native_package_archive
        assert archive is not None
        directory = TemporaryDirectory(prefix="orbit-codex-package-")
        try:
            with archive.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != NATIVE_PACKAGE_SHA256:
                raise ConfigError(f"native Codex archive has unexpected sha256 {digest}")
            with tarfile.open(archive, "r:gz") as package:
                package.extractall(directory.name, filter="data")
        except (OSError, tarfile.TarError) as error:
            directory.cleanup()
            raise ConfigError(f"could not stage native Codex package: {error}") from error
        except ConfigError:
            directory.cleanup()
            raise
        return directory

    async def install(self, sandbox: Sandbox) -> str:
        if self.settings.native_package_archive is None:
            return await super().install(sandbox)

        self.validate_os(sandbox.request.os)
        architecture = await sandbox.exec(["uname", "-m"], identity=Identity.AGENT, timeout_sec=60)
        if not architecture.ok or architecture.stdout.strip() != "x86_64":
            raise AgentUnsupportedError("the pinned native Codex package requires Linux x86-64")

        prefix = agent_path(await agent_home(sandbox)) / ".local"
        package_dir = prefix / "share" / "orbit" / "codex-package"
        binary = prefix / "bin" / self.binary
        await sandbox.upload_dir(
            self._native_package_directory.name, str(package_dir), identity=Identity.AGENT
        )
        result = await sandbox.exec(
            ["sh", "-c", _ACTIVATE_NATIVE_PACKAGE, "sh", str(package_dir), str(binary)],
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        output = f"{result.stdout}\n{result.stderr}"
        installed = reported_version(output, self.version()) if result.ok else None
        if installed is None:
            raise AgentError(version_mismatch(self.binary, self.version(), output))
        return installed

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        codex_home = agent_path(session.home) / ".codex-orbit"
        skills_root = codex_home / "skills"
        await sandbox.exec(["mkdir", "-p", str(skills_root)], identity=Identity.AGENT)
        await install_skills(sandbox, session, resources, skills_root)

        lines = [
            'model_provider = "orbit"',
            f"model = {json.dumps(session.model)}",
            "check_for_update_on_startup = false",
            'approval_policy = "never"',
            'sandbox_mode = "danger-full-access"',
            # Codex searches by default, on OpenAI's side.
            f"web_search = {json.dumps(self._web_search(session))}",
        ]
        if self.settings.reasoning_effort != "default":
            lines.append(f"model_reasoning_effort = {json.dumps(self.settings.reasoning_effort)}")
        lines.extend(
            [
                "",
                "[model_providers.orbit]",
                'name = "Orbit Gateway"',
                f"base_url = {json.dumps(session.gateway_url + '/v1')}",
                'env_key = "ORBIT_GATEWAY_TOKEN"',
                'wire_api = "responses"',
                "",
            ]
        )
        for resolved in resources.mcp_servers:
            server = resolved.server
            if resolved.name == CUA_DESKTOP_NAME:
                await stage_cua_desktop(sandbox, session.home)
            lines.append(f"[mcp_servers.{json.dumps(resolved.name)}]")
            if isinstance(server, StdioMcpServer):
                lines.append(
                    f"command = {json.dumps(server.command.replace('{home}', session.home))}"
                )
                args = [arg.replace("{home}", session.home) for arg in server.args]
                lines.append(f"args = [{', '.join(json.dumps(arg) for arg in args)}]")
                if server.cwd:
                    lines.append(f"cwd = {json.dumps(server.cwd.replace('{home}', session.home))}")
                if server.environment:
                    rendered = ", ".join(
                        f"{json.dumps(key)} = {json.dumps(value.replace('{home}', session.home))}"
                        for key, value in sorted(server.environment.items())
                    )
                    lines.append(f"env = {{ {rendered} }}")
            else:
                lines.append(f"url = {json.dumps(server.url)}")
                if resolved.headers:
                    rendered = ", ".join(
                        f"{json.dumps(key)} = {json.dumps(value)}"
                        for key, value in sorted(resolved.headers.items())
                    )
                    lines.append(f"http_headers = {{ {rendered} }}")
            lines.append("")
        await sandbox.write_file(
            codex_home / "config.toml",
            ("\n".join(lines) + "\n").encode(),
            identity=Identity.AGENT,
        )

    def _web_search(self, session: HarnessSession) -> str:
        return "live" if self.settings.web_search and session.provider_web_tools else "disabled"

    async def prepare_command(
        self,
        prompt_path: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
    ) -> CliCommand:
        argv = [
            "codex",
            "exec",
            "--json",
            "--model",
            session.model,
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
        if native_session_id is not None:
            argv.extend(["resume", native_session_id])
        # `-` reads the prompt from stdin.
        argv.append("-")
        script = (
            "unset OPENAI_API_KEY OPENAI_BASE_URL CODEX_ACCESS_TOKEN; "
            f"{shlex.join(argv)} < {shlex.quote(prompt_path)}"
        )
        env = {
            **await npm_env(sandbox, session.home),
            "CODEX_HOME": str(agent_path(session.home) / ".codex-orbit"),
            # The rollout keeps no record of a code-mode script's shell commands; this does.
            "CODEX_ROLLOUT_TRACE_ROOT": str(agent_path(session.home) / trajectory.TRACES),
            "NO_COLOR": "1",
            "ORBIT_GATEWAY_TOKEN": session.token,
        }
        # Codex chooses a new thread's ID itself, so only a resume asks for one.
        return CliCommand(script, env, native_session_id)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)
