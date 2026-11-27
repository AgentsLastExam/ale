"""Pure conversion of Antigravity CLI evidence to ATIF."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

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
    call_metrics,
    epoch_time,
    invocations,
    iso_time,
    mirrored_store,
    native_events,
    requested_session_id,
    stdout_events,
    token_count,
    unconverted,
)

NATIVE = ".gemini/antigravity-cli"
BRAIN = f"{NATIVE}/brain"
CONVERSATIONS = f"{NATIVE}/conversations"
#: Each conversation's step records, complete and with structured tool arguments.
TRANSCRIPT = ".system_generated/logs/transcript_full.jsonl"
SUBAGENTS = ".system_generated/subagents"
STEP_OUTPUT = ".system_generated/steps/{}/output.txt"
_REQUEST = re.compile(r"<USER_REQUEST>\n(.*?)\n</USER_REQUEST>", re.DOTALL)
#: One message agy delivers inside a SYSTEM_MESSAGE record: from a parent, a subagent or a task.
_MESSAGE = re.compile(
    r"\[Message\] timestamp=\S+ sender=(\S+) priority=\S+ content=(.*?)"
    r"(?=\n\[Message\] |\n</SYSTEM_MESSAGE>|\Z)",
    re.DOTALL,
)
#: Bookkeeping records that are neither conversation nor tool output.
_INTERNAL = frozenset({"CHECKPOINT", "CONVERSATION_HISTORY", "EPHEMERAL_MESSAGE"})
_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
#: A result's resource that agy saved to a file of the step instead of inlining it.
_OFFLOADED = re.compile(r"\[Resource offloaded to (file://[^\]\s]+)\]")
_SUFFIX_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
#: Field numbers of the protobuf messages agy stores as each step's ``metadata``:
#: ``CortexStepMetadata.created_at`` and ``.model_usage``, ``google.protobuf.Timestamp``'s
#: ``seconds`` and ``nanos``, and ``ModelUsageStats.message_id``.
_CREATED_AT, _MODEL_USAGE = 1, 9
_SECONDS, _NANOS = 1, 2
_MESSAGE_ID = 7
#: A planner response's token counts: uncached input, cache reads and output.
_USAGE = ("input_tokens", "cache_read_tokens", "output_tokens")


@dataclass(frozen=True)
class _Stored:
    """What the conversation database adds to a step record: its time below the second,
    where the transcript keeps whole seconds, and the response ID of the model call that
    produced it."""

    timestamp: str | None
    response_id: str | None


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    """A conversation's brain directory, or its database, by its exact ID."""
    if path.is_relative_to(BRAIN):
        parts = path.relative_to(BRAIN).parts
        return len(parts) > 1 and parts[0] == session_id
    return path.parent == PurePosixPath(CONVERSATIONS) and path.name == f"{session_id}.db"


def read_result(stdout: str) -> NativeResult:
    return _result([event for line in stdout.splitlines() if (event := _json(line)) is not None])


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    turns, issues = stdout_events(recorded)
    stream = [event for turn in turns for event in turn]
    expected = requested_session_id(context, recorded) or _result(stream).session_id
    records: list[dict[str, Any]] = []
    transcript = context.logs_dir / BRAIN / str(expected) / TRANSCRIPT if expected else None
    if transcript is not None and transcript.is_file():
        records = _records(context, transcript, issues)
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
    conversation = _Conversation(context, issues)
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=expected,
        agent=_agent(context),
    )
    subagents = conversation.walk(builder, expected, records, recorded=recorded, turns=turns)
    unvisited = [
        path
        for path in sorted((context.logs_dir / BRAIN).glob(f"*/{TRANSCRIPT}"))
        if path.parents[2].name not in conversation.visited
    ]
    if records:
        # A conversation no spawn record links to its spawning call is left unconverted.
        for path in unvisited:
            conversation.unlinked(path)
    else:
        issues.extend(unconverted(context, unvisited))
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    builder.extra = _parse_extra(context, records, issues)
    return builder.build(subagent_trajectories=subagents or None)


class _Conversation:
    """Converts conversations; a subagent is embedded where its parent spawned it."""

    def __init__(self, context: TrajectoryParseContext, issues: list[dict[str, Any]]) -> None:
        self.context = context
        self.issues = issues
        self.visited: set[str] = set()
        self.reported: set[Path] = set()

    def walk(
        self,
        builder: TrajectoryBuilder,
        session_id: str | None,
        records: list[dict[str, Any]],
        *,
        recorded: Sequence[Invocation] = (),
        turns: Sequence[list[dict[str, Any]]] = (),
        parent: str | None = None,
    ) -> list[AtifTrajectory]:
        """Add one conversation to ``builder`` and return the subagents it spawned.

        At the root, the k-th native prompt is the k-th invocation, which supplies the
        prompt as it was sent and, from its stdout, the tool names agy reported and any
        failure the session does not record. In a subagent, what its ``parent``
        conversation sent arrives as messages, which are the subagent's user turns.
        """
        if session_id:
            self.visited.add(session_id)
        tools = _tool_events([event for turn in turns for event in turn])
        spawned = self._spawned(session_id)
        stored = self._stored(session_id)
        embedded: list[AtifTrajectory] = []
        turn = _Turn(builder)
        prompts = 0
        if records and recorded and not any(_type(record) == "USER_INPUT" for record in records):
            builder.add(source="user", message=recorded[0].prompt)
            prompts = 1
        for record in records:
            kind = _type(record)
            if kind in _INTERNAL:
                continue
            step = stored.get(record.get("step_index"), _Stored(None, None))
            timestamp = step.timestamp or iso_time(record.get("created_at"))
            if kind == "PLANNER_RESPONSE":
                turn.flush()
                turn.open(
                    record,
                    timestamp=timestamp,
                    model_name=self.context.model or None,
                    metrics=self._metrics(record, session_id),
                    extra=(
                        {"orbit": {"native_event_ids": [step.response_id]}}
                        if step.response_id
                        else None
                    ),
                )
                continue
            if turn.pending and kind not in {"USER_INPUT", "SYSTEM_MESSAGE"}:
                call_id, name = turn.pending.pop(0)
                result, children = self._result(
                    record, call_id, name, spawned, tools, builder.trajectory_id, session_id
                )
                turn.results.append(result)
                embedded.extend(children)
                continue
            turn.flush()
            if kind == "SYSTEM_MESSAGE" and parent is not None:
                sent = [
                    text
                    for sender, text in _MESSAGE.findall(_text(record.get("content")))
                    if sender == parent
                ]
                for text in sent:
                    builder.add(source="user", message=text, timestamp=timestamp)
                if sent:
                    continue
            if kind == "USER_INPUT":
                if prompts and prompts <= len(turns):
                    _failure(builder, turns[prompts - 1])
                if prompts < len(recorded):
                    message = recorded[prompts].prompt
                else:
                    message = _user_request(_text(record.get("content")))
                builder.add(source="user", message=message, timestamp=timestamp)
                prompts += 1
                continue
            message = _text(record.get("content")) or error_text(record.get("error"))
            if message:
                builder.add(source="system", message=message, timestamp=timestamp)
        turn.flush()
        if recorded:
            if prompts and prompts <= len(turns):
                _failure(builder, turns[prompts - 1])
            if records and prompts < len(recorded):
                self.issues.append(
                    {"reason": "prompt_not_in_native_session", "invocation": prompts + 1}
                )
            # Without the native session, each turn is its prompt and what it reported.
            for item, events in zip(recorded[prompts:], turns[prompts:], strict=True):
                builder.add(source="user", message=item.prompt)
                if message := _result(events).final_message:
                    builder.add(source="agent", message=message)
                _failure(builder, events)
        return embedded

    def _result(
        self,
        record: dict[str, Any],
        call_id: str,
        name: str,
        spawned: dict[int, list[tuple[str, Path]]],
        tools: dict[int, dict[str, Any]],
        parent_id: str,
        session_id: str | None,
    ) -> tuple[AtifObservationResult, list[AtifTrajectory]]:
        step_index = record.get("step_index")
        observed = tools.get(step_index) if isinstance(step_index, int) else None
        if observed is not None and observed.get("tool_name") not in (None, name):
            self.issues.append(
                {
                    "reason": "tool_result_mismatch",
                    "step_index": step_index,
                    "expected": name,
                    "observed": observed.get("tool_name"),
                }
            )
        orbit: dict[str, Any] = {}
        if str(record.get("status") or "").upper() in {"ERROR", "FAILED"}:
            orbit["is_error"] = True
            if error := error_text(record.get("error")):
                orbit["error"] = error
        if session_id and isinstance(step_index, int):
            output = self.context.logs_dir / BRAIN / session_id / STEP_OUTPUT.format(step_index)
            text = _read(output) if output.is_file() else ""
            if text and _plain(text) not in _plain(_text(record.get("content"))):
                # Written after the record, as when a background task ends: the result keeps
                # what the model received, and the tool's final output is kept beside it.
                orbit["final_output"] = text
        children: list[AtifTrajectory] = []
        refs: list[AtifSubagentTrajectoryRef] = []
        for child_id, spawn in spawned.get(step_index, ()) if isinstance(step_index, int) else ():
            child = self._subagent(parent_id, child_id, spawn, session_id)
            if child is None:
                continue
            children.append(child)
            refs.append(
                AtifSubagentTrajectoryRef(trajectory_id=child.trajectory_id, session_id=child_id)
            )
        content = self._content(record)
        return (
            AtifObservationResult(
                source_call_id=call_id,
                content=content if content else (orbit.get("error") or ""),
                subagent_trajectory_ref=refs or None,
                extra={"orbit": orbit} if orbit else None,
            ),
            children,
        )

    def _subagent(
        self, parent_id: str, child_id: str, spawn: Path, parent_session: str | None
    ) -> AtifTrajectory | None:
        """The conversation a spawn record names, once; ``None`` leaves it unconverted."""
        if child_id in self.visited:
            return None
        transcript = self.context.logs_dir / BRAIN / child_id / TRANSCRIPT
        if not transcript.is_file():
            self.unlinked(spawn)
            return None
        builder = TrajectoryBuilder(
            trajectory_id=f"{parent_id}-subagent-{child_id}",
            session_id=child_id,
            agent=_agent(self.context),
        )
        records = _records(self.context, transcript, self.issues)
        nested = self.walk(builder, child_id, records, parent=parent_session)
        if not builder.steps:
            self.visited.discard(child_id)
            self.unlinked(transcript)
            return None
        return builder.build(subagent_trajectories=nested or None)

    def _stored(self, session_id: str | None) -> dict[int, _Stored]:
        """Each step's time and response ID from ``conversations/<id>.db``, by step index.

        agy keeps a step's ``metadata`` there as a ``CortexStepMetadata`` protobuf. Its
        ``created_at`` is the time the transcript rounds down to the second, and its
        ``model_usage`` keeps the ``responseId`` of the Gemini response as ``message_id``.
        """
        path = self.context.logs_dir / CONVERSATIONS / f"{session_id}.db"
        if not session_id or not path.is_file():
            return {}
        source = path.relative_to(self.context.logs_dir).as_posix()
        stored: dict[int, _Stored] = {}
        with mirrored_store(path) as connection:
            try:
                rows = connection.execute("select idx, metadata from steps").fetchall()
            except sqlite3.Error as error:
                self.issues.append(
                    {
                        "source": source,
                        "reason": "native_store_unreadable",
                        "detail": str(error)[:500],
                    }
                )
                return {}
        for index, blob in rows:
            if blob is None:
                continue
            metadata = _fields(blob) if isinstance(blob, bytes) else None
            if not isinstance(index, int) or metadata is None:
                self.issues.append(
                    {"source": source, "reason": "invalid_step_metadata", "step_index": index}
                )
                continue
            created = _fields(_first(metadata, _CREATED_AT)) or {}
            usage = _fields(_first(metadata, _MODEL_USAGE)) or {}
            response = _first(usage, _MESSAGE_ID)
            stored[index] = _Stored(
                _proto_time(created), response.decode("utf-8", "replace") if response else None
            )
        return stored

    def _metrics(self, record: dict[str, Any], session_id: str | None) -> AtifMetrics | None:
        """A planner response's usage, which agy writes on the record itself rather than in a
        usage object: a record with none of its counts has none.

        agy reports uncached input apart from cache reads, which ATIF's prompt includes, and
        output with the thinking. Gemini reports no cache writes.
        """
        if not any(name in record for name in _USAGE):
            return None
        try:
            fresh, read, output = (
                token_count(record.get(name), optional=name == "cache_read_tokens")
                for name in _USAGE
            )
        except ValueError:
            self.issues.append(
                {
                    "source": f"{BRAIN}/{session_id}/{TRANSCRIPT}",
                    "reason": "invalid_token_usage",
                    "step_index": record.get("step_index"),
                }
            )
            return None
        return call_metrics(fresh + read, output, read)

    def unlinked(self, path: Path) -> None:
        """Report a child left unconverted, once."""
        if path in self.reported:
            return
        self.reported.add(path)
        self.issues.append(
            {
                "reason": "unlinked_subagent",
                "source": path.relative_to(self.context.logs_dir).as_posix(),
            }
        )

    def _spawned(self, session_id: str | None) -> dict[int, list[tuple[str, Path]]]:
        """Each subagent a conversation spawned and its record, by the spawning result's step."""
        spawned: dict[int, list[tuple[str, Path]]] = {}
        if not session_id:
            return spawned
        folder = self.context.logs_dir / BRAIN / session_id / SUBAGENTS
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                self.issues.append(
                    {
                        "source": path.relative_to(self.context.logs_dir).as_posix(),
                        "reason": "malformed_json",
                        "detail": str(exc)[:500],
                    }
                )
                continue
            if not isinstance(value, dict):
                continue
            child, index = value.get("conversationId"), value.get("spawnStepIndex")
            if isinstance(child, str) and isinstance(index, int) and not isinstance(index, bool):
                spawned.setdefault(index, []).append((child, path))
        return spawned

    def _content(self, record: dict[str, Any]) -> str | list[AtifContentPart]:
        text = _text(record.get("content"))
        images: list[AtifContentPart] = []
        shown: set[bytes] = set()
        for item in record.get("media") or ():
            if not isinstance(item, dict):
                continue
            media_type, path = item.get("mime_type"), self._mirrored(item.get("uri"))
            if media_type not in _IMAGE_TYPES or path is None:
                self.issues.append(
                    {"reason": "media_unavailable", "step_index": record.get("step_index")}
                )
                continue
            shown.add(path.read_bytes())
            blob = self.context.blobs.put(path.read_bytes(), media_type=media_type)
            images.append(
                AtifContentPart(
                    type="image", source=AtifImageSource(media_type=media_type, path=blob.path)
                )
            )
        # An image agy saved instead of inlining is still the result: the model got its path.
        for uri in _OFFLOADED.findall(text):
            path = self._mirrored(uri)
            media_type = _SUFFIX_TYPES.get(path.suffix.lower()) if path else None
            if path is None or media_type is None or path.read_bytes() in shown:
                continue
            blob = self.context.blobs.put(path.read_bytes(), media_type=media_type)
            images.append(
                AtifContentPart(
                    type="image", source=AtifImageSource(media_type=media_type, path=blob.path)
                )
            )
        if not images:
            return text
        return [*([AtifContentPart(type="text", text=text)] if text else []), *images]

    def _mirrored(self, uri: Any) -> Path | None:
        """The mirrored copy of a file agy names by its absolute in-Sandbox URI."""
        if not isinstance(uri, str) or not uri.startswith("file:"):
            return None
        native = unquote(urlparse(uri).path)
        anchor = native.find(f"/{BRAIN}/")
        if anchor < 0:
            return None
        relative = PurePosixPath(native[anchor + 1 :])
        if ".." in relative.parts:
            return None
        path = self.context.logs_dir / relative
        return path if path.is_file() else None


class _Turn:
    """One planner response, held until the tool results that follow it are known."""

    def __init__(self, builder: TrajectoryBuilder) -> None:
        self.builder = builder
        self.step: dict[str, Any] | None = None
        self.calls: list[AtifToolCall] = []
        self.results: list[AtifObservationResult] = []
        self.pending: list[tuple[str, str]] = []

    def open(self, record: dict[str, Any], **data: Any) -> None:
        self.calls = [
            _call(record.get("step_index"), index, raw)
            for index, raw in enumerate(record.get("tool_calls") or ())
            if isinstance(raw, dict)
        ]
        self.step = {
            "message": _text(record.get("content")),
            "reasoning_content": _text(record.get("thinking")) or None,
            **data,
        }
        self.results = []
        # Results carry no call ID; they follow their planner response in call order.
        self.pending = [(call.tool_call_id, _native_name(call)) for call in self.calls]

    def flush(self) -> None:
        if self.step is None:
            return
        self.builder.add(
            source="agent",
            tool_calls=self.calls or None,
            observation=AtifObservation(results=self.results) if self.results else None,
            **self.step,
        )
        self.step, self.calls, self.results, self.pending = None, [], [], []


def _records(
    context: TrajectoryParseContext, path: Path, issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Step records in step order; a later snapshot of a step replaces an earlier one."""
    events, problems = native_events(context, path)
    issues.extend(problems)
    latest: dict[tuple[int, str, str], dict[str, Any]] = {}
    for event in events:
        index = event.get("step_index")
        if isinstance(index, int) and not isinstance(index, bool):
            latest[(index, _type(event), str(event.get("source") or ""))] = event
    return [latest[key] for key in sorted(latest, key=lambda key: key[0])]


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", "replace")


def _plain(text: str) -> str:
    """Text compared across copies: agy writes CRLF in some and LF in others."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _result(events: list[dict[str, Any]]) -> NativeResult:
    session_id: str | None = None
    for event in events:
        value = event.get("conversation_id")
        if event.get("event") == "init" and isinstance(value, str) and value:
            session_id = value
    result = next(
        (
            event["result"]
            for event in reversed(events)
            if event.get("event") == "result" and isinstance(event.get("result"), dict)
        ),
        None,
    )
    if result is None:
        return NativeResult(session_id, None, "agy ended without a result event")
    value = result.get("conversation_id")
    session_id = value if isinstance(value, str) and value else session_id
    response = result.get("response")
    final = response.strip() if isinstance(response, str) and response.strip() else None
    status = str(result.get("status") or "")
    error = None
    if status != "SUCCESS":
        error = error_text(result.get("error")) or f"agy finished with status {status or '?'}"
    return NativeResult(session_id, final, error)


def _tool_events(stream: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """The root conversation's tool steps by step index, as stdout reported them."""
    tools: dict[int, dict[str, Any]] = {}
    for event in stream:
        update = event.get("step_update")
        if event.get("event") != "step_update" or not isinstance(update, dict):
            continue
        index = update.get("step_index")
        if update.get("step_type") in {"tool", "subagent"} and isinstance(index, int):
            tools[index] = update
    return tools


def _failure(builder: TrajectoryBuilder, events: list[dict[str, Any]]) -> None:
    """A failure only the invocation's stdout reports, at the end of its turn."""
    if any(event.get("event") == "result" for event in events) and (error := _result(events).error):
        builder.add(source="system", message=error)


def _call(step_index: Any, index: int, raw: dict[str, Any]) -> AtifToolCall:
    name = str(raw.get("name") or "")
    arguments = raw.get("args") if isinstance(raw.get("args"), dict) else {"value": raw.get("args")}
    call_id = f"step-{step_index}-call-{index}"
    server, tool = arguments.get("ServerName"), arguments.get("ToolName")
    if name == "call_mcp_tool" and isinstance(server, str) and isinstance(tool, str):
        # The lazy-MCP wrapper has no result of its own, so the call is the MCP call.
        inner = arguments.get("Arguments")
        return AtifToolCall(
            tool_call_id=call_id,
            function_name=f"mcp__{server}__{tool}",
            arguments=inner if isinstance(inner, dict) else {"value": inner},
            extra={"orbit": {"mcp": {"server": server, "tool": tool}, "native_tool": name}},
        )
    return AtifToolCall(tool_call_id=call_id, function_name=name, arguments=dict(arguments))


def _native_name(call: AtifToolCall) -> str:
    native = ((call.extra or {}).get("orbit") or {}).get("native_tool")
    return native if isinstance(native, str) else call.function_name


def _fields(data: bytes | None) -> dict[int, list[int | bytes]] | None:
    """One protobuf message's fields by number: a varint as an int, any other value as its
    bytes; ``None`` if ``data`` is not a message."""
    if data is None:
        return None
    fields: dict[int, list[int | bytes]] = {}
    index = 0
    try:
        while index < len(data):
            key, index = _varint(data, index)
            wire = key & 7
            if wire == 0:
                value, index = _varint(data, index)
                fields.setdefault(key >> 3, []).append(value)
                continue
            if wire == 2:
                size, index = _varint(data, index)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
            else:
                return None
            if index + size > len(data):
                return None
            fields.setdefault(key >> 3, []).append(data[index : index + size])
            index += size
    except IndexError:
        return None
    return fields


def _varint(data: bytes, index: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, index
        shift += 7


def _first(fields: dict[int, list[int | bytes]], number: int) -> bytes | None:
    """A field's first value, if it holds bytes."""
    value = next(iter(fields.get(number, ())), None)
    return value if isinstance(value, bytes) else None


def _proto_time(timestamp: dict[int, list[int | bytes]]) -> str | None:
    """A ``google.protobuf.Timestamp``, seconds and nanoseconds since the epoch in UTC, in
    ISO 8601 to the microsecond."""
    seconds = next(iter(timestamp.get(_SECONDS, ())), None)
    nanos = next(iter(timestamp.get(_NANOS, ())), 0)
    if not isinstance(seconds, int) or not isinstance(nanos, int):
        return None
    return epoch_time(seconds * 1_000_000 + nanos // 1000, 1_000_000)


def _agent(context: TrajectoryParseContext) -> AtifAgent:
    return AtifAgent(
        name="antigravity-cli", version=context.agent_version, model_name=context.model or None
    )


def _type(record: dict[str, Any]) -> str:
    return str(record.get("type") or "").upper()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _user_request(content: str) -> str:
    match = _REQUEST.search(content)
    return match.group(1) if match else content


def _json(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _parse_extra(
    context: TrajectoryParseContext,
    records: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    if issues:
        data["parse_issues"] = issues
    if not records:
        data["limitations"] = [
            "Native step records are unavailable; prompts and stdout results only."
        ]
    return {"orbit": data} if data else None
