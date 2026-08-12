"""Official upstream OpenAI Codex CLI as an autonomous harness."""

from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

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
    AtifFinalMetrics,
    AtifImageSource,
    AtifObservation,
    AtifObservationResult,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from ale.run.agent_resources import continuation_fingerprint
from ale.run.harnesses._npm import ensure_npm_cli, npm_env
from ale.run.subscription import classify_subscription_error
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

__all__ = ["CodexCliHarness", "CodexCliSettings"]

DEFAULT_CLI_VERSION = "0.146.0"
TRANSCRIPT_NAME = "transcript.jsonl"
SESSION_NAME = "session.jsonl"
STDERR_NAME = "stderr.log"


class CodexCliSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning_effort: Literal[
        "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "default"
    web_search: bool = False


class CodexCliHarness(AutonomousHarness):
    name = "codex-cli"
    resume_support = ResumeSupport.NATIVE
    logs = (TRANSCRIPT_NAME, SESSION_NAME, STDERR_NAME)
    __slots__ = ("cli_version", "settings")

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        settings: CodexCliSettings | dict[str, Any] | None = None,
    ) -> None:
        self.cli_version = cli_version
        try:
            self.settings = (
                settings
                if isinstance(settings, CodexCliSettings)
                else CodexCliSettings.model_validate(settings or {})
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid codex-cli settings: {exc}") from exc

    def version(self) -> str:
        return self.cli_version or DEFAULT_CLI_VERSION

    def integrity(self) -> str:
        return f"npm:@openai/codex@{self.version()}"

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        for resolved in resources.mcp_servers:
            if not isinstance(resolved.server, StdioMcpServer | StreamableHttpMcpServer):
                raise ConfigError(f"unsupported MCP transport for {resolved.name}")

    async def install(self, sandbox: Sandbox) -> str:
        return await ensure_npm_cli(
            sandbox,
            package="@openai/codex",
            binary="codex",
            version=self.version(),
        )

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        codex_home = PurePosixPath(session.home) / ".codex-ale"
        skills_root = codex_home / "skills"
        await sandbox.exec(["mkdir", "-p", str(skills_root)], identity=Identity.AGENT)
        for skill in resources.skills:
            target = skills_root / skill.name
            await sandbox.upload_dir(str(skill.path), target, identity=Identity.AGENT)
            if skill.executable_files:
                await sandbox.exec(
                    ["chmod", "+x", *(str(target / path) for path in skill.executable_files)],
                    identity=Identity.AGENT,
                )

        lines = [f"model = {json.dumps(session.model)}"]
        if session.authentication == "subscription":
            lines.extend(
                [
                    'forced_login_method = "chatgpt"',
                    'cli_auth_credentials_store = "file"',
                    "check_for_update_on_startup = false",
                ]
            )
        else:
            lines.insert(0, 'model_provider = "ale"')
        lines.extend(['approval_policy = "never"', 'sandbox_mode = "danger-full-access"'])
        if self.settings.reasoning_effort != "default":
            lines.append(f"model_reasoning_effort = {json.dumps(self.settings.reasoning_effort)}")
        if session.authentication == "subscription":
            lines.append("")
        else:
            lines.extend(
                [
                    "",
                    "[model_providers.ale]",
                    'name = "ALE Gateway"',
                    f"base_url = {json.dumps(session.gateway_url + '/v1')}",
                    'env_key = "ALE_GATEWAY_TOKEN"',
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
            lines.append("")
        await sandbox.write_file(
            codex_home / "config.toml",
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
            native_session_id=None,
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
                f"find {shlex.quote(session.home + '/.codex-ale/sessions')} -type f "
                f"-name '*.jsonl' -exec grep -l -F "
                f"{shlex.quote(_session_id_marker(continuation.native_session_id))} "
                "{} + | grep -q .",
            ],
            identity=Identity.AGENT,
        )
        if not state.ok:
            raise NativeContinuationError(
                f"native Codex thread {continuation.native_session_id!r} is absent"
            )
        return await self._run(
            instruction,
            sandbox,
            session,
            native_session_id=continuation.native_session_id,
            timeout_sec=timeout_sec,
        )

    async def _run(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        native_session_id: str | None,
        timeout_sec: float,
    ) -> AgentRun:
        home = PurePosixPath(session.home)
        prompt_var = f"ALE_PROMPT_{native_session_id or 'NEW'}".replace("-", "_")
        argv = [
            "codex",
            "exec",
            "--json",
            "--model",
            session.model,
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
        if self.settings.web_search:
            argv.append("--search")
        if native_session_id is not None:
            argv.extend(["resume", native_session_id])
        argv.append("-")
        redirect = ">>" if native_session_id is not None else ">"
        clear_api_auth = ""
        if session.authentication == "subscription":
            clear_api_auth = (
                "unset OPENAI_API_KEY OPENAI_BASE_URL CODEX_ACCESS_TOKEN ALE_GATEWAY_TOKEN; "
            )
        command = (
            f'prompt="${{{prompt_var}}}"; unset {prompt_var}; '
            f"{clear_api_auth}"
            f'printf "%s" "$prompt" | {" ".join(shlex.quote(part) for part in argv)} '
            f"{redirect} {shlex.quote(str(home / TRANSCRIPT_NAME))} "
            f"2>> {shlex.quote(str(home / STDERR_NAME))}"
        )
        env = {
            **npm_env(session.home),
            "CODEX_HOME": str(home / ".codex-ale"),
            "NO_COLOR": "1",
            prompt_var: instruction,
        }
        if session.authentication == "api-key":
            env["ALE_GATEWAY_TOKEN"] = session.token
        result = await sandbox.exec(
            ["bash", "-lc", command],
            cwd=session.home,
            env=env,
            timeout_sec=timeout_sec,
            identity=Identity.AGENT,
        )
        transcript = await _read(sandbox, home / TRANSCRIPT_NAME)
        if result.exit_code != 0:
            detail = await _read(sandbox, home / STDERR_NAME)
            if session.authentication == "subscription":
                raise classify_subscription_error(self.name, detail or "Codex failed")
            raise AgentError(detail[-1000:] or "Codex failed")
        confirmed = _thread_id(transcript)
        if confirmed is None:
            raise NativeContinuationError("Codex did not report a native thread ID")
        if native_session_id is not None and confirmed != native_session_id:
            raise NativeContinuationError(
                "Codex started a different thread instead of resuming the requested ID"
            )
        exported = await sandbox.exec(
            [
                "sh",
                "-c",
                f"file=$(find {shlex.quote(session.home + '/.codex-ale/sessions')} "
                "-type f -name '*.jsonl' -exec grep -l -m 1 -F "
                f"{shlex.quote(_session_id_marker(confirmed))} {{}} + | head -n 1); "
                f'test -n "$file" && cp "$file" {shlex.quote(str(home / SESSION_NAME))}',
            ],
            identity=Identity.AGENT,
        )
        if not exported.ok:
            raise NativeContinuationError("Codex native session transcript is absent")
        continuation = NativeContinuation(
            harness=self.name,
            native_session_id=confirmed,
            episode_id=session.episode_id,
            sandbox_id=session.sandbox_id,
            fingerprint=self._fingerprint(session),
        )
        return AgentRun(
            exit_code=0,
            final_message=_final_message(transcript),
            continuation=continuation,
        )

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        events, issues = _events(context.logs_dir / TRANSCRIPT_NAME)
        session_path = context.logs_dir / SESSION_NAME
        session_events, session_issues = (
            _events(session_path) if session_path.is_file() else ([], [])
        )
        expected_session_id = context.session_id or _thread_id_from_events(events)
        actual_session_id = _session_id_from_events(session_events)
        if actual_session_id and expected_session_id and actual_session_id != expected_session_id:
            session_issues.append(
                {
                    "reason": "session_id_mismatch",
                    "expected": expected_session_id,
                    "actual": actual_session_id,
                }
            )
            session_events = []
        issues.extend(
            {"source": SESSION_NAME, **issue}
            for issue in session_issues
            if issue.get("reason") != "no_native_log"
        )
        native_calls = _session_tool_calls(session_events, events, context)
        builder = TrajectoryBuilder(
            trajectory_id=context.trajectory_id,
            session_id=context.session_id or _thread_id_from_events(events),
            agent=AtifAgent(
                name=self.name,
                version=context.agent_version,
                model_name=context.model or None,
            ),
            extra=_extra(context, issues),
        )
        builder.add(source="user", message=context.instruction)
        usage: dict[str, int] = {}
        final_message = ""
        for event in events:
            kind = event.get("type")
            if kind == "turn.completed":
                raw = event.get("usage")
                if isinstance(raw, dict):
                    usage = {
                        key: int(value or 0) for key, value in raw.items() if isinstance(value, int)
                    }
                continue
            if kind == "error":
                builder.add(source="system", message=str(event.get("message") or event))
                continue
            if kind != "item.completed":
                continue
            item = event.get("item") or {}
            item_type = item.get("type")
            item_id = str(item.get("id") or "")
            if item_type == "agent_message":
                final_message = str(item.get("text") or "")
                if not native_calls:
                    builder.add(source="agent", message=final_message)
            elif item_type == "reasoning":
                if not native_calls:
                    builder.add(
                        source="agent",
                        message="",
                        reasoning_content=str(item.get("text") or ""),
                    )
            elif item_type == "command_execution":
                if native_calls:
                    continue
                call = AtifToolCall(
                    tool_call_id=item_id,
                    function_name="shell",
                    arguments={"command": str(item.get("command") or "")},
                )
                builder.add(
                    source="agent",
                    message="",
                    tool_calls=[call],
                    observation=AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id=item_id,
                                content=str(item.get("aggregated_output") or ""),
                                extra=(
                                    {"ale": {"is_error": True}}
                                    if int(item.get("exit_code") or 0) != 0
                                    else None
                                ),
                            )
                        ]
                    ),
                )
            elif item_type == "mcp_tool_call":
                if native_calls:
                    continue
                server = str(item.get("server") or "")
                tool = str(item.get("tool") or "")
                call = AtifToolCall(
                    tool_call_id=item_id,
                    function_name=f"mcp__{server}__{tool}",
                    arguments=_dict(item.get("arguments")),
                    extra={"ale": {"mcp": {"server": server, "tool": tool}}},
                )
                result = item.get("result")
                error = item.get("error")
                builder.add(
                    source="agent",
                    message="",
                    tool_calls=[call],
                    observation=(
                        AtifObservation(
                            results=[
                                AtifObservationResult(
                                    source_call_id=item_id,
                                    content=_result_content(result or error or "", context),
                                    extra=(
                                        {"ale": {"is_error": True}} if error is not None else None
                                    ),
                                )
                            ]
                        )
                        if result is not None or error is not None
                        else None
                    ),
                )
            elif item_type == "file_change":
                if native_calls:
                    continue
                builder.add(
                    source="agent",
                    message="",
                    tool_calls=[
                        AtifToolCall(
                            tool_call_id=item_id,
                            function_name="apply_patch",
                            arguments={"changes": item.get("changes") or []},
                        )
                    ],
                    observation=AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id=item_id,
                                content=str(item.get("status") or "completed"),
                            )
                        ]
                    ),
                )
            elif item_type == "web_search":
                if not any(call.function_name == "web.run" for call, _ in native_calls):
                    builder.add(
                        source="agent",
                        message="",
                        tool_calls=[
                            AtifToolCall(
                                tool_call_id=item_id,
                                function_name="web.run",
                                arguments={"query": str(item.get("query") or "")},
                            )
                        ],
                        observation=AtifObservation(
                            results=[
                                AtifObservationResult(
                                    source_call_id=item_id,
                                    content=json.dumps(
                                        {
                                            "status": item.get("status") or "completed",
                                            "action": item.get("action"),
                                        },
                                        ensure_ascii=False,
                                        default=str,
                                    ),
                                )
                            ]
                        ),
                    )
        for call, observation in native_calls:
            builder.add(
                source="agent",
                message="",
                tool_calls=[call],
                observation=observation,
            )
        if native_calls and final_message:
            builder.add(source="agent", message=final_message)
        if not any(step.source == "agent" for step in builder.steps) and context.final_message:
            builder.add(source="agent", message=context.final_message)
        cached = usage.get("cached_input_tokens") or (
            usage.get("input_tokens_details_cached_tokens")
        )
        final_metrics = (
            AtifFinalMetrics(
                total_prompt_tokens=usage.get("input_tokens"),
                total_completion_tokens=usage.get("output_tokens"),
                total_cached_tokens=cached,
                total_steps=len(builder.steps),
            )
            if usage
            else None
        )
        return builder.build(final_metrics=final_metrics)

    def _fingerprint(self, session: HarnessSession) -> str:
        return continuation_fingerprint(
            harness=self.name,
            model=session.model,
            settings=self.settings.model_dump(mode="json"),
            resources_digest=session.resources_digest,
            authentication=session.authentication,
            profile_slot_id=session.profile_slot_id,
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


async def _read(sandbox: Sandbox, path: PurePosixPath) -> str:
    try:
        return (await sandbox.read_file(path)).decode("utf-8", "replace")
    except Exception:
        return ""


def _events(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    if not path.is_file():
        return events, [{"reason": "no_native_log"}]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for number, line in enumerate(lines, 1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append({"line": number, "reason": "malformed_json", "detail": str(exc)[:200]})
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, issues


def _thread_id(text: str) -> str | None:
    events = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return _thread_id_from_events(events)


def _thread_id_from_events(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


def _session_id_from_events(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "session_meta" and isinstance(payload.get("id"), str):
            return payload["id"]
    return None


def _session_id_marker(session_id: str) -> str:
    return f'"id":{json.dumps(session_id)}'


def _final_message(text: str) -> str | None:
    final = None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            final = str(item.get("text") or "")
    return final


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {"value": value}


def _result_content(value: Any, context: TrajectoryParseContext) -> str | list[AtifContentPart]:
    if not isinstance(value, dict):
        return str(value)
    parts: list[AtifContentPart] = []
    for block in value.get("content") or ():
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(AtifContentPart(type="text", text=str(block.get("text") or "")))
        elif block.get("type") == "image" and isinstance(block.get("data"), str):
            media_type = str(block.get("mimeType") or "image/png")
            blob = context.blobs.put(base64.b64decode(block["data"]), media_type=media_type)
            parts.append(
                AtifContentPart(
                    type="image",
                    source=AtifImageSource(media_type=media_type, path=blob.path),
                )
            )
    return parts or json.dumps(value, ensure_ascii=False, default=str)


def _session_tool_calls(
    session_events: list[dict[str, Any]],
    transcript_events: list[dict[str, Any]],
    context: TrajectoryParseContext,
) -> list[tuple[AtifToolCall, AtifObservation | None]]:
    outputs: dict[str, Any] = {}
    for event in session_events:
        payload = event.get("payload") or {}
        if event.get("type") != "response_item":
            continue
        if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            outputs[str(payload.get("call_id") or "")] = payload.get("output")
        elif payload.get("type") == "tool_search_output":
            outputs[str(payload.get("call_id") or "")] = {
                "status": payload.get("status"),
                "tools": payload.get("tools") or [],
            }
    transcript_mcp = [
        event.get("item") or {}
        for event in transcript_events
        if event.get("type") == "item.completed"
        if (event.get("item") or {}).get("type") == "mcp_tool_call"
    ]
    used_mcp: set[int] = set()
    calls: list[tuple[AtifToolCall, AtifObservation | None]] = []
    for event in session_events:
        payload = event.get("payload") or {}
        if event.get("type") != "response_item":
            continue
        if payload.get("type") == "web_search_call":
            call_id = str(payload.get("id") or "")
            calls.append(
                (
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name="web.run",
                        arguments=_dict(payload.get("action")),
                    ),
                    AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id=call_id,
                                content=json.dumps(
                                    {
                                        "status": payload.get("status") or "completed",
                                        "action": payload.get("action"),
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                ),
                            )
                        ]
                    ),
                )
            )
            continue
        if payload.get("type") == "tool_search_call":
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            calls.append(
                (
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name="tool_search_tool",
                        arguments=_dict(payload.get("arguments")),
                    ),
                    AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id=call_id,
                                content=_session_result_content(outputs.get(call_id), context),
                            )
                        ]
                    )
                    if call_id in outputs
                    else None,
                )
            )
            continue
        if payload.get("type") == "custom_tool_call":
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            calls.append(
                (
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name=str(payload.get("name") or "custom_tool"),
                        arguments={"input": payload.get("input")},
                    ),
                    AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id=call_id,
                                content=_session_result_content(outputs.get(call_id), context),
                            )
                        ]
                    )
                    if call_id in outputs
                    else None,
                )
            )
            continue
        if payload.get("type") != "function_call":
            continue
        call_id = str(payload.get("call_id") or payload.get("id") or "")
        name = str(payload.get("name") or "")
        namespace = str(payload.get("namespace") or "")
        arguments = _dict(payload.get("arguments"))
        extra = None
        if namespace.startswith("mcp__"):
            match = next(
                (
                    (index, item)
                    for index, item in enumerate(transcript_mcp)
                    if index not in used_mcp
                    and item.get("tool") == name
                    and _dict(item.get("arguments")) == arguments
                ),
                None,
            )
            if match is not None:
                index, item = match
                used_mcp.add(index)
                server = str(item.get("server") or namespace.removeprefix("mcp__"))
                result = item.get("result")
                error = item.get("error")
                output = result if result is not None else error
            else:
                server = namespace.removeprefix("mcp__")
                error = None
                output = outputs.get(call_id)
            function_name = f"mcp__{server}__{name}"
            extra = {"ale": {"mcp": {"server": server, "tool": name}}}
        else:
            function_name = (
                f"multi_agent.{name}"
                if namespace == "multi_agent_v1"
                else f"{namespace}.{name}"
                if namespace
                else name
            )
            error = None
            output = outputs.get(call_id)
        observation = (
            AtifObservation(
                results=[
                    AtifObservationResult(
                        source_call_id=call_id,
                        content=_session_result_content(output, context),
                        extra={"ale": {"is_error": True}} if error is not None else None,
                    )
                ]
            )
            if output is not None
            else None
        )
        calls.append(
            (
                AtifToolCall(
                    tool_call_id=call_id,
                    function_name=function_name,
                    arguments=arguments,
                    extra=extra,
                ),
                observation,
            )
        )
    return calls


def _session_result_content(
    value: Any, context: TrajectoryParseContext
) -> str | list[AtifContentPart]:
    if not isinstance(value, list):
        return _result_content(value, context)
    parts = [
        AtifContentPart(type="text", text=str(block.get("text") or ""))
        for block in value
        if isinstance(block, dict) and block.get("type") in {"input_text", "output_text", "text"}
    ]
    return parts or json.dumps(value, ensure_ascii=False, default=str)


def _extra(context: TrajectoryParseContext, issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"ale": data} if data else None
