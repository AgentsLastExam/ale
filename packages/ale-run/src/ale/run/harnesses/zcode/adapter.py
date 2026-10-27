"""ZCode configuration, installation and invocation."""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel

from ale.core.errors import AgentError, ConfigError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import StdioMcpServer
from ale.core.trajectory import AtifTrajectory
from ale.run.agent_resources import continuation_fingerprint
from ale.run.harnesses._npm import (
    agent_home,
    agent_path,
    bash_command,
    is_windows,
    npm_env,
    reported_version,
    version_mismatch,
)
from ale.run.harnesses.base import (
    LOG_ROOT,
    PROMPT_ARGUMENT_WINDOWS_ENV,
    CliCommand,
    CliHarness,
    NativeResult,
    prompt_argument,
)
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import ZCodeSettings

#: The CLI version inside ZCode's desktop release 3.16.3, its only official distribution.
DEFAULT_CLI_VERSION = "0.17.0"
RELEASE = "3.16.3"
#: The official Linux installer, pinned by the sha512 its release's `latest.yml` publishes.
#: The CLI is plain JavaScript, byte-identical in every platform's installer, so this one
#: payload serves Linux and Windows alike.
INSTALLER_URL = (
    f"https://cdn-zcode.z.ai/zcode/electron/releases/{RELEASE}/linux-x64/"
    f"ZCode-{RELEASE}-linux-x64.deb"
)
INSTALLER_SHA512 = (
    "5af39bd81e0d9d174d14800ab8c27e4b4bc24feb907d57e98853cbb2b686f0a4"
    "021d35d046cbc2e7c3a44f3efdb277db9a60a401eb57522fd1277f5b286f93f2"
)
#: The files ALE runs, by their path in the installer's data archive.
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
INSTALL_DIR = ".local/lib/zcode"
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

# Run by the Sandbox's own Python on Linux and Windows alike. Matching files are reused;
# otherwise the installer is downloaded, accepted only by its pinned sha512, and only the
# pinned members of its data archive are written, each accepted only by its own sha256.
_INSTALL = r"""
import hashlib, json, os, sys, tarfile, tempfile, urllib.request

spec = json.loads(sys.argv[1])
target = spec["target"]
wanted = {member: (name, digest) for member, (name, digest) in spec["files"].items()}

def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        while block := stream.read(1 << 20):
            value.update(block)
    return value.hexdigest()

def present(name, expected):
    path = os.path.join(target, name)
    return not os.path.islink(path) and os.path.isfile(path) and digest(path) == expected

if all(present(name, expected) for name, expected in wanted.values()):
    sys.exit(0)
os.makedirs(target, exist_ok=True)
handle, installer = tempfile.mkstemp(dir=target, prefix=".installer-")
try:
    checksum = hashlib.sha512()
    response = urllib.request.urlopen(spec["url"], timeout=600)
    with os.fdopen(handle, "wb") as out, response:
        while block := response.read(1 << 20):
            checksum.update(block)
            out.write(block)
    if checksum.hexdigest() != spec["sha512"]:
        sys.exit(f"{spec['url']} has sha512 {checksum.hexdigest()}, not the pinned one")

    class Member:
        # One ar member of the Debian package, read in place.
        def __init__(self, stream, size):
            self.stream, self.left = stream, size
        def read(self, size=-1):
            size = self.left if size is None or size < 0 else min(size, self.left)
            data = self.stream.read(size)
            self.left -= len(data)
            return data

    found = {}
    with open(installer, "rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            sys.exit("the installer is not a Debian package")
        while header := stream.read(60):
            name, size = header[:16].decode().strip().rstrip("/"), int(header[48:58])
            start = stream.tell()
            if name.startswith("data.tar"):
                with tarfile.open(fileobj=Member(stream, size), mode="r|*") as archive:
                    for entry in archive:
                        if entry.name in wanted and entry.isreg():
                            found[entry.name] = archive.extractfile(entry).read()
            stream.seek(start + size + size % 2)
    for member, (name, expected) in wanted.items():
        data = found.get(member)
        if data is None or hashlib.sha256(data).hexdigest() != expected:
            sys.exit(f"the installer's {member} is missing or not the pinned build")
        handle, staged = tempfile.mkstemp(dir=target, prefix=".file-")
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(staged, os.path.join(target, name))
finally:
    if os.path.exists(installer):
        os.unlink(installer)
"""


class ZCodeHarness(CliHarness):
    """Runs the ZCode CLI headless under the Sandbox's Node, from ZCode's desktop release."""

    name = "zcode"
    package = f"cdn-zcode.z.ai/zcode/electron/releases/{RELEASE}"
    binary = "zcode"
    default_version = DEFAULT_CLI_VERSION
    settings_type = ZCodeSettings
    settings: ZCodeSettings
    logs = (LOG_ROOT, trajectory.DB_DIR, trajectory.ARTIFACTS, trajectory.AGENTS)

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
        """``sha256:`` of the files ALE runs, named with their own digests."""
        lines = sorted(f"{name} {digest}" for name, digest in FILES.values())
        return "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()

    async def install(self, sandbox: Sandbox) -> str:
        home = await agent_home(sandbox)
        target = agent_path(home) / INSTALL_DIR
        spec = {
            "url": INSTALLER_URL,
            "sha512": INSTALLER_SHA512,
            "target": str(target),
            "files": {member: list(entry) for member, entry in FILES.items()},
        }
        result = await sandbox.exec(
            ["python" if is_windows(home) else "python3", "-c", _INSTALL, json.dumps(spec)],
            timeout_sec=1200,
            identity=Identity.AGENT,
        )
        if not result.ok:
            detail = (result.stderr or result.stdout)[-800:]
            raise AgentError(f"could not install zcode {self.version()}: {detail}")
        probe = await sandbox.exec(
            bash_command(
                home, f"node {shlex.quote(str(target / 'zcode.cjs'))} --version", login=False
            ),
            env=await npm_env(sandbox, home),
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        output = probe.stdout + "\n" + probe.stderr
        installed = reported_version(output, self.version()) if probe.ok else None
        if installed is None:
            raise AgentError(version_mismatch("zcode", self.version(), output))
        return installed

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
                native = {"type": "http", "url": server.url}
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
            "providerId": "ale",
            "providerName": "ALE Gateway",
            "config": {
                "group": "standard-personal",
                # ZCode reads only a literal key here; it is the episode token, readable
                # by the agent account alone.
                "access": {"type": "api-key", "apiKey": session.token},
                "api": {"type": api, "baseUrl": session.gateway_url + suffix},
                "personalModelIds": [session.model],
            },
        }
        personal = {
            "schemaVersion": 1,
            "config": {
                "providerOrder": ["ale"],
                "providerConfigRules": {"providerRules": [provider]},
                "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
                "defaultModelSelection": {"providerId": "ale", "modelId": session.model},
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
            "node",
            str(home / INSTALL_DIR / "zcode.cjs"),
            "--mode",
            "yolo",
            "--output-format",
            "stream-json",
            "--cwd",
            str(home),
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
            **await npm_env(sandbox, session.home),
            "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": str(home / INSTALL_DIR / "zcode-builtin.json"),
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
                "python" if is_windows(session.home) else "python3",
                "-c",
                script,
                str(agent_path(session.home) / trajectory.DB),
                session_id,
            ],
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        return result.ok

    def _fingerprint(self, session: HarnessSession) -> str:
        return continuation_fingerprint(
            harness=self.name,
            model=session.model,
            settings={
                **self.settings.model_dump(mode="json"),
                "gateway_dialect": self.gateway_dialect,
            },
            resources_digest=session.resources_digest,
            authentication=session.authentication,
            profile_slot_id=session.profile_slot_id,
        )
