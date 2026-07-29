"""Claude Code as an autonomous harness.

The agent runs inside the sandbox and drives itself; we hand it a prompt, point its SDK
at the gateway, and read what it left behind. Pointing it at the gateway needs no change
to the agent: it already honours ``ANTHROPIC_BASE_URL``, which is why credentials can
stay on the host and ceilings can bind an agent that knows nothing about them.

The flag and environment tables are declarative so the same descriptors serve three
purposes — building the command line, documenting what is configurable, and validating
what a run asked for. Harbor's integration is the reference for that shape.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ale.core.blob import InlineText
from ale.core.errors import (
    AgentError,
    AgentRefusalError,
    BudgetExceededError,
    ConfigError,
    HarnessLimitError,
    NativeContinuationError,
    TrajectoryReferenceError,
)
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
from ale.core.trace import DesktopAction
from ale.core.trajectory import (
    AtifAgent,
    AtifContentPart,
    AtifImageSource,
    AtifObservation,
    AtifObservationResult,
    AtifSubagentTrajectoryRef,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from ale.run.agent_resources import continuation_fingerprint
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop

__all__ = ["ClaudeCodeHarness", "ClaudeCodeSettings"]

#: File names inside whatever workspace the session supplies. The directory itself is
#: not ours to choose: the agent runs unprivileged and can only write what it owns.
#: Both streams go here. Interleaved on purpose: when the CLI fails, the reason is often
#: the last thing it printed before the stream stopped, and two files lose that ordering.
TRANSCRIPT_NAME = "transcript.jsonl"

#: The version a run gets unless it asks for another. A pin rather than "latest", because
#: an unpinned agent makes two runs incomparable for a reason that never appears in the
#: result. Bumping this is a deliberate, reviewable change; images need not be rebuilt for
#: it, since a mismatch installs the pinned build at the start of the episode.
DEFAULT_CLI_VERSION = "2.1.220"

#: Where a version this image did not bake gets installed, relative to the agent's home —
#: the account that runs it owns it, so no privilege is needed and none is granted.
CLI_PREFIX = ".local"


PositiveInt = Annotated[int, Field(gt=0, strict=True)]
PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ClaudeCodeSettings(BaseModel):
    """The complete ALE-supported Claude Code configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: PositiveInt | Literal["default", "unlimited"] = "unlimited"
    max_budget_usd: PositiveFloat | Literal["default", "unlimited"] = "unlimited"
    permission_mode: Literal[
        "default",
        "manual",
        "acceptEdits",
        "plan",
        "auto",
        "dontAsk",
        "bypassPermissions",
    ] = "bypassPermissions"
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    append_system_prompt: str | Literal["default"] = "default"
    effort: Literal["default", "low", "medium", "high", "xhigh", "max", "ultracode"] = "default"

    @field_validator("allowed_tools", "disallowed_tools")
    @classmethod
    def _nonempty_tools(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("tool names must be non-empty")
        return values

    @field_validator("append_system_prompt")
    @classmethod
    def _nonempty_prompt(cls, value: str) -> str:
        if not value:
            raise ValueError("append_system_prompt must be non-empty or 'default'")
        return value


#: Error signatures worth naming, so a failure is attributed rather than lumped into
#: "the agent exited non-zero".
ERROR_PATTERNS: tuple[tuple[str, type[Exception]], ...] = (
    ("rate_limit", AgentError),
    ("I can't help", AgentRefusalError),
    ("I cannot help", AgentRefusalError),
)


class ClaudeCodeHarness(AutonomousHarness):
    """Runs the Claude Code CLI inside the sandbox."""

    name = "claude-code"
    resume_support = ResumeSupport.NATIVE

    #: Both streams are in here: the CLI is run with them interleaved on purpose, because
    #: when it fails the reason is usually the last thing it printed before the stream
    #: stopped, and two files lose that ordering.
    logs = (TRANSCRIPT_NAME,)
    __slots__ = ("cli_version", "settings")

    def __init__(
        self,
        *,
        cli_version: str | None = None,
        settings: ClaudeCodeSettings | dict[str, Any] | None = None,
    ) -> None:
        self.cli_version = cli_version
        try:
            self.settings = (
                settings
                if isinstance(settings, ClaudeCodeSettings)
                else ClaudeCodeSettings.model_validate(settings or {})
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid claude-code settings: {exc}") from exc

    def version(self) -> str:
        return self.cli_version or DEFAULT_CLI_VERSION

    def integrity(self) -> str:
        """What pins the binary. Always a version, never "whatever the image had"."""
        return f"npm:{self.cli_version or DEFAULT_CLI_VERSION}"

    def validate_resources(self, resources: EffectiveAgentResources) -> None:
        return None

    async def install_resources(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        resources: EffectiveAgentResources,
    ) -> None:
        root = PurePosixPath(session.home) / ".claude-config" / "skills"
        await sandbox.exec(["mkdir", "-p", str(root)], identity=Identity.AGENT)
        for skill in resources.skills:
            target = root / skill.name
            await sandbox.upload_dir(str(skill.path), target, identity=Identity.AGENT)
            if skill.executable_files:
                await sandbox.exec(
                    ["chmod", "+x", *(str(target / path) for path in skill.executable_files)],
                    identity=Identity.AGENT,
                )
        mcp_servers: dict[str, dict[str, object]] = {}
        for resolved in resources.mcp_servers:
            server = resolved.server
            if resolved.name == CUA_DESKTOP_NAME:
                await stage_cua_desktop(sandbox, session.home)
            if isinstance(server, StdioMcpServer):
                native: dict[str, object] = {
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
            elif isinstance(server, StreamableHttpMcpServer):
                native = {"type": "http", "url": server.url}
            else:  # pragma: no cover - the core discriminated union is exhaustive
                raise ConfigError(f"unsupported MCP transport for {resolved.name}")
            mcp_servers[resolved.name] = native

        config_path = PurePosixPath(session.home) / ".claude-config" / "mcp.json"
        await sandbox.write_file(
            config_path,
            json.dumps({"mcpServers": mcp_servers}, sort_keys=True).encode(),
            identity=Identity.AGENT,
        )

    async def install(self, sandbox: Sandbox) -> str:
        """Put the pinned CLI in place, whatever the image happened to ship.

        Three cases, and the middle one is the one that matters:

        * **Missing** — install it. An image is not required to bake an agent, and a run
          that fails because the image is not the one somebody assumed is a run that
          reports nothing about the model.
        * **Present but a different version** — install the pinned one anyway. This is
          what makes an experiment controlled: "claude-code" is not a version, and two
          runs a month apart against an image tagged ``latest`` are two different agents
          being compared as though they were one. It is also what lets the pin move
          without rebuilding every image.
        * **Present and already the pinned version** — do nothing, and cost no network.

        Installed under the agent's own ``~/.local`` and prepended to ``PATH`` so it wins
        over any baked copy. Prepending unconditionally rather than checking membership:
        the directory can already be on ``PATH`` but *behind* the one holding the stale
        copy, which looks identical until you read the version that actually ran.

        Follows ``agents-last-exam``'s deployer, which had already worked all of this out.
        """
        # Resolved rather than assumed: the home directory belongs to the image, which
        # declares the account but not where it lives.
        home = await sandbox.exec(["sh", "-c", "echo $HOME"], identity=Identity.AGENT)
        prefix = f"{home.stdout.strip() or '/home/user'}/{CLI_PREFIX}"
        path = f"{prefix}/bin:/usr/local/bin:/usr/bin:/bin"

        wanted = self.cli_version or DEFAULT_CLI_VERSION
        installed = await self._installed_version(sandbox, path)

        if installed != wanted:
            spec = f"@anthropic-ai/claude-code@{wanted}"
            # As the agent: it is the agent that runs this binary, and an install into
            # root's prefix is one the unprivileged account may not be able to execute.
            # `--force` so a same-version residue under the prefix is overwritten cleanly.
            result = await sandbox.exec(
                ["npm", "install", "-g", "--force", "--prefix", prefix, spec],
                env={"npm_config_cache": f"{prefix}/.npm-cache"},
                timeout_sec=900,  # a cold install on an image with no cache is minutes
                identity=Identity.AGENT,
            )
            if not result.ok:
                detail = (result.stderr or result.stdout)[-500:]
                if "EAI_AGAIN" in detail or "ENOTFOUND" in detail:
                    # The ordinary case, and worth naming: a sandbox has no egress but the
                    # gateway, so an image that does not carry the pinned build cannot
                    # obtain it. Both ways out are the operator's to choose.
                    raise AgentError(
                        f"this image has {installed or 'no claude'} and the run pinned "
                        f"{wanted}, but the sandbox has no network to install it. Either "
                        f"rebuild the image with CLAUDE_CODE_VERSION={wanted}, or run a "
                        f"task whose network policy reaches a registry."
                    )
                raise AgentError(f"could not install {spec}: {detail}")
            installed = await self._installed_version(sandbox, path)

        if installed != wanted:
            raise AgentError(
                f"the claude CLI reports {installed or 'nothing'} after installing "
                f"{wanted}; the run would not be measuring the agent it says it is"
            )
        return installed

    async def _installed_version(self, sandbox: Sandbox, path: str) -> str | None:
        """What ``claude --version`` says, or ``None`` when there is no claude."""
        # Through a shell, so "there is no claude" is an exit code rather than an
        # exception: the guest service raises when it cannot find a binary at all, and
        # the missing case is the ordinary one here, not an error.
        probe = await sandbox.exec(
            ["sh", "-c", "command -v claude >/dev/null 2>&1 && claude --version"],
            env={"PATH": path},
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        if not probe.ok or not probe.stdout.strip():
            return None
        # "2.1.220 (Claude Code)" — the first field is the version.
        return probe.stdout.strip().split()[0]

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        native_session_id = str(uuid.uuid4())
        return await self._run_native(
            instruction,
            sandbox,
            session,
            native_session_id=native_session_id,
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
        fingerprint = self._continuation_fingerprint(session)
        if continuation.harness != self.name:
            raise NativeContinuationError("continuation belongs to a different harness")
        if continuation.episode_id != session.episode_id:
            raise NativeContinuationError("continuation belongs to a different episode")
        if not session.sandbox_id or continuation.sandbox_id != session.sandbox_id:
            raise NativeContinuationError("continuation requires the original live sandbox")
        if continuation.fingerprint != fingerprint:
            raise NativeContinuationError(
                "model, settings, or effective resources changed since launch"
            )
        state = await sandbox.exec(
            [
                "sh",
                "-c",
                'find "$CLAUDE_CONFIG_DIR/projects" -type f '
                f"-name {shlex.quote(continuation.native_session_id + '.jsonl')} "
                "-print -quit | grep -q .",
            ],
            env=self._env(session),
            identity=Identity.AGENT,
        )
        if not state.ok:
            raise NativeContinuationError(
                f"native Claude session {continuation.native_session_id!r} is absent"
            )
        return await self._run_native(
            instruction,
            sandbox,
            session,
            native_session_id=continuation.native_session_id,
            resume=True,
            timeout_sec=timeout_sec,
        )

    async def _run_native(
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
        transcript_path = home / TRANSCRIPT_NAME
        env = self._env(session)

        config_path = home / ".claude-config" / "mcp.json"
        mcp_flags = f"--strict-mcp-config --mcp-config {shlex.quote(str(config_path))}"

        # The CLI expects its configuration directory to exist, with these subdirectories
        # in place. It creates neither, and the failures are opaque when they are missing.
        await sandbox.exec(
            [
                "sh",
                "-c",
                'mkdir -p "$CLAUDE_CONFIG_DIR"/debug "$CLAUDE_CONFIG_DIR"/projects '
                '"$CLAUDE_CONFIG_DIR"/shell-snapshots "$CLAUDE_CONFIG_DIR"/statsig '
                '"$CLAUDE_CONFIG_DIR"/todos',
            ],
            env=env,
            identity=Identity.AGENT,
        )

        # The instruction travels in the environment, not on the command line and not
        # through a file. A command line is visible to every process in the sandbox, and a
        # file is one more thing to place somewhere the agent can read; an environment
        # variable read into a shell variable and then unset is visible to neither.
        prompt_var = f"ALE_PROMPT_{uuid.uuid4().hex}"
        selector = (
            f"--resume {shlex.quote(native_session_id)}"
            if resume
            else f"--session-id {shlex.quote(native_session_id)}"
        )
        redirect = ">>" if resume else ">"
        command = (
            f'prompt="${{{prompt_var}}}"; unset {prompt_var}; '
            f'printf "%s" "$prompt" | '
            f"claude --verbose --output-format=stream-json {self._flags()} "
            f"{mcp_flags} {selector} --print "
            f"{redirect} {transcript_path} 2>&1"
        )

        result = await sandbox.exec(
            ["bash", "-lc", command],
            cwd=str(home),
            env={**env, prompt_var: instruction},
            timeout_sec=timeout_sec,
            # The thing being measured runs unprivileged, so it cannot change the
            # conditions of its own measurement.
            identity=Identity.AGENT,
        )

        transcript = await self._read_text(sandbox, transcript_path)
        if result.exit_code != 0:
            raise self._classify(transcript or result.stderr, result.exit_code)
        confirmed = _native_session_id(transcript)
        if confirmed != native_session_id:
            raise NativeContinuationError(
                "Claude stream did not confirm the requested native session ID"
            )
        continuation = NativeContinuation(
            harness=self.name,
            native_session_id=confirmed,
            episode_id=session.episode_id,
            sandbox_id=session.sandbox_id,
            fingerprint=self._continuation_fingerprint(session),
        )
        return AgentRun(
            exit_code=result.exit_code,
            final_message=_final_message(transcript),
            continuation=continuation,
        )

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        """Convert Claude stream-json directly into canonical ATIF v1.7."""
        transcript = context.logs_dir / TRANSCRIPT_NAME
        events: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        if transcript.is_file():
            for line_number, line in enumerate(
                transcript.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    issues.append(
                        {
                            "line": line_number,
                            "reason": "malformed_json",
                            "detail": str(exc)[:500],
                        }
                    )
                    continue
                if isinstance(event, dict):
                    events.append(event)
                else:
                    issues.append(
                        {
                            "line": line_number,
                            "reason": "unexpected_shape",
                            "detail": repr(event)[:500],
                        }
                    )

        sidechains = _sidechain_groups(events)
        events = [event for event in events if not event.get("isSidechain")]
        results: dict[str, tuple[Any, bool]] = {}
        for event in events:
            if event.get("type") != "user":
                continue
            content = (event.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool_result":
                    continue
                call_id = str(part.get("tool_use_id") or "")
                if not call_id:
                    raise TrajectoryReferenceError("Claude tool result has no tool_use_id")
                if call_id in results:
                    raise TrajectoryReferenceError(f"duplicate Claude tool result for {call_id!r}")
                results[call_id] = (
                    event.get("tool_use_result", part.get("content")),
                    part.get("is_error") is True,
                )

        root_extra: dict[str, Any] = {}
        if issues:
            root_extra["parse_issues"] = issues
        if context.incomplete:
            root_extra.update(
                {
                    "incomplete": True,
                    "incomplete_reason": context.incomplete_reason or "interrupted",
                }
            )
        builder = TrajectoryBuilder(
            trajectory_id=context.trajectory_id,
            session_id=context.session_id,
            agent=AtifAgent(
                name=self.name,
                version=context.agent_version,
                model_name=context.model or None,
            ),
            extra={"ale": root_extra} if root_extra else None,
        )
        instruction_added = False

        def ensure_instruction() -> None:
            nonlocal instruction_added
            if not instruction_added:
                builder.add(source="user", message=context.instruction)
                instruction_added = True

        known_calls: set[str] = set()
        native_session_id = context.session_id
        for event in events:
            kind = event.get("type")
            timestamp = event.get("timestamp") if isinstance(event.get("timestamp"), str) else None
            if kind == "system":
                message, attachments = _atif_content(
                    event.get("message") or event.get("content") or "",
                    context,
                )
                if message:
                    builder.add(
                        source="system",
                        message=message,
                        timestamp=timestamp,
                        extra=_step_extra(event, attachments=attachments),
                    )
                continue
            if kind == "user":
                content = (event.get("message") or {}).get("content")
                if isinstance(content, list) and any(
                    isinstance(part, dict) and part.get("type") == "tool_result" for part in content
                ):
                    continue
                message, attachments = _atif_content(content or "", context)
                if message:
                    if _content_text(message) == context.instruction:
                        if instruction_added:
                            continue
                        instruction_added = True
                    else:
                        ensure_instruction()
                    builder.add(
                        source="user",
                        message=message,
                        timestamp=timestamp,
                        extra=_step_extra(event, attachments=attachments),
                    )
                continue
            if kind == "assistant":
                ensure_instruction()
                message = event.get("message") or {}
                content = message.get("content") or []
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                text_parts: list[str] = []
                reasoning_parts: list[str] = []
                calls: list[AtifToolCall] = []
                observations: list[AtifObservationResult] = []
                attachments: list[dict[str, Any]] = []
                for part in content if isinstance(content, list) else ():
                    if not isinstance(part, dict):
                        continue
                    part_type = part.get("type")
                    if part_type == "text":
                        rendered, refs = _atif_content(str(part.get("text") or ""), context)
                        text_parts.append(rendered if isinstance(rendered, str) else "")
                        attachments.extend(refs)
                        continue
                    if part_type in {"thinking", "reasoning"}:
                        reasoning_parts.append(str(part.get("thinking") or part.get("text") or ""))
                        continue
                    if part_type != "tool_use":
                        continue
                    call_id = str(part.get("id") or "")
                    if not call_id:
                        raise TrajectoryReferenceError("Claude tool call has no id")
                    if call_id in known_calls:
                        raise TrajectoryReferenceError(f"duplicate Claude tool call {call_id!r}")
                    known_calls.add(call_id)
                    name = str(part.get("name") or "")
                    arguments = part.get("input") if isinstance(part.get("input"), dict) else {}
                    extra = _tool_extra(name, arguments)
                    calls.append(
                        AtifToolCall(
                            tool_call_id=call_id,
                            function_name=name,
                            arguments=arguments,
                            extra=extra,
                        )
                    )
                    if call_id in results:
                        value, is_error = results[call_id]
                        rendered, refs = _atif_content(value, context)
                        result_extra: dict[str, Any] = {}
                        if refs:
                            result_extra["attachments"] = refs
                        if is_error:
                            result_extra["is_error"] = True
                        observations.append(
                            AtifObservationResult(
                                source_call_id=call_id,
                                content=rendered,
                                extra={"ale": result_extra} if result_extra else None,
                            )
                        )
                builder.add(
                    source="agent",
                    message="".join(text_parts),
                    timestamp=timestamp,
                    model_name=message.get("model")
                    if isinstance(message.get("model"), str)
                    else None,
                    reasoning_content="".join(reasoning_parts) or None,
                    tool_calls=calls or None,
                    observation=AtifObservation(results=observations) if observations else None,
                    extra=_step_extra(event, attachments=attachments),
                )
                continue
            if kind == "result":
                ensure_instruction()
                if isinstance(event.get("session_id"), str):
                    native_session_id = event["session_id"]
                final = _text_of(event)
                if final and (
                    not builder.steps
                    or builder.steps[-1].source != "agent"
                    or builder.steps[-1].message != final
                ):
                    builder.add(
                        source="agent",
                        message=final,
                        timestamp=timestamp,
                        extra=_step_extra(event),
                    )

        orphan_results = sorted(set(results) - known_calls)
        if orphan_results:
            raise TrajectoryReferenceError(
                f"Claude tool results reference unknown calls: {', '.join(orphan_results)}"
            )
        ensure_instruction()
        if (
            not any(step.source == "agent" for step in builder.steps)
            and context.final_message is not None
        ):
            builder.add(source="agent", message=context.final_message)
        trajectory = builder.build()
        if native_session_id and trajectory.session_id != native_session_id:
            trajectory = trajectory.model_copy(update={"session_id": native_session_id})
        if sidechains:
            trajectory = self._embed_sidechains(trajectory, sidechains, context)
        return AtifTrajectory.model_validate(trajectory.model_dump())

    def _embed_sidechains(
        self,
        root: AtifTrajectory,
        groups: dict[str, list[dict[str, Any]]],
        context: TrajectoryParseContext,
    ) -> AtifTrajectory:
        embedded: list[AtifTrajectory] = []
        refs_by_call: dict[str, list[AtifSubagentTrajectoryRef]] = {}
        for agent_id, events in sorted(groups.items()):
            parent_call = _sidechain_parent(events)
            if parent_call is None:
                raise TrajectoryReferenceError(
                    f"Claude subagent {agent_id!r} has no parent tool call"
                )
            trajectory_id = f"{context.trajectory_id}-subagent-{_safe_id(agent_id)}"
            normalized = [
                {
                    key: value
                    for key, value in event.items()
                    if key
                    not in {
                        "isSidechain",
                        "agentId",
                        "parentToolUseID",
                        "parent_tool_use_id",
                    }
                }
                for event in events
            ]
            with tempfile.TemporaryDirectory(prefix="ale-claude-subagent-") as directory:
                logs_dir = Path(directory)
                (logs_dir / TRANSCRIPT_NAME).write_text(
                    "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in normalized)
                )
                child = self.parse_trajectory(
                    TrajectoryParseContext(
                        episode_id=context.episode_id,
                        trajectory_id=trajectory_id,
                        instruction=_delegated_instruction(root, parent_call),
                        logs_dir=logs_dir,
                        model=context.model,
                        agent_version=context.agent_version,
                        blobs=context.blobs,
                        session_id=_event_session(events),
                        incomplete=context.incomplete,
                        incomplete_reason=context.incomplete_reason,
                    )
                )
            embedded.append(child)
            refs_by_call.setdefault(parent_call, []).append(
                AtifSubagentTrajectoryRef(
                    trajectory_id=trajectory_id,
                    session_id=child.session_id,
                )
            )

        linked: set[str] = set()
        steps = []
        for step in root.steps:
            calls = {call.tool_call_id for call in step.tool_calls or ()}
            matching = calls & refs_by_call.keys()
            if not matching:
                steps.append(step)
                continue
            results = list(step.observation.results if step.observation else ())
            by_call = {
                result.source_call_id: index
                for index, result in enumerate(results)
                if result.source_call_id is not None
            }
            for call_id in sorted(matching):
                linked.add(call_id)
                refs = refs_by_call[call_id]
                if call_id in by_call:
                    index = by_call[call_id]
                    current = results[index]
                    results[index] = current.model_copy(
                        update={
                            "subagent_trajectory_ref": [
                                *(current.subagent_trajectory_ref or ()),
                                *refs,
                            ]
                        }
                    )
                else:
                    results.append(
                        AtifObservationResult(
                            source_call_id=call_id,
                            subagent_trajectory_ref=refs,
                        )
                    )
            steps.append(step.model_copy(update={"observation": AtifObservation(results=results)}))
        unresolved = sorted(refs_by_call.keys() - linked)
        if unresolved:
            raise TrajectoryReferenceError(
                "Claude subagents reference unknown parent calls: " + ", ".join(unresolved)
            )
        return root.model_copy(
            update={
                "steps": steps,
                "subagent_trajectories": [
                    *(root.subagent_trajectories or ()),
                    *embedded,
                ],
            }
        )

    # --- internals ---

    def _env(self, session: HarnessSession) -> dict[str, str]:
        """Point the agent's own SDK at the gateway.

        No patching, no wrapper: the agent believes it is talking to a provider, and every
        call is metered and recorded regardless of what it does.

        The alias variables are the part that is easy to miss. Claude Code does not use
        one model — it reaches for a small one to summarise, name a session or run a
        subagent, and asks for it by alias. Against Anthropic those aliases resolve. Against
        anything else they are model names the endpoint has never heard of, so the run dies
        partway through on a request nobody made deliberately. Pointing every alias at the
        one model a run declared is what makes a third-party endpoint usable at all.
        Borrowed from Harbor, which learned it the same way.
        """
        config_dir = PurePosixPath(session.home) / ".claude-config"
        env = {
            "ANTHROPIC_BASE_URL": session.gateway_url,
            "ANTHROPIC_API_KEY": session.token,
            "ANTHROPIC_AUTH_TOKEN": session.token,
            "ANTHROPIC_MODEL": session.model,
            # Telemetry has nowhere to go under a deny-all policy, and an agent retrying a
            # blocked request is an agent spending its budget on nothing.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # Without this the CLI refuses `bypassPermissions` outright in some
            # environments; it is the switch that says the isolation is the sandbox's job.
            "IS_SANDBOX": "1",
            # Somewhere writable that belongs to the agent. Left unset, the CLI writes to a
            # home directory it may not own.
            "CLAUDE_CONFIG_DIR": str(config_dir),
            # The same search path `install` verified against. Without it the launch can
            # run a different binary from the one whose version was checked and recorded,
            # and provenance would name a build that never ran.
            "PATH": (
                f"{PurePosixPath(session.home) / CLI_PREFIX}/bin:/usr/local/bin:/usr/bin:/bin"
            ),
        }
        for alias in (
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env[alias] = session.model
        return env

    def _continuation_fingerprint(self, session: HarnessSession) -> str:
        return continuation_fingerprint(
            harness=self.name,
            model=session.model,
            settings=self.settings.model_dump(mode="json"),
            resources_digest=session.resources_digest,
        )

    def _flags(self) -> str:
        parts: list[str] = []
        if isinstance(self.settings.max_turns, int):
            parts.extend(("--max-turns", str(self.settings.max_turns)))
        if isinstance(self.settings.max_budget_usd, float):
            parts.extend(("--max-budget-usd", str(self.settings.max_budget_usd)))
        if self.settings.permission_mode != "default":
            mode = (
                "default"
                if self.settings.permission_mode == "manual"
                else self.settings.permission_mode
            )
            parts.extend(("--permission-mode", shlex.quote(mode)))
        if self.settings.allowed_tools:
            parts.append("--allowedTools")
            parts.extend(shlex.quote(tool) for tool in self.settings.allowed_tools)
        if self.settings.disallowed_tools:
            parts.append("--disallowedTools")
            parts.extend(shlex.quote(tool) for tool in self.settings.disallowed_tools)
        if self.settings.append_system_prompt != "default":
            parts.extend(
                ("--append-system-prompt", shlex.quote(self.settings.append_system_prompt))
            )
        if self.settings.effort != "default":
            parts.extend(("--effort", self.settings.effort))
        return " ".join(parts)

    def _classify(self, stderr: str, exit_code: int) -> Exception:
        if "ale_limit_reached" in stderr:
            limit = _match(stderr, r'"limit"\s*:\s*"([^"]+)"') or "unknown"
            value = float(_match(stderr, r'"value"\s*:\s*([0-9.eE+-]+)') or 0)
            observed = _match(stderr, r'"observed_value"\s*:\s*([0-9.eE+-]+)')
            return BudgetExceededError(
                limit,
                value,
                float(observed) if observed is not None else None,
            )
        lowered = stderr.lower()
        if isinstance(self.settings.max_turns, int) and "max turns" in lowered:
            return HarnessLimitError(
                "max_turns",
                self.settings.max_turns,
                self.settings.max_turns,
            )
        if isinstance(self.settings.max_budget_usd, float) and "max budget" in lowered:
            return HarnessLimitError(
                "max_budget_usd",
                self.settings.max_budget_usd,
                self.settings.max_budget_usd,
            )
        for needle, error_type in ERROR_PATTERNS:
            if needle.lower() in stderr.lower():
                return error_type(f"claude-code failed: {stderr[-500:]}")
        return AgentError(f"claude-code exited {exit_code}: {stderr[-500:]}")

    async def _read_text(self, sandbox: Sandbox, path: PurePosixPath) -> str:
        try:
            return (await sandbox.read_file(path)).decode("utf-8", "replace")
        except Exception:  # the file may not exist if the agent died early
            return ""


def _final_message(transcript: str) -> str | None:
    """The last thing the agent said, which is what a human reads first."""
    for line in reversed(transcript.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") in {"assistant", "result"}:
            return _text_of(event)
    return None


def _atif_content(
    value: Any, context: TrajectoryParseContext
) -> tuple[str | list[AtifContentPart], list[dict[str, Any]]]:
    attachments: list[dict[str, Any]] = []
    if isinstance(value, str):
        retained = context.blobs.text(value)
        if isinstance(retained, InlineText):
            return retained.inline, attachments
        attachments.append(retained.model_dump(mode="json"))
        return f"[large text retained at {retained.path}]", attachments
    if isinstance(value, dict) and value.get("type") == "image":
        value = [value]
    if isinstance(value, list):
        parts: list[AtifContentPart] = []
        for part in value:
            if not isinstance(part, dict):
                parts.append(AtifContentPart(type="text", text=str(part)))
                continue
            if part.get("type") == "text":
                text, refs = _atif_content(str(part.get("text") or ""), context)
                attachments.extend(refs)
                if isinstance(text, str):
                    parts.append(AtifContentPart(type="text", text=text))
                else:
                    parts.extend(text)
                continue
            if part.get("type") == "image":
                source = part.get("source") if isinstance(part.get("source"), dict) else {}
                media_type = str(source.get("media_type") or "image/png")
                encoded = source.get("data")
                if isinstance(encoded, str):
                    data = _decode_base64(encoded, "image")
                    ref = context.blobs.put(data, media_type=media_type)
                    parts.append(
                        AtifContentPart(
                            type="image",
                            source=AtifImageSource(
                                media_type=media_type,  # type: ignore[arg-type]
                                path=ref.path,
                            ),
                        )
                    )
                    continue
            source = (
                part.get("source")
                if isinstance(part.get("source"), dict)
                else part.get("resource")
                if isinstance(part.get("resource"), dict)
                else {}
            )
            encoded = source.get("data") or source.get("blob")
            media_type = str(
                source.get("media_type")
                or source.get("mime_type")
                or source.get("mimeType")
                or part.get("media_type")
                or "application/octet-stream"
            )
            if isinstance(encoded, str):
                data = _decode_base64(encoded, str(part.get("type") or "attachment"))
                ref = context.blobs.put(data, media_type=media_type)
                if media_type.startswith("image/"):
                    parts.append(
                        AtifContentPart(
                            type="image",
                            source=AtifImageSource(
                                media_type=media_type,  # type: ignore[arg-type]
                                path=ref.path,
                            ),
                        )
                    )
                else:
                    attachments.append(ref.model_dump(mode="json"))
                    parts.append(
                        AtifContentPart(
                            type="text",
                            text=f"[{media_type} retained at {ref.path}]",
                        )
                    )
                continue
            rendered = json.dumps(part, ensure_ascii=False, sort_keys=True)
            text, refs = _atif_content(rendered, context)
            attachments.extend(refs)
            parts.append(
                AtifContentPart(
                    type="text",
                    text=text if isinstance(text, str) else rendered,
                )
            )
        return parts, attachments
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return _atif_content(rendered, context)


def _step_extra(
    event: dict[str, Any], *, attachments: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    ale: dict[str, Any] = {}
    message = event.get("message")
    native_id = (message.get("id") if isinstance(message, dict) else None) or event.get("uuid")
    if native_id:
        ale["native_event_ids"] = [str(native_id)]
    if attachments:
        ale["attachments"] = attachments
    return {"ale": ale} if ale else None


def _tool_extra(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if not name.startswith("mcp__"):
        return None
    _, server, tool = [*name.split("__", 2), "", ""][:3]
    ale: dict[str, Any] = {"mcp": {"server": server, "tool": tool}}
    if server == CUA_DESKTOP_NAME:
        try:
            action = _cua_action(tool, arguments)
        except ValidationError:
            action = None
        if action is not None:
            ale["normalized_action"] = action.model_dump(mode="json", exclude_none=True)
    return {"ale": ale}


def _text_of(event: dict[str, Any]) -> str:
    message = event.get("message") or event
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(event.get("result", ""))


def _native_session_id(transcript: str) -> str | None:
    for line in reversed(transcript.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def _decode_base64(value: str, kind: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise TrajectoryReferenceError(f"Claude {kind} contains invalid base64") from exc


def _sidechain_groups(
    events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if not event.get("isSidechain"):
            continue
        agent_id = event.get("agentId") or event.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise TrajectoryReferenceError("Claude sidechain event has no agentId")
        groups.setdefault(agent_id, []).append(event)
    return groups


def _sidechain_parent(events: list[dict[str, Any]]) -> str | None:
    parents = {
        value
        for event in events
        for value in (
            event.get("parentToolUseID"),
            event.get("parent_tool_use_id"),
        )
        if isinstance(value, str) and value
    }
    if len(parents) > 1:
        raise TrajectoryReferenceError("Claude subagent has multiple parent tool calls")
    return next(iter(parents), None)


def _delegated_instruction(root: AtifTrajectory, call_id: str) -> str:
    for step in root.steps:
        for call in step.tool_calls or ():
            if call.tool_call_id != call_id:
                continue
            for key in ("prompt", "instruction", "task", "description"):
                value = call.arguments.get(key)
                if isinstance(value, str):
                    return value
            return json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
    return ""


def _event_session(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        value = event.get("session_id") or event.get("sessionId")
        if isinstance(value, str) and value:
            return value
    return None


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return normalized or "unknown"


def _content_text(value: str | list[AtifContentPart]) -> str:
    if isinstance(value, str):
        return value
    return "".join(part.text or "" for part in value if part.type == "text")


def _cua_action(tool: str, arguments: dict[str, Any]) -> DesktopAction | None:
    action_type = {
        "screenshot": "screenshot",
        "click": "click",
        "mouse_move": "move",
        "drag": "drag",
        "mouse_down": "mouse_down",
        "mouse_up": "mouse_up",
        "scroll": "scroll",
        "type": "type",
        "key": "key",
        "key_down": "key_down",
        "key_up": "key_up",
        "hold_key": "hold_key",
        "cursor_position": "cursor_position",
        "wait": "wait",
    }.get(tool)
    if action_type is None:
        return None
    coordinate = arguments.get("coordinate")
    destination = None
    if tool == "drag":
        coordinate = arguments.get("start_coordinate")
        destination = arguments.get("coordinate")
    duration = arguments.get("duration")
    return DesktopAction(
        type=action_type,  # type: ignore[arg-type]
        coordinate=coordinate,
        to=destination,
        text=arguments.get("text"),
        keys=arguments.get("keys"),
        button=arguments.get("button"),
        clicks=arguments.get("clicks"),
        direction=arguments.get("direction"),
        amount=arguments.get("amount"),
        duration_ms=round(float(duration) * 1000) if duration is not None else None,
    )


def _match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None
