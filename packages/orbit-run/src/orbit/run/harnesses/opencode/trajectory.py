"""Pure conversion of OpenCode evidence to ATIF."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from orbit.core.harness import TrajectoryParseContext
from orbit.core.trajectory import (
    AtifAgent,
    AtifContentPart,
    AtifImageSource,
    AtifMetrics,
    AtifObservation,
    AtifObservationResult,
    AtifSubagentTrajectoryRef,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from orbit.run.harnesses._diagnostics import error_text
from orbit.run.harnesses.base import (
    Invocation,
    NativeResult,
    invocations,
    json_lines,
    requested_session_id,
    unconverted,
)

#: ``OPENCODE_CONFIG_DIR``.
STATE = ".opencode-orbit"
#: Orbit's configuration document, which names each MCP server as declared.
CONFIG = f"{STATE}/orbit.json"
#: One export per invocation of the session and every session it spawned:
#: ``<root session id>/<time>/<session id>.json``.
EXPORTS = f"{STATE}/exports"
#: The custom provider every model call goes through.
PROVIDER = "orbit"
_DATA_URI = re.compile(r"data:([a-z]+/[a-z0-9.+-]+);base64,(.*)", re.DOTALL)
#: The image types ATIF records, by the type a data URI names.
_IMAGES = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}
#: Bookkeeping a session records that is no part of the conversation.
_BOOKKEEPING = frozenset({"agent-switched", "model-switched", "location-switched"})


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    return (
        path.parent.parent == PurePosixPath(EXPORTS, session_id)
        and not path.parent.name.startswith(".")
        and path.name == f"{session_id}.json"
    )


def read_result(stdout: str) -> NativeResult:
    """The session and final answer of one invocation's JSON events, and its error."""
    events, _ = json_lines(stdout)
    session_id = next(
        (
            event["sessionID"]
            for event in reversed(events)
            if isinstance(event.get("sessionID"), str) and event["sessionID"]
        ),
        None,
    )
    texts = _texts(events)
    error = next(
        (
            error_text(event.get("error")) or "opencode reported an error"
            for event in reversed(events)
            if event.get("type") == "error"
        ),
        None,
    )
    return NativeResult(session_id, texts[-1] if texts else None, error)


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    expected = requested_session_id(context, recorded) or next(
        (found for item in reversed(recorded) if (found := read_result(item.stdout).session_id)),
        None,
    )
    issues: list[dict[str, Any]] = []
    exported = _latest_export(context, expected, issues)
    root = exported.get(expected or "")
    parse = _Parse(context, _mcp_servers(context), exported, issues)
    turns = _Turns(
        recorded, [read_result(item.stdout).error for item in recorded], root is not None
    )
    builder = _builder(context, context.trajectory_id, expected)
    if root is not None:
        parse.convert(builder, expected or "", root, turns)
    parse.rebuild(builder, turns, len(recorded))
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    issues.extend(
        {"reason": "unlinked_subagent", "session_id": session_id}
        for session_id in sorted(set(exported) - parse.claimed - {expected})
    )
    issues.extend(
        {"reason": "unknown_message_type", "type": kind, "count": count}
        for kind, count in sorted(parse.unknown.items())
    )
    issues.extend(unconverted(context, _other_sessions(context, expected)))
    trajectory = builder.build(subagent_trajectories=parse.children or None)
    extra = _extra(context, issues, native=root is not None)
    return trajectory.model_copy(update={"extra": extra}) if extra else trajectory


@dataclass
class _Turns:
    """The recorded invocations, matched in order to the prompts a session holds."""

    recorded: list[Invocation]
    failures: list[str | None]
    #: Whether a session export exists, so that a turn missing from it is an issue.
    native: bool
    #: The first invocation no prompt of the session has matched.
    pending: int = 0
    #: The invocation whose turn the session is in, if an invocation sent its prompt.
    current: int | None = None

    def match(self, prompt: str) -> int | None:
        """The next pending invocation that sent ``prompt``."""
        return next(
            (
                index
                for index in range(self.pending, len(self.recorded))
                if self.recorded[index].prompt == prompt
            ),
            None,
        )


@dataclass
class _Parse:
    """Conversion state shared by a session and the sessions it spawned."""

    context: TrajectoryParseContext
    servers: dict[str, str]
    exported: dict[str, dict[str, Any]]
    issues: list[dict[str, Any]]
    children: list[AtifTrajectory] = field(default_factory=list)
    claimed: set[str] = field(default_factory=set)
    #: The sessions being converted, which no call of theirs may embed again.
    converting: set[str] = field(default_factory=set)
    unknown: dict[str, int] = field(default_factory=dict)

    def convert(
        self,
        builder: TrajectoryBuilder,
        session_id: str,
        data: dict[str, Any],
        turns: _Turns | None = None,
        children: list[AtifTrajectory] | None = None,
    ) -> None:
        """Add one exported session's steps; ``turns`` match its prompts to invocations."""
        spawned = self.children if children is None else children
        self.converting.add(session_id)
        recorded_failure = False
        messages = data.get("messages")
        for message in messages if isinstance(messages, list) else ():
            if not isinstance(message, dict):
                continue
            kind = message.get("type") if isinstance(message.get("type"), str) else ""
            time = message.get("time") if isinstance(message.get("time"), dict) else {}
            timestamp = _timestamp(time.get("created"))
            if kind == "user":
                text = str(message.get("text") or "")
                if turns is not None:
                    _turn_failure(builder, turns, recorded_failure)
                    recorded_failure = False
                    matched = turns.match(text)
                    if matched is not None:
                        # Invocations whose prompt never reached the session come first.
                        self.rebuild(builder, turns, matched)
                        turns.pending = matched + 1
                    turns.current = matched
                builder.add(source="user", message=text, timestamp=timestamp)
            elif kind == "assistant":
                self._assistant(builder, session_id, message, timestamp, spawned)
            elif kind in {"synthetic", "system", "skill"}:
                builder.add(
                    source="system", message=str(message.get("text") or ""), timestamp=timestamp
                )
            elif kind == "shell":
                output = message.get("output")
                text = output if isinstance(output, str) else json.dumps(output, sort_keys=True)
                builder.add(
                    source="system",
                    message=f"$ {message.get('command') or ''}\n{text if output else ''}".rstrip(),
                    timestamp=timestamp,
                )
            elif kind == "compaction":
                summary = message.get("summary")
                if not isinstance(summary, str):
                    summary = f"compaction failed: {error_text(message.get('error'))}"
                builder.add(source="system", message=summary, timestamp=timestamp)
            elif kind == "idle":
                if message.get("outcome") not in (None, "succeeded"):
                    recorded_failure = True
                    failure = error_text(message.get("error")) or f"turn {message.get('outcome')}"
                    # A turn that ends on its last step's error repeats it.
                    last = builder.steps[-1] if builder.steps else None
                    if last is None or (last.source, last.message) != ("system", failure):
                        builder.add(source="system", message=failure, timestamp=timestamp)
            elif kind not in _BOOKKEEPING:
                self.unknown[kind] = self.unknown.get(kind, 0) + 1
        if turns is not None:
            _turn_failure(builder, turns, recorded_failure)
            turns.current = None
        self.converting.discard(session_id)

    def rebuild(self, builder: TrajectoryBuilder, turns: _Turns, until: int) -> None:
        """The pending invocations before ``until``, each its prompt and JSON events."""
        for index in range(turns.pending, until):
            item = turns.recorded[index]
            if turns.native:
                self.issues.append(
                    {"reason": "prompt_not_in_native_session", "invocation": index + 1}
                )
            events, problems = json_lines(item.stdout)
            self.issues.extend({"invocation": index + 1, **problem} for problem in problems)
            builder.add(source="user", message=item.prompt)
            self.stdout_turn(builder, events)
            if failure := turns.failures[index]:
                builder.add(source="system", message=failure)
        turns.pending = max(turns.pending, until)

    def _assistant(
        self,
        builder: TrajectoryBuilder,
        session_id: str,
        message: dict[str, Any],
        timestamp: str | None,
        spawned: list[AtifTrajectory],
    ) -> None:
        texts: list[str] = []
        thoughts: list[str] = []
        calls: list[AtifToolCall] = []
        observed: list[AtifObservationResult] = []
        content = message.get("content")
        for part in content if isinstance(content, list) else ():
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif part.get("type") == "reasoning" and isinstance(part.get("text"), str):
                thoughts.append(part["text"])
            elif part.get("type") == "tool":
                call_id, name = str(part.get("id") or ""), str(part.get("name") or "")
                if any(call.tool_call_id == call_id for call in calls):
                    self.issues.append(
                        {"reason": "duplicate_tool_call", "session_id": session_id, "id": call_id}
                    )
                    continue
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                calls.append(
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name=name,
                        arguments=_arguments(state.get("input")),
                        extra=self._mcp(name),
                    )
                )
                if state.get("status") in ("completed", "error"):
                    observed.append(
                        self._result(builder, session_id, call_id, name, state, spawned)
                    )
        model = message.get("model") if isinstance(message.get("model"), dict) else {}
        builder.add(
            source="agent",
            message="\n\n".join(text for text in texts if text),
            timestamp=timestamp,
            model_name=model.get("id") if isinstance(model.get("id"), str) else None,
            reasoning_content="\n\n".join(thoughts) or None,
            tool_calls=calls or None,
            observation=AtifObservation(results=observed) if observed else None,
            metrics=_metrics(message.get("tokens")),
        )
        if failure := error_text(message.get("error")):
            builder.add(source="system", message=failure, timestamp=timestamp)

    def _result(
        self,
        builder: TrajectoryBuilder,
        session_id: str,
        call_id: str,
        name: str,
        state: dict[str, Any],
        spawned: list[AtifTrajectory],
    ) -> AtifObservationResult:
        parts = self._content(state.get("content"))
        failure = error_text(state.get("error")) if state.get("status") == "error" else ""
        if failure:
            parts.insert(0, AtifContentPart(type="text", text=failure))
        refs: list[AtifSubagentTrajectoryRef] = []
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        child_id = metadata.get("sessionID") if name == "subagent" else None
        if isinstance(child_id, str) and (
            child := self._child(builder.trajectory_id, session_id, child_id, spawned)
        ):
            refs.append(AtifSubagentTrajectoryRef(trajectory_id=child, session_id=child_id))
        content: str | list[AtifContentPart] = (
            "\n".join(part.text or "" for part in parts)
            if all(part.type == "text" for part in parts)
            else parts
        )
        return AtifObservationResult(
            source_call_id=call_id,
            content=content,
            subagent_trajectory_ref=refs or None,
            extra={"orbit": {"is_error": True}} if state.get("status") == "error" else None,
        )

    def _child(
        self, trajectory_id: str, spawner: str, child_id: str, spawned: list[AtifTrajectory]
    ) -> str | None:
        """The trajectory ID of a session the ``spawner`` session's call started, embedded
        in the spawner's trajectory, ``trajectory_id``, the first time a call names it."""
        data = self.exported.get(child_id)
        info = data.get("info") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            self.issues.append({"reason": "subagent_session_unavailable", "session_id": child_id})
            return None
        if info.get("parentID") != spawner or child_id in self.converting:
            return None
        trajectory_id = f"{trajectory_id}-subagent-{child_id}"
        if child_id in self.claimed:
            return trajectory_id
        self.claimed.add(child_id)
        builder = _builder(self.context, trajectory_id, child_id)
        nested: list[AtifTrajectory] = []
        self.convert(builder, child_id, data or {}, children=nested)
        if not builder.steps:
            builder.add(source="user", message=str(info.get("title") or ""))
        spawned.append(builder.build(subagent_trajectories=nested or None))
        return trajectory_id

    def _content(self, value: Any) -> list[AtifContentPart]:
        parts: list[AtifContentPart] = []
        for item in value if isinstance(value, list) else ():
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(AtifContentPart(type="text", text=str(item.get("text") or "")))
            elif item.get("type") == "file":
                parts.append(self._file(item))
        return parts

    def _file(self, item: dict[str, Any]) -> AtifContentPart:
        uri, mime = str(item.get("uri") or ""), str(item.get("mime") or "")
        match = _DATA_URI.fullmatch(uri)
        media = _IMAGES.get(match.group(1)) if match else None
        if match and media:
            try:
                data = base64.b64decode(match.group(2), validate=True)
            except (binascii.Error, ValueError):
                return AtifContentPart(type="text", text=f"[image {mime} with invalid base64]")
            blob = self.context.blobs.put(data, media_type=media)
            return AtifContentPart(
                type="image",
                source=AtifImageSource(media_type=media, path=blob.path),  # type: ignore[arg-type]
            )
        # Any other file, an image ATIF has no type for included, is named, not embedded.
        shown = f"data:{match.group(1)}" if match else uri
        named = (str(item.get("name") or ""), mime, shown)
        return AtifContentPart(
            type="text", text=f"[file {' '.join(part for part in named if part)}]"
        )

    def stdout_turn(self, builder: TrajectoryBuilder, events: list[dict[str, Any]]) -> None:
        """One turn from its JSON events alone: each model step's text and tool calls."""
        steps: dict[str, dict[str, Any]] = {}
        for event in events:
            part = event.get("part") if isinstance(event.get("part"), dict) else None
            if part is None or not isinstance(part.get("messageID"), str):
                continue
            step = steps.setdefault(part["messageID"], {"texts": {}, "tools": {}, "tokens": None})
            if event.get("type") == "text":
                key = str(part.get("id") or "")
                step["texts"][key] = step["texts"].get(key, "") + str(part.get("text") or "")
            elif event.get("type") == "tool_use":
                step["tools"][str(part.get("id") or "")] = part
            elif event.get("type") == "step_finish":
                step["tokens"] = part.get("tokens")
        for step in steps.values():
            calls, observed = [], []
            for call_id, part in step["tools"].items():
                name = str(part.get("tool") or "")
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                calls.append(
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name=name,
                        arguments=_arguments(state.get("input")),
                        extra=self._mcp(name),
                    )
                )
                if state.get("status") not in ("completed", "error"):
                    continue
                failed = state.get("status") == "error"
                observed.append(
                    AtifObservationResult(
                        source_call_id=call_id,
                        content=error_text(state.get("error"))
                        if failed
                        else str(state.get("output") or ""),
                        extra={"orbit": {"is_error": True}} if failed else None,
                    )
                )
            builder.add(
                source="agent",
                message="\n\n".join(text for text in step["texts"].values() if text),
                tool_calls=calls or None,
                observation=AtifObservation(results=observed) if observed else None,
                metrics=_metrics(step["tokens"]),
            )

    def _mcp(self, name: str) -> dict[str, Any] | None:
        """``<server>_<tool>``, each as OpenCode normalizes it, for a configured server."""
        for normalized in sorted(self.servers, key=len, reverse=True):
            prefix = f"{normalized}_"
            if name.startswith(prefix) and len(name) > len(prefix):
                tool = name[len(prefix) :]
                return {"orbit": {"mcp": {"server": self.servers[normalized], "tool": tool}}}
        return None


def _builder(
    context: TrajectoryParseContext, trajectory_id: str, session_id: str | None
) -> TrajectoryBuilder:
    return TrajectoryBuilder(
        trajectory_id=trajectory_id,
        session_id=session_id,
        agent=AtifAgent(
            name="opencode", version=context.agent_version, model_name=context.model or None
        ),
    )


def _turn_failure(builder: TrajectoryBuilder, turns: _Turns, recorded: bool) -> None:
    """How the current turn ended when only its stdout says so, after the turn's own steps."""
    if not recorded and turns.current is not None and (failure := turns.failures[turns.current]):
        builder.add(source="system", message=failure)


def _texts(events: list[dict[str, Any]]) -> list[str]:
    """Each text part's whole text, in order; OpenCode prints a part's later text as
    a continuation of what it printed."""
    texts: dict[str, str] = {}
    for event in events:
        part = event.get("part") if isinstance(event.get("part"), dict) else None
        if event.get("type") == "text" and part is not None:
            key = str(part.get("id") or "")
            texts[key] = texts.get(key, "") + str(part.get("text") or "")
    return [text for text in texts.values() if text]


def _latest_export(
    context: TrajectoryParseContext, session_id: str | None, issues: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The newest export that holds the session: it and each session it spawned, by ID."""
    base = context.logs_dir / EXPORTS
    if not session_id or not (base / session_id).is_dir():
        issues.append({"reason": "native_session_unavailable", "session_id": session_id})
        return {}
    exports = sorted(
        (path for path in (base / session_id).iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    for directory in (path for path in exports if not path.name.startswith(".")):
        sessions: dict[str, dict[str, Any]] = {}
        for path in sorted(directory.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                issues.append(
                    {
                        "source": path.relative_to(context.logs_dir).as_posix(),
                        "reason": "malformed_json",
                        "detail": str(exc)[:500],
                    }
                )
                continue
            if isinstance(value, dict) and isinstance(value.get("messages"), list):
                sessions[path.stem] = value
        if session_id in sessions:
            return sessions
    issues.append({"reason": "native_session_unavailable", "session_id": session_id})
    return {}


def _other_sessions(context: TrajectoryParseContext, session_id: str | None) -> list[Path]:
    """The newest export of each other session Orbit exported in this home."""
    base = context.logs_dir / EXPORTS
    found = []
    for directory in sorted(base.iterdir()) if base.is_dir() else ():
        if directory.name == session_id or not directory.is_dir():
            continue
        exports = sorted(
            (path for path in directory.iterdir() if path.is_dir() and path.name[:1] != "."),
            reverse=True,
        )
        root = exports[0] / f"{directory.name}.json" if exports else None
        if root is not None and root.is_file():
            found.append(root)
    return found


def _mcp_servers(context: TrajectoryParseContext) -> dict[str, str]:
    """OpenCode's normalized name of each configured MCP server, to its declared name."""
    path = context.logs_dir / CONFIG
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    mcp = value.get("mcp") if isinstance(value, dict) else None
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    return {
        normalized_name(str(name)): str(name)
        for name in (servers if isinstance(servers, dict) else ())
    }


def normalized_name(name: str) -> str:
    """A name as OpenCode spells it in a tool's name."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def _metrics(tokens: Any) -> AtifMetrics | None:
    if not isinstance(tokens, dict):
        return None
    fresh, text = tokens.get("input"), tokens.get("output")
    if not isinstance(fresh, int) or not isinstance(text, int):
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    read, written, thought = (
        value if isinstance(value, int) else 0
        for value in (cache.get("read"), cache.get("write"), tokens.get("reasoning"))
    )
    # OpenCode counts cached input and reasoning apart. As the Gateway accounts them, the
    # prompt includes prompt-cache reads and writes, and the completion its reasoning.
    return AtifMetrics(
        prompt_tokens=fresh + read + written, completion_tokens=text + thought, cached_tokens=read
    )


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        # A call still streaming its input when the session ended.
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {} if value is None else {"value": value}


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _extra(
    context: TrajectoryParseContext, issues: list[dict[str, Any]], *, native: bool
) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if not native:
        data["limitations"] = [
            "No session export is available; each turn keeps only its prompt and JSON events."
        ]
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"orbit": data} if data else None
