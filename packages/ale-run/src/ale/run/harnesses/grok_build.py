"""Official xAI Grok Build CLI as an autonomous harness."""

from __future__ import annotations

import base64
import json
import re
import shlex
import uuid
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ale.core.errors import AgentError, ConfigError, NativeContinuationError
from ale.core.harness import (
    AgentRun,
    AutonomousHarness,
    EffectiveAgentResources,
    HarnessSession,
    NativeContinuation,
    ResumeSupport,
    TrajectoryParseContext,
)
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import StdioMcpServer, StreamableHttpMcpServer
from ale.core.trajectory import (
    AtifAgent,
    AtifContentPart,
    AtifImageSource,
    AtifObservation,
    AtifObservationResult,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from ale.run.agent_resources import continuation_fingerprint
from ale.run.harnesses._npm import agent_home, npm_env
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

__all__ = ["GrokBuildHarness", "GrokBuildSettings"]

DEFAULT_CLI_VERSION = "0.2.112"
TRANSCRIPT_NAME = "transcript.jsonl"
STDERR_NAME = "stderr.log"
CHAT_NAME = "session_chat_history.jsonl"
UPDATES_NAME = "session_updates.jsonl"
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
_DATA_URL = re.compile(r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=_-]+)")
_HEADLESS_DISABLED_TOOLS = ("ask_user_question", "enter_plan_mode", "exit_plan_mode")
_API_BACKENDS = {
    "anthropic": "messages",
    "openai-chat-completions": "chat_completions",
    "openai-responses": "responses",
}


class GrokBuildSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: PositiveInt | Literal["default", "unlimited"] = "unlimited"
    reasoning_effort: Literal[
        "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "high"
    disabled_tools: tuple[str, ...] = ()

    @field_validator("disabled_tools")
    @classmethod
    def _nonempty_tools(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("tool names must be non-empty")
        return values


class GrokBuildHarness(AutonomousHarness):
    name = "grok-build"
    resume_support = ResumeSupport.NATIVE
    logs = (TRANSCRIPT_NAME, STDERR_NAME, CHAT_NAME, UPDATES_NAME)
    __slots__ = ("cli_version", "gateway_dialect", "settings")

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        gateway_dialect: str = "openai-responses",
        settings: GrokBuildSettings | dict[str, Any] | None = None,
    ) -> None:
        self.cli_version = cli_version
        if gateway_dialect not in _API_BACKENDS:
            raise ConfigError(f"grok-build does not support gateway dialect {gateway_dialect!r}")
        self.gateway_dialect = gateway_dialect
        try:
            self.settings = (
                settings
                if isinstance(settings, GrokBuildSettings)
                else GrokBuildSettings.model_validate(settings or {})
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid grok-build settings: {exc}") from exc

    def version(self) -> str:
        return self.cli_version or DEFAULT_CLI_VERSION

    def integrity(self) -> str:
        return f"npm:@xai-official/grok@{self.version()}"

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        for resolved in resources.mcp_servers:
            if not isinstance(resolved.server, StdioMcpServer | StreamableHttpMcpServer):
                raise ConfigError(f"unsupported MCP transport for {resolved.name}")

    async def install(self, sandbox: Sandbox) -> str:
        home = await agent_home(sandbox)
        install_home = f"{home}/.grok-build-install"
        env = {
            **npm_env(home),
            "GROK_HOME": install_home,
            "GROK_MANAGED_BY_NPM": "1",
        }
        binary = f"{install_home}/bin/grok"
        probe = await sandbox.exec(
            ["sh", "-c", f"test -x {shlex.quote(binary)} && {shlex.quote(binary)} --version"],
            env=env,
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        installed = _version(probe.stdout + probe.stderr) if probe.ok else None
        if installed != self.version():
            result = await sandbox.exec(
                [
                    "npm",
                    "install",
                    "-g",
                    "--force",
                    "--prefix",
                    f"{home}/.local",
                    f"@xai-official/grok@{self.version()}",
                ],
                env=env,
                timeout_sec=1200,
                identity=Identity.AGENT,
            )
            if not result.ok:
                raise AgentError(
                    f"could not install Grok Build {self.version()}: "
                    f"{(result.stderr or result.stdout)[-800:]}"
                )
            probe = await sandbox.exec(
                [binary, "--version"], env=env, timeout_sec=60, identity=Identity.AGENT
            )
            installed = _version(probe.stdout + probe.stderr) if probe.ok else None
        if installed != self.version():
            raise AgentError(
                f"Grok Build reports {installed or 'no version'} after installing {self.version()}"
            )
        return installed

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        grok_home = PurePosixPath(session.home) / ".grok-ale"
        skills_root = grok_home / "skills"
        await sandbox.exec(["mkdir", "-p", str(skills_root)], identity=Identity.AGENT)
        for skill in resources.skills:
            target = skills_root / skill.name
            await sandbox.upload_dir(str(skill.path), target, identity=Identity.AGENT)
            if skill.executable_files:
                await sandbox.exec(
                    ["chmod", "+x", *(str(target / path) for path in skill.executable_files)],
                    identity=Identity.AGENT,
                )

        lines = [
            "[models]",
            'default = "ale"',
            "",
            '[model."ale"]',
            f"model = {_toml(session.model)}",
            f"base_url = {_toml(session.gateway_url + '/v1')}",
            'name = "ALE Gateway"',
            'env_key = "ALE_GATEWAY_TOKEN"',
            f"api_backend = {_toml(_API_BACKENDS[self.gateway_dialect])}",
            "supports_reasoning_effort = true",
            "",
            "[features]",
            "telemetry = false",
            "",
            "[telemetry]",
            "trace_upload = false",
            "mixpanel_enabled = false",
            "",
            "[cli]",
            "auto_update = false",
            "",
            "[compat.cursor]",
            "skills = false",
            "rules = false",
            "agents = false",
            "mcps = false",
            "hooks = false",
            "",
            "[compat.claude]",
            "skills = false",
            "rules = false",
            "agents = false",
            "mcps = false",
            "hooks = false",
            "",
        ]
        for resolved in resources.mcp_servers:
            server = resolved.server
            if resolved.name == CUA_DESKTOP_NAME:
                await stage_cua_desktop(sandbox, session.home)
            lines.extend([f"[mcp_servers.{_toml_key(resolved.name)}]"])
            if isinstance(server, StdioMcpServer):
                lines.append(f"command = {_toml(server.command.replace('{home}', session.home))}")
                args = [arg.replace("{home}", session.home) for arg in server.args]
                lines.append(f"args = [{', '.join(_toml(arg) for arg in args)}]")
                if server.cwd:
                    lines.append(f"cwd = {_toml(server.cwd.replace('{home}', session.home))}")
                if server.environment:
                    rendered = ", ".join(
                        f"{_toml_key(key)} = {_toml(value.replace('{home}', session.home))}"
                        for key, value in sorted(server.environment.items())
                    )
                    lines.append(f"env = {{ {rendered} }}")
            else:
                lines.append(f"url = {_toml(server.url)}")
            lines.append("")
        await sandbox.write_file(
            grok_home / "config.toml",
            ("\n".join(lines) + "\n").encode(),
            identity=Identity.AGENT,
        )

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        return await self._run(
            instruction,
            sandbox,
            session,
            native_session_id=str(uuid.uuid4()),
            resume=False,
            timeout_sec=timeout_sec,
        )

    async def resume(
        self,
        instruction: str,
        continuation: NativeContinuation,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        self._check_continuation(continuation, session)
        state = await sandbox.exec(
            [
                "sh",
                "-c",
                f"find {shlex.quote(session.home + '/.grok-ale/sessions')} -type d "
                f"-name {shlex.quote(continuation.native_session_id)} -print -quit | grep -q .",
            ],
            identity=Identity.AGENT,
        )
        if not state.ok:
            raise NativeContinuationError(
                f"native Grok session {continuation.native_session_id!r} is absent"
            )
        return await self._run(
            instruction,
            sandbox,
            session,
            native_session_id=continuation.native_session_id,
            resume=True,
            timeout_sec=timeout_sec,
        )

    async def _run(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str,
        resume: bool,
        timeout_sec: float,
    ) -> AgentRun:
        home = PurePosixPath(session.home)
        grok_home = home / ".grok-ale"
        prompt = grok_home / "prompt.txt"
        await sandbox.write_file(prompt, instruction.encode(), identity=Identity.AGENT)
        selector = (
            f"--resume {shlex.quote(native_session_id)}"
            if resume
            else f"--session-id {shlex.quote(native_session_id)}"
        )
        flags = [
            "--prompt-file",
            str(prompt),
            "--cwd",
            session.home,
            "--model",
            "ale",
            "--output-format",
            "streaming-json",
            "--sandbox",
            "off",
            "--no-plan",
            "--always-approve",
            "--no-auto-update",
            "--disallowed-tools",
            ",".join(dict.fromkeys((*_HEADLESS_DISABLED_TOOLS, *self.settings.disabled_tools))),
        ]
        if self.settings.reasoning_effort != "default":
            flags.extend(["--reasoning-effort", self.settings.reasoning_effort])
        if isinstance(self.settings.max_turns, int):
            flags.extend(["--max-turns", str(self.settings.max_turns)])
        binary = home / ".grok-build-install" / "bin" / "grok"
        segment = grok_home / "segment.jsonl"
        transcript = home / TRANSCRIPT_NAME
        command = (
            f"{shlex.quote(str(binary))} {' '.join(shlex.quote(part) for part in flags)} "
            f"{selector} > {shlex.quote(str(segment))} "
            f"2>> {shlex.quote(str(home / STDERR_NAME))}; "
            f"status=$?; cat {shlex.quote(str(segment))} "
            f">> {shlex.quote(str(transcript))}; exit $status"
        )
        env = {
            **npm_env(session.home),
            "GROK_HOME": str(grok_home),
            "GROK_MANAGED_BY_NPM": "1",
            "GROK_SANDBOX": "off",
            "GROK_DISABLE_AUTOUPDATER": "1",
            "GROK_TELEMETRY_ENABLED": "0",
            "GROK_CURSOR_SKILLS_ENABLED": "0",
            "GROK_CURSOR_RULES_ENABLED": "0",
            "GROK_CURSOR_AGENTS_ENABLED": "0",
            "GROK_CURSOR_MCPS_ENABLED": "0",
            "GROK_CURSOR_HOOKS_ENABLED": "0",
            "GROK_CLAUDE_SKILLS_ENABLED": "0",
            "GROK_CLAUDE_RULES_ENABLED": "0",
            "GROK_CLAUDE_AGENTS_ENABLED": "0",
            "GROK_CLAUDE_MCPS_ENABLED": "0",
            "GROK_CLAUDE_HOOKS_ENABLED": "0",
            "ALE_GATEWAY_TOKEN": session.token,
        }
        result = await sandbox.exec(
            ["bash", "-lc", command],
            cwd=session.home,
            env=env,
            timeout_sec=timeout_sec,
            identity=Identity.AGENT,
        )
        segment_output = await _read(sandbox, segment)
        confirmed = _terminal_session_id(segment_output)
        if result.exit_code != 0:
            raise AgentError((await _read(sandbox, home / STDERR_NAME))[-1000:] or "Grok failed")
        if confirmed != native_session_id:
            raise NativeContinuationError("Grok did not confirm the requested native session ID")
        await self._export_session(sandbox, session, confirmed)
        continuation = NativeContinuation(
            harness=self.name,
            native_session_id=confirmed,
            episode_id=session.episode_id,
            sandbox_id=session.sandbox_id,
            fingerprint=self._fingerprint(session),
        )
        return AgentRun(
            exit_code=0,
            final_message=_stream_final_message(segment_output),
            continuation=continuation,
        )

    async def _export_session(
        self, sandbox: Sandbox, session: HarnessSession, native_session_id: str
    ) -> None:
        root = f"{session.home}/.grok-ale/sessions"
        command = (
            f"dir=$(find {shlex.quote(root)} -type d -name "
            f"{shlex.quote(native_session_id)} -print -quit); "
            f'test -n "$dir" || exit 0; '
            f'cp "$dir/chat_history.jsonl" {shlex.quote(session.home + "/" + CHAT_NAME)} '
            "2>/dev/null || true; "
            f'cp "$dir/updates.jsonl" {shlex.quote(session.home + "/" + UPDATES_NAME)} '
            "2>/dev/null || true"
        )
        await sandbox.exec(["sh", "-c", command], identity=Identity.AGENT)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        chat = _jsonl(context.logs_dir / CHAT_NAME)
        stream = _jsonl(context.logs_dir / TRANSCRIPT_NAME)
        updates = _jsonl(context.logs_dir / UPDATES_NAME)
        results = {
            str(event.get("tool_call_id")): event
            for event in chat
            if event.get("type") == "tool_result" and event.get("tool_call_id")
        }
        errors = _tool_errors(updates)
        builder = TrajectoryBuilder(
            trajectory_id=context.trajectory_id,
            session_id=context.session_id or _terminal_session_id_from_events(stream),
            agent=AtifAgent(
                name=self.name,
                version=context.agent_version,
                model_name=context.model or None,
            ),
            extra=_parse_extra(context, chat, stream),
        )
        has_native_prompts = any(
            event.get("type") == "user" and isinstance(event.get("prompt_index"), int)
            for event in chat
        )
        if not has_native_prompts:
            builder.add(source="user", message=context.instruction)
        reasoning: list[str] = []
        for event in chat:
            if event.get("type") == "user" and isinstance(event.get("prompt_index"), int):
                builder.add(source="user", message=_user_prompt(event.get("content")))
                continue
            if event.get("type") == "reasoning":
                reasoning.extend(
                    str(item.get("text") or "")
                    for item in event.get("summary") or ()
                    if isinstance(item, dict)
                )
                continue
            if event.get("type") != "assistant":
                continue
            calls: list[AtifToolCall] = []
            observations: list[AtifObservationResult] = []
            for raw_call in event.get("tool_calls") or ():
                if not isinstance(raw_call, dict):
                    continue
                call_id = str(raw_call.get("id") or "")
                arguments = _arguments(raw_call.get("arguments"))
                name = str(raw_call.get("name") or "")
                if name == "use_tool" and isinstance(arguments.get("tool_name"), str):
                    name = arguments.pop("tool_name")
                    arguments = arguments.get("tool_input") or {}
                calls.append(
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name=name,
                        arguments=arguments,
                        extra=_mcp_extra(name),
                    )
                )
                if call_id in results:
                    observations.append(
                        AtifObservationResult(
                            source_call_id=call_id,
                            content=_content(results[call_id].get("content"), context),
                            extra=(
                                {"ale": {"is_error": True}} if errors.get(call_id, False) else None
                            ),
                        )
                    )
            message = _message(event.get("content"))
            if message or reasoning or calls:
                builder.add(
                    source="agent",
                    message=message or "",
                    reasoning_content="\n".join(reasoning).strip() or None,
                    tool_calls=calls or None,
                    observation=AtifObservation(results=observations) if observations else None,
                )
                reasoning.clear()
        if not any(step.source == "agent" for step in builder.steps):
            message = _stream_final_message_from_events(stream) or context.final_message
            if message is not None:
                builder.add(source="agent", message=message)
        return builder.build()

    def _fingerprint(self, session: HarnessSession) -> str:
        return continuation_fingerprint(
            harness=self.name,
            model=session.model,
            settings={
                **self.settings.model_dump(mode="json"),
                "gateway_dialect": self.gateway_dialect,
            },
            resources_digest=session.resources_digest,
        )

    def _check_continuation(
        self, continuation: NativeContinuation, session: HarnessSession
    ) -> None:
        if continuation.harness != self.name or continuation.episode_id != session.episode_id:
            raise NativeContinuationError("continuation belongs to a different harness or episode")
        if not session.sandbox_id or continuation.sandbox_id != session.sandbox_id:
            raise NativeContinuationError("continuation requires the original live sandbox")
        if continuation.fingerprint != self._fingerprint(session):
            raise NativeContinuationError(
                "model, settings, or effective resources changed since launch"
            )


def _toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_key(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _version(output: str) -> str | None:
    match = re.search(r"\d+(?:\.\d+)+", output)
    return match.group(0) if match else None


async def _read(sandbox: Sandbox, path: PurePosixPath) -> str:
    try:
        return (await sandbox.read_file(path)).decode("utf-8", "replace")
    except Exception:
        return ""


def _jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _terminal_session_id(text: str) -> str | None:
    return _terminal_session_id_from_events(
        [event for line in text.splitlines() if (event := _json(line)) is not None]
    )


def _terminal_session_id_from_events(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        value = event.get("sessionId")
        if event.get("type") in {"end", "error"} and isinstance(value, str):
            return value
    return None


def _stream_final_message(text: str) -> str | None:
    return _stream_final_message_from_events(
        [event for line in text.splitlines() if (event := _json(line)) is not None]
    )


def _stream_final_message_from_events(events: list[dict[str, Any]]) -> str | None:
    text = "".join(
        str(event.get("data") or "") for event in events if event.get("type") == "text"
    ).strip()
    return text or None


def _json(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _tool_errors(events: list[dict[str, Any]]) -> dict[str, bool]:
    errors: dict[str, bool] = {}
    for event in events:
        update = (event.get("params") or {}).get("update") or {}
        if update.get("sessionUpdate") == "tool_call_update":
            call_id = update.get("toolCallId")
            if isinstance(call_id, str):
                errors[call_id] = str(update.get("status") or "").lower() in {
                    "failed",
                    "error",
                    "cancelled",
                }
    return errors


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {"value": value}


def _message(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        )
    return ""


def _user_prompt(value: Any) -> str:
    text = _message(value)
    match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
    return match.group(1) if match else text


def _content(value: Any, context: TrajectoryParseContext) -> str | list[AtifContentPart]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    parts: list[AtifContentPart] = []
    cursor = 0
    for match in _DATA_URL.finditer(text):
        if match.start() > cursor:
            parts.append(AtifContentPart(type="text", text=text[cursor : match.start()]))
        blob = context.blobs.put(base64.b64decode(match.group(2)), media_type=match.group(1))
        parts.append(
            AtifContentPart(
                type="image",
                source=AtifImageSource(media_type=match.group(1), path=blob.path),
            )
        )
        cursor = match.end()
    if not parts:
        return text
    if cursor < len(text):
        parts.append(AtifContentPart(type="text", text=text[cursor:]))
    return parts


def _mcp_extra(name: str) -> dict[str, Any] | None:
    logical = name.removeprefix("mcp__")
    if "__" not in logical:
        return None
    server, tool = logical.split("__", 1)
    return {"ale": {"mcp": {"server": server, "tool": tool}}}


def _parse_extra(
    context: TrajectoryParseContext,
    chat: list[dict[str, Any]],
    stream: list[dict[str, Any]],
) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    if not chat and not stream:
        data["parse_issues"] = [{"reason": "no_native_events"}]
    return {"ale": data} if data else None
