"""OpenCode configuration and invocation."""

from __future__ import annotations

import json
import re
import shlex
import uuid
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel

from orbit.core.errors import AgentError, AgentUnsupportedError, ConfigError
from orbit.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from orbit.core.sandbox import Identity, Sandbox
from orbit.core.taskspec import OperatingSystem, StdioMcpServer
from orbit.core.trajectory import AtifTrajectory
from orbit.run.harnesses._npm import agent_home, agent_path, agent_python, bash_command, npm_env
from orbit.run.harnesses._release import ReleaseAsset, ReleaseAssets, ensure_release_binary
from orbit.run.harnesses.base import (
    LOG_ROOT,
    CliCommand,
    CliHarness,
    NativeResult,
    http_headers,
    install_skills,
)
from orbit.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

from . import trajectory
from .settings import OpenCodeSettings

DEFAULT_CLI_VERSION = "2.3.2"
#: ``OPENCODE_CONFIG_DIR``, in place of the agent's ``~/.config/opencode``: Skills, and
#: Orbit's configuration document beside it.
STATE = trajectory.STATE
#: The custom provider every model call goes through.
PROVIDER = trajectory.PROVIDER
#: OpenCode's documented package for a compatible endpoint of each Gateway dialect, and
#: the base URL path its client appends its route to.
_PACKAGES = {
    "openai-chat-completions": ("@opencode/ai/providers/openai-compatible", "/v1"),
    "openai-responses": ("@opencode/ai/providers/openai-compatible/responses", "/v1"),
    "anthropic": ("@opencode/ai/providers/anthropic-compatible", "/v1"),
    "gemini": ("@opencode/ai/providers/google", "/v1beta"),
}
#: Built-in plugins that probe the loopback for a local model server at every start
#: (Ollama's port 11434, LM Studio's 1234 and vLLM's 8000), which in a Sandbox may be the
#: Task's own service.
_LOCAL_DISCOVERY = (
    "-opencode.provider.ollama",
    "-opencode.provider.lmstudio",
    "-opencode.provider.vllm",
)
_RIPGREP = "https://github.com/BurntSushi/ripgrep/releases/download/15.1.0"
#: The ripgrep release OpenCode downloads on its first search, by the digests OpenCode
#: pins in ``core/src/ripgrep/binary.ts``, and each archive's ``rg``.
RIPGREP: ReleaseAssets = {
    (OperatingSystem.LINUX, "x86_64"): ReleaseAsset(
        f"{_RIPGREP}/ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz",
        "1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599",
        "ripgrep-15.1.0-x86_64-unknown-linux-musl/rg",
        "ebeaf56f8a25e102e9419933423738b3a2a613a444fd749d695e15eba53f71f2",
    ),
    (OperatingSystem.LINUX, "aarch64"): ReleaseAsset(
        f"{_RIPGREP}/ripgrep-15.1.0-aarch64-unknown-linux-gnu.tar.gz",
        "2b661c6ef508e902f388e9098d9c4c5aca72c87b55922d94abdba830b4dc885e",
        "ripgrep-15.1.0-aarch64-unknown-linux-gnu/rg",
        "968cabe8efed72fd8fd482cb76b6084fcb695fc5293af7fb62296b02f487fb69",
    ),
    (OperatingSystem.WINDOWS, "x86_64"): ReleaseAsset(
        f"{_RIPGREP}/ripgrep-15.1.0-x86_64-pc-windows-msvc.zip",
        "124510b94b6baa3380d051fdf4650eaa80a302c876d611e9dba0b2e18d87493a",
        "ripgrep-15.1.0-x86_64-pc-windows-msvc/rg.exe",
        "decdd4992f3f1b9a5ef9898f1b40ab16886d579d6516b4efd3d5eaa19364e408",
    ),
    (OperatingSystem.WINDOWS, "aarch64"): ReleaseAsset(
        f"{_RIPGREP}/ripgrep-15.1.0-aarch64-pc-windows-msvc.zip",
        "00d931fb5237c9696ca49308818edb76d8eb6fc132761cb2a1bd616b2df02f8e",
        "ripgrep-15.1.0-aarch64-pc-windows-msvc/rg.exe",
        "f7799d737b520e00b10dfa72def23904fe66fb03315636a7b78549845ee9609c",
    ),
    (OperatingSystem.MACOS, "aarch64"): ReleaseAsset(
        f"{_RIPGREP}/ripgrep-15.1.0-aarch64-apple-darwin.tar.gz",
        "378e973289176ca0c6054054ee7f631a065874a352bf43f0fa60ef079b6ba715",
        "ripgrep-15.1.0-aarch64-apple-darwin/rg",
        "4fdf1d8365af224bc70e3c1490d8461d859c37cc70e739a11e987af0215f3e94",
    ),
}
#: Run by the agent's Python on an exported session: the sessions it spawned, as each
#: ``subagent`` call's result names them, separated by spaces.
_CHILDREN = r"""
import json, re, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(0)
found = []
messages = data.get("messages") if isinstance(data, dict) else None
for message in messages if isinstance(messages, list) else ():
    content = message.get("content") if isinstance(message, dict) else None
    for part in content if isinstance(content, list) else ():
        if not isinstance(part, dict) or part.get("name") != "subagent":
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        child = metadata.get("sessionID")
        if isinstance(child, str) and re.fullmatch(r"ses[A-Za-z0-9_]+", child):
            found.append(child)
print(" ".join(dict.fromkeys(found)))
"""
#: No catalog refresh or updater at start; the configuration turns updates off too.
_OFFLINE = {"OPENCODE_DISABLE_MODELS_FETCH": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1"}
#: What OpenCode substitutes anywhere in its configuration's text.
_SUBSTITUTION = re.compile(r"\{(?:env|file):")
_STAMP = (
    "import datetime; "
    "print(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))"
)
#: Every invocation runs from this file, as a Windows desktop agent's command line may not
#: exceed 1,024 characters: `sh invoke.sh <python> <prompt file> <session ID> <command…>`.
INVOKE = f"{STATE}/invoke.sh"
_INVOKE_SCRIPT = (
    r"""state="$(dirname -- "$0")"; python=$1; prompt=$2; requested=$3
shift 3
# Outranks a workspace's own `opencode.json` and `.opencode`.
OPENCODE_CONFIG_CONTENT="$(cat -- "$state/@CONFIG@")"
export OPENCODE_CONFIG_CONTENT
"$@" < "$prompt"
status=$?
# OpenCode keeps sessions in SQLite only. After every invocation the session and every
# session it spawned, at any depth, are exported as the native evidence,
# `<root>/<time>/<session>.json`, kept once the root's is there.
exports="$state/@EXPORTS@/$requested"
stamp="$("$python" -Ic @STAMP@)"
work="$exports/.$stamp"
mkdir -p -- "$work"
pending=$requested
while [ -n "$pending" ]; do
    id="${pending%% *}"; pending="${pending#"$id"}"; pending="${pending# }"
    [ -e "$work/$id.json" ] && continue
    if opencode session export "$id" --standalone < /dev/null > "$work/$id.json"; then
        pending="$pending $("$python" -Ic @CHILDREN@ "$work/$id.json")"
        pending="${pending# }"; pending="${pending% }"
    else
        rm -f -- "$work/$id.json"
    fi
done
if [ -e "$work/$requested.json" ]; then
    mv -- "$work" "$exports/$stamp"
else
    rm -rf -- "$work"
fi
exit "$status"
""".replace("@CONFIG@", str(PurePosixPath(trajectory.CONFIG).relative_to(STATE)))
    .replace("@EXPORTS@", str(PurePosixPath(trajectory.EXPORTS).relative_to(STATE)))
    .replace("@STAMP@", shlex.quote(_STAMP))
    .replace("@CHILDREN@", shlex.quote(_CHILDREN))
)


class OpenCodeHarness(CliHarness):
    """Runs ``opencode run`` with a private server, the prompt on stdin, one exact session."""

    name = "opencode"
    package = "@opencode/cli"
    binary = "opencode"
    default_version = DEFAULT_CLI_VERSION
    settings_type = OpenCodeSettings
    settings: OpenCodeSettings
    logs = (LOG_ROOT, trajectory.EXPORTS, trajectory.CONFIG)

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "openai-chat-completions",
        settings: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if gateway_dialect not in _PACKAGES:
            raise ConfigError(f"opencode does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        super().__init__(cli_version=cli_version, settings=settings)

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        super().validate_resources(resources)
        named: dict[str, str] = {}
        for resolved in resources.mcp_servers:
            normalized = trajectory.normalized_name(resolved.name)
            if normalized in named:
                raise AgentUnsupportedError(
                    f"MCP servers {named[normalized]} and {resolved.name}: opencode names the "
                    f"tools of both {normalized}_<tool>"
                )
            named[normalized] = resolved.name
            server = resolved.server
            values = (
                (server.command, *server.args, server.cwd or "", *server.environment.values())
                if isinstance(server, StdioMcpServer)
                else (server.url,)
            )
            if any(_SUBSTITUTION.search(value) for value in values):
                raise AgentUnsupportedError(
                    f"MCP server {resolved.name}: opencode substitutes {{env:}} and {{file:}} "
                    "in its configuration, so the value would change"
                )

    async def install(self, sandbox: Sandbox) -> str:
        version = await super().install(sandbox)
        # OpenCode downloads ripgrep on its first search, which a sealed Sandbox would
        # refuse; the same pinned build goes where OpenCode looks before downloading.
        home = await agent_home(sandbox)
        paths = await sandbox.exec(
            bash_command(home, "opencode debug paths", login=False),
            env={**await npm_env(sandbox, home), **_OFFLINE},
            timeout_sec=120,
            identity=Identity.AGENT,
        )
        found = (line.split(None, 1) for line in paths.stdout.splitlines())
        directory = next(
            (pair[1].strip() for pair in found if pair[:1] == ["bin"] and len(pair) == 2), None
        )
        if not paths.ok or not directory:
            raise AgentError(f"opencode debug paths named no bin directory: {paths.stderr[-400:]}")
        await ensure_release_binary(
            sandbox,
            binary="rg",
            version="15.1.0",
            assets=RIPGREP,
            directory=str(agent_path(directory)),
        )
        return version

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        if "#" in session.model:
            # `#` separates a model from its variant in every OpenCode model reference.
            raise AgentUnsupportedError(
                f"opencode cannot name a model containing '#': {session.model!r}"
            )
        home = agent_path(session.home)
        state = home / STATE
        skills_root = state / "skills"
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
                    "type": "local",
                    "command": [
                        server.command.replace("{home}", session.home),
                        *(arg.replace("{home}", session.home) for arg in server.args),
                    ],
                }
                if server.cwd:
                    native["cwd"] = server.cwd.replace("{home}", session.home)
                if server.environment:
                    native["environment"] = {
                        key: value.replace("{home}", session.home)
                        for key, value in server.environment.items()
                    }
            else:
                # Streamable HTTP, OpenCode's only remote transport: an SSE server gets
                # the same entry, as Harbor writes it. Remote MCP authentication is never
                # copied from the Host, so OpenCode's sign-in on a 401 stays off.
                native = {"type": "remote", "url": server.url, "oauth": False}
                native.update(http_headers(resolved))
            # On the model's own tool list: under Code Mode, OpenCode's default, a call
            # is made from a script and its result is not recorded.
            native["codemode"] = False
            servers[resolved.name] = native

        package, suffix = _PACKAGES[self.gateway_dialect]
        model: dict[str, Any] = {}
        capabilities: dict[str, Any] = {}
        if self.settings.reasoning_effort != "default":
            capabilities["reasoning"] = {
                "supported": True,
                "effort": [self.settings.reasoning_effort],
            }
        if self.settings.input != "default":
            capabilities["input"] = list(self.settings.input)
        if capabilities:
            model["capabilities"] = capabilities
        limit: dict[str, int] = {}
        if isinstance(self.settings.context_window, int):
            limit["context"] = self.settings.context_window
        if isinstance(self.settings.max_output_tokens, int):
            limit["output"] = self.settings.max_output_tokens
        if limit:
            model["limit"] = limit
        config: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "model": f"{PROVIDER}/{session.model}",
            "update": "disable",
            "share": "disabled",
            "providers": {
                PROVIDER: {
                    "name": "Orbit Gateway",
                    "package": package,
                    # The provider's credential, read from the environment.
                    "env": ["ORBIT_GATEWAY_TOKEN"],
                    "settings": {"baseURL": session.gateway_url + suffix},
                    "models": {session.model: model},
                }
            },
            # Every other provider OpenCode knows, its keyless hosted models included,
            # is unusable, however it became known.
            "policies": [
                {"action": "provider.use", "resource": "*", "effect": "deny"},
                {"action": "provider.use", "resource": PROVIDER, "effect": "allow"},
            ],
            "plugins": list(_LOCAL_DISCOVERY),
            "websearch": (
                {"provider": self.settings.web_search}
                if self.settings.web_search != "off"
                else False
            ),
            "mcp": {"servers": servers},
        }
        if isinstance(self.settings.max_steps, int):
            config["agents"] = {"build": {"steps": self.settings.max_steps}}
        await sandbox.write_file(
            str(home / trajectory.CONFIG),
            (json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
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
        # `--session` continues the session with this ID, or creates it under this ID.
        requested = native_session_id or f"ses_{uuid.uuid4().hex}"
        reference = f"{PROVIDER}/{session.model}"
        if self.settings.reasoning_effort != "default":
            reference += f"#{self.settings.reasoning_effort}"
        argv = [
            "opencode",
            "run",
            # A server of its own that ends with the invocation, never the background
            # service another process may have started.
            "--standalone",
            "--format",
            "json",
            "--auto",
            "--model",
            reference,
            "--session",
            requested,
        ]
        invoke = str(home / INVOKE)
        await sandbox.write_file(invoke, _INVOKE_SCRIPT.encode(), identity=Identity.AGENT)
        script = shlex.join(["sh", invoke, agent_python(session.os), prompt_path, requested, *argv])
        env = {
            **await npm_env(sandbox, session.home),
            # In place of `~/.config/opencode`.
            "OPENCODE_CONFIG_DIR": str(home / STATE),
            **_OFFLINE,
            "ORBIT_GATEWAY_TOKEN": session.token,
            "NO_COLOR": "1",
        }
        return CliCommand(script, env, requested)

    def read_result(self, stdout: str) -> NativeResult:
        return trajectory.read_result(stdout)

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return trajectory.is_session_file(path, session_id)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        return trajectory.parse_trajectory(context)
