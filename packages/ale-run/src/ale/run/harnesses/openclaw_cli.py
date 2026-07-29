"""Official upstream OpenClaw CLI as an autonomous harness."""

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
    AtifFinalMetrics,
    AtifImageSource,
    AtifMetrics,
    AtifObservation,
    AtifObservationResult,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from ale.run.agent_resources import continuation_fingerprint
from ale.run.harnesses._npm import ensure_npm_cli, npm_env
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

__all__ = ["OpenClawCliHarness", "OpenClawCliSettings"]

DEFAULT_CLI_VERSION = "2026.7.1"
TRANSCRIPT_NAME = "transcript.jsonl"
RESULT_NAME = "result.json"
STDERR_NAME = "stderr.log"
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
_PROVIDER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class OpenClawCliSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = "openai"
    thinking: Literal[
        "default", "off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"
    ] = "default"
    timeout_seconds: PositiveInt | Literal["default", "unlimited"] = "unlimited"
    tool_profile: Literal["minimal", "coding", "messaging", "full"] = "coding"
    tools_allow: tuple[str, ...] = ()
    tools_deny: tuple[str, ...] = ()
    model_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _provider_id(cls, value: str) -> str:
        if not _PROVIDER.fullmatch(value):
            raise ValueError("provider must be an OpenClaw provider identifier")
        return value

    @field_validator("tools_allow", "tools_deny")
    @classmethod
    def _nonempty_tools(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("tool names must be non-empty")
        return values


class OpenClawCliHarness(AutonomousHarness):
    name = "openclaw-cli"
    resume_support = ResumeSupport.NATIVE
    logs = (TRANSCRIPT_NAME, RESULT_NAME, STDERR_NAME)
    __slots__ = ("cli_version", "settings")

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        settings: OpenClawCliSettings | dict[str, Any] | None = None,
    ) -> None:
        self.cli_version = cli_version
        try:
            self.settings = (
                settings
                if isinstance(settings, OpenClawCliSettings)
                else OpenClawCliSettings.model_validate(settings or {})
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid openclaw-cli settings: {exc}") from exc
        if self.settings.tools_allow and self.settings.tool_profile != "full":
            raise ConfigError("OpenClaw tools_allow requires tool_profile='full'")

    def version(self) -> str:
        return self.cli_version or DEFAULT_CLI_VERSION

    def integrity(self) -> str:
        return f"npm:openclaw@{self.version()}"

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        for resolved in resources.mcp_servers:
            if not isinstance(resolved.server, StdioMcpServer | StreamableHttpMcpServer):
                raise ConfigError(f"unsupported MCP transport for {resolved.name}")

    async def install(self, sandbox: Sandbox) -> str:
        node = await sandbox.exec(["node", "--version"], timeout_sec=30, identity=Identity.AGENT)
        if not node.ok or not _supported_node(node.stdout):
            raise AgentError(
                "OpenClaw 2026.7.1 requires Node >=22.22.3; rebuild the ALE base image"
            )
        return await ensure_npm_cli(
            sandbox,
            package="openclaw",
            binary="openclaw",
            version=self.version(),
        )

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        state = PurePosixPath(session.home) / ".openclaw-ale"
        workspace = state / "workspace"
        skills_root = workspace / "skills"
        await sandbox.exec(
            ["mkdir", "-p", str(skills_root), str(state / "agents/main/sessions")],
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
                "pricing": {"enabled": False},
                "providers": {
                    self.settings.provider: {
                        "baseUrl": session.gateway_url + "/v1",
                        "apiKey": "${ALE_GATEWAY_TOKEN}",
                        "api": "openai-responses",
                        "models": [
                            {
                                "id": session.model,
                                "name": session.model,
                                "input": ["text", "image"],
                                "agentRuntime": {"id": "openclaw"},
                            }
                        ],
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
        transcript = (
            PurePosixPath(session.home)
            / ".openclaw-ale"
            / "agents"
            / "main"
            / "sessions"
            / f"{continuation.native_session_id}.jsonl"
        )
        state = await sandbox.exec(["test", "-f", str(transcript)], identity=Identity.AGENT)
        if not state.ok:
            raise NativeContinuationError(
                f"native OpenClaw session {continuation.native_session_id!r} is absent"
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
        native_session_id: str,
        timeout_sec: float,
    ) -> AgentRun:
        home = PurePosixPath(session.home)
        state = home / ".openclaw-ale"
        prompt = state / "prompt.txt"
        await sandbox.write_file(prompt, instruction.encode(), identity=Identity.AGENT)
        model_ref = f"{self.settings.provider}/{session.model}"
        argv = [
            "openclaw",
            "agent",
            "--local",
            "--agent",
            "main",
            "--session-id",
            native_session_id,
            "--message-file",
            str(prompt),
            "--model",
            model_ref,
            "--json",
        ]
        if self.settings.thinking != "default":
            argv.extend(["--thinking", self.settings.thinking])
        if self.settings.timeout_seconds == "unlimited":
            argv.extend(["--timeout", "0"])
        elif isinstance(self.settings.timeout_seconds, int):
            argv.extend(["--timeout", str(self.settings.timeout_seconds)])
        command = (
            f"{' '.join(shlex.quote(part) for part in argv)} "
            f"> {shlex.quote(str(home / RESULT_NAME))} "
            f"2>> {shlex.quote(str(home / STDERR_NAME))}"
        )
        result = await sandbox.exec(
            ["bash", "-lc", command],
            cwd=session.home,
            env={
                **npm_env(session.home),
                "OPENCLAW_HOME": str(state),
                "OPENCLAW_STATE_DIR": str(state),
                "OPENCLAW_CONFIG_PATH": str(state / "openclaw.json"),
                "OPENCLAW_WORKSPACE_DIR": str(state / "workspace"),
                "ALE_GATEWAY_TOKEN": session.token,
                "NO_COLOR": "1",
            },
            timeout_sec=timeout_sec,
            identity=Identity.AGENT,
        )
        raw_result = await _read(sandbox, home / RESULT_NAME)
        envelope = _json_object(raw_result)
        if result.exit_code != 0 or envelope is None:
            detail = await _read(sandbox, home / STDERR_NAME)
            raise AgentError(detail[-1200:] or raw_result[-1200:] or "OpenClaw failed")
        confirmed = ((envelope.get("meta") or {}).get("agentMeta") or {}).get("sessionId")
        if confirmed != native_session_id:
            raise NativeContinuationError(
                "OpenClaw did not confirm the requested native session ID"
            )
        native_log = state / "agents" / "main" / "sessions" / f"{native_session_id}.jsonl"
        copied = await sandbox.exec(
            ["cp", str(native_log), str(home / TRANSCRIPT_NAME)],
            identity=Identity.AGENT,
        )
        if not copied.ok:
            raise NativeContinuationError("OpenClaw native session transcript is absent")
        continuation = NativeContinuation(
            harness=self.name,
            native_session_id=native_session_id,
            episode_id=session.episode_id,
            sandbox_id=session.sandbox_id,
            fingerprint=self._fingerprint(session),
        )
        return AgentRun(
            exit_code=0,
            final_message=_envelope_message(envelope),
            continuation=continuation,
        )

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        events, issues = _events(context.logs_dir / TRANSCRIPT_NAME)
        results = _results(events, context)
        builder = TrajectoryBuilder(
            trajectory_id=context.trajectory_id,
            session_id=context.session_id,
            agent=AtifAgent(
                name=self.name,
                version=context.agent_version,
                model_name=context.model or None,
            ),
            extra=_extra(context, issues),
        )
        user_seen = False
        totals = {"input": 0, "output": 0, "cacheRead": 0, "cost": 0.0}
        for event in events:
            if event.get("type") != "message":
                continue
            message = event.get("message") or {}
            role = message.get("role")
            blocks = message.get("content") or []
            if role == "user":
                text = _blocks_text(blocks)
                if text:
                    builder.add(source="user", message=text)
                    user_seen = True
                continue
            if role != "assistant":
                continue
            if not user_seen:
                builder.add(source="user", message=context.instruction)
                user_seen = True
            text: list[str] = []
            reasoning: list[str] = []
            calls: list[AtifToolCall] = []
            observations: list[AtifObservationResult] = []
            for block in blocks if isinstance(blocks, list) else ():
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    text.append(str(block.get("text") or ""))
                elif kind == "thinking":
                    reasoning.append(str(block.get("thinking") or ""))
                elif kind in {"toolCall", "tool_use"}:
                    call_id = str(block.get("id") or "")
                    name = str(block.get("name") or "")
                    calls.append(
                        AtifToolCall(
                            tool_call_id=call_id,
                            function_name=name,
                            arguments=_arguments(block.get("arguments") or block.get("input")),
                            extra=_mcp_extra(name),
                        )
                    )
                    if call_id in results:
                        observations.append(results[call_id])
            usage = message.get("usage") or {}
            cost = (usage.get("cost") or {}).get("total") if isinstance(usage, dict) else None
            for key in ("input", "output", "cacheRead"):
                totals[key] += int(usage.get(key) or 0)
            totals["cost"] += float(cost or 0.0)
            builder.add(
                source="agent",
                message="\n".join(text),
                reasoning_content="\n".join(reasoning) or None,
                tool_calls=calls or None,
                observation=AtifObservation(results=observations) if observations else None,
                metrics=(
                    AtifMetrics(
                        prompt_tokens=int(usage.get("input") or 0),
                        completion_tokens=int(usage.get("output") or 0),
                        cached_tokens=int(usage.get("cacheRead") or 0),
                        cost_usd=float(cost) if cost is not None else None,
                    )
                    if usage
                    else None
                ),
            )
        if not user_seen:
            builder.add(source="user", message=context.instruction)
        if not any(step.source == "agent" for step in builder.steps) and context.final_message:
            builder.add(source="agent", message=context.final_message)
        final_metrics = (
            AtifFinalMetrics(
                total_prompt_tokens=int(totals["input"]),
                total_completion_tokens=int(totals["output"]),
                total_cached_tokens=int(totals["cacheRead"]),
                total_cost_usd=float(totals["cost"]),
                total_steps=len(builder.steps),
            )
            if any(totals.values())
            else None
        )
        return builder.build(final_metrics=final_metrics)

    def _fingerprint(self, session: HarnessSession) -> str:
        return continuation_fingerprint(
            harness=self.name,
            model=session.model,
            settings=self.settings.model_dump(mode="json"),
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


async def _read(sandbox: Sandbox, path: PurePosixPath) -> str:
    try:
        return (await sandbox.read_file(path)).decode("utf-8", "replace")
    except Exception:
        return ""


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _envelope_message(envelope: dict[str, Any]) -> str | None:
    text = "\n".join(
        str(payload.get("text") or "")
        for payload in envelope.get("payloads") or ()
        if isinstance(payload, dict)
    ).strip()
    return text or None


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


def _results(
    events: list[dict[str, Any]], context: TrajectoryParseContext
) -> dict[str, AtifObservationResult]:
    results: dict[str, AtifObservationResult] = {}
    for event in events:
        if event.get("type") == "tool_result":
            call_id = str(event.get("call_id") or event.get("tool_use_id") or "")
            value = event.get("output") or ""
            error = bool(event.get("is_error"))
        elif event.get("type") == "message":
            message = event.get("message") or {}
            role = message.get("role")
            if role not in {"user", "toolResult"}:
                continue
            for block in message.get("content") or ():
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = str(block.get("tool_use_id") or block.get("call_id") or "")
                results[call_id] = AtifObservationResult(
                    source_call_id=call_id,
                    content=_block_content(block.get("content"), context),
                    extra={"ale": {"is_error": True}} if block.get("is_error") else None,
                )
            if role != "toolResult":
                continue
            call_id = str(message.get("toolCallId") or "")
            value = message.get("content") or event.get("output") or ""
            error = bool(message.get("isError"))
        else:
            continue
        if call_id:
            results[call_id] = AtifObservationResult(
                source_call_id=call_id,
                content=_block_content(value, context),
                extra={"ale": {"is_error": True}} if error else None,
            )
    return results


def _block_content(value: Any, context: TrajectoryParseContext) -> str | list[AtifContentPart]:
    if isinstance(value, str):
        return value
    parts: list[AtifContentPart] = []
    for block in value if isinstance(value, list) else ():
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


def _blocks_text(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks.strip()
    return "\n".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(blocks, list)
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {"value": value}


def _mcp_extra(name: str) -> dict[str, Any] | None:
    logical = name.removeprefix("mcp__")
    if "__" not in logical:
        return None
    server, tool = logical.split("__", 1)
    return {"ale": {"mcp": {"server": server, "tool": tool}}}


def _extra(context: TrajectoryParseContext, issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"ale": data} if data else None
