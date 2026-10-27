"""Pure conversion of Antigravity CLI evidence to ATIF."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from ale.core.harness import TrajectoryParseContext
from ale.core.trajectory import (
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
from ale.run.harnesses._diagnostics import error_text
from ale.run.harnesses.base import (
    Invocation,
    NativeResult,
    invocations,
    mirrored_files,
    native_events,
    requested_session_id,
    stdout_events,
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
_TRUNCATED = re.compile(r"\n<truncated \d+ bytes>\n")
#: Bookkeeping records that are neither conversation nor tool output.
_INTERNAL = frozenset({"CHECKPOINT", "CONVERSATION_HISTORY", "EPHEMERAL_MESSAGE"})
_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


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
    used: set[Path] = set()
    records: list[dict[str, Any]] = []
    transcript = context.logs_dir / BRAIN / str(expected) / TRANSCRIPT if expected else None
    if transcript is not None and transcript.is_file():
        records = _records(context, transcript, issues)
        used.add(transcript)
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
    conversation = _Conversation(context, issues, used)
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=expected,
        agent=_agent(context),
    )
    subagents = conversation.walk(builder, expected, records, recorded=recorded, turns=turns)
    if records:
        # A conversation no spawn record links to its spawning call stays raw evidence.
        for path in sorted((context.logs_dir / BRAIN).glob(f"*/{TRANSCRIPT}")):
            if path.parents[2].name not in conversation.visited:
                conversation.unlinked(path)
    files = [
        path
        for root in (BRAIN, CONVERSATIONS)
        for path in mirrored_files(context, root)
        if path not in used
    ]
    issues.extend(unconverted(context, files))
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    builder.extra = _parse_extra(context, records, issues)
    return builder.build(subagent_trajectories=subagents or None)


class _Conversation:
    """Converts conversations; a subagent is embedded where its parent spawned it."""

    def __init__(
        self, context: TrajectoryParseContext, issues: list[dict[str, Any]], used: set[Path]
    ) -> None:
        self.context = context
        self.issues = issues
        self.used = used
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
            timestamp = _timestamp(record.get("created_at"))
            if kind == "PLANNER_RESPONSE":
                turn.flush()
                turn.open(
                    record,
                    timestamp=timestamp,
                    model_name=self.context.model or None,
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
        if session_id and records:
            self.used.update(_covered(self.context, session_id, records, self.used))
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
        ale: dict[str, Any] = {}
        if str(record.get("status") or "").upper() in {"ERROR", "FAILED"}:
            ale["is_error"] = True
            if error := error_text(record.get("error")):
                ale["error"] = error
        if session_id and isinstance(step_index, int):
            output = self.context.logs_dir / BRAIN / session_id / STEP_OUTPUT.format(step_index)
            text = _read(output) if output.is_file() else ""
            if text and _plain(text) not in _plain(_text(record.get("content"))):
                # Written after the record, as when a background task ends: the result keeps
                # what the model received, and the tool's final output is kept beside it.
                ale["final_output"] = text
                self.used.add(output)
        children: list[AtifTrajectory] = []
        refs: list[AtifSubagentTrajectoryRef] = []
        for child_id, spawn in spawned.get(step_index, ()) if isinstance(step_index, int) else ():
            child = self._subagent(parent_id, child_id, spawn, session_id)
            if child is None:
                continue
            self.used.add(spawn)
            children.append(child)
            refs.append(
                AtifSubagentTrajectoryRef(trajectory_id=child.trajectory_id, session_id=child_id)
            )
        content = self._content(record)
        return (
            AtifObservationResult(
                source_call_id=call_id,
                content=content if content else (ale.get("error") or ""),
                subagent_trajectory_ref=refs or None,
                extra={"ale": ale} if ale else None,
            ),
            children,
        )

    def _subagent(
        self, parent_id: str, child_id: str, spawn: Path, parent_session: str | None
    ) -> AtifTrajectory | None:
        """The conversation a spawn record names, once; ``None`` leaves it raw evidence."""
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
        self.used.add(transcript)
        return builder.build(subagent_trajectories=nested or None)

    def unlinked(self, path: Path) -> None:
        """Report a child that stays raw evidence, once."""
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
        for item in record.get("media") or ():
            if not isinstance(item, dict):
                continue
            media_type, path = item.get("mime_type"), self._mirrored(item.get("uri"))
            if media_type not in _IMAGE_TYPES or path is None:
                self.issues.append(
                    {"reason": "media_unavailable", "step_index": record.get("step_index")}
                )
                continue
            self.used.add(path)
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
            "metrics": _metrics(record),
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


def _covered(
    context: TrajectoryParseContext, session_id: str, records: list[dict[str, Any]], used: set[Path]
) -> set[Path]:
    """A conversation's other files whose whole content the converted records already hold.

    agy keeps copies beside its full transcript: an abridged transcript, both split into
    chunks, each tool's output, the messages delivered to it, its background task logs and
    the subagent definitions it wrote. A copy is consumed only when every byte of it is
    accounted for, so a task's later output or a truncated record stays raw evidence.
    """
    folder = context.logs_dir / BRAIN / session_id
    generated = folder / ".system_generated"
    covered: set[Path] = set()
    contents = [_plain(_text(record.get("content"))) for record in records]
    everything = "\n".join(contents)
    delivered = "\n".join(
        _plain(_text(record.get("content")))
        for record in records
        if _type(record) in {"SYSTEM_MESSAGE", "USER_INPUT"}
    )
    full = _lines(generated / "logs" / "transcript_full.jsonl")
    abridged_path = generated / "logs" / "transcript.jsonl"
    abridged = _lines(abridged_path)
    if abridged_path.is_file() and _derivable(abridged, full):
        covered.add(abridged_path)
        covered.update(_chunks(generated / "logs" / "chunks" / "transcript", abridged))
    covered.update(_chunks(generated / "logs" / "chunks" / "transcript_full", full))
    steps = generated / "steps"
    for path in sorted(steps.glob("*/*")) if steps.is_dir() else ():
        step = path.parent.name
        if path.name == "output.txt":
            text = _plain(_read(path))
            if any(
                text in content
                for record, content in zip(records, contents, strict=True)
                if str(record.get("step_index")) == step
            ):
                covered.add(path)
        elif match := re.fullmatch(r"media_(\d+)(\.\w+)", path.name):
            # The same bytes as the logs/media copy the result's image was read from.
            twin = generated / "logs" / "media" / f"step_{step}_media_{match[1]}{match[2]}"
            if twin in used and twin.read_bytes() == path.read_bytes():
                covered.add(path)
    messages = generated / "messages"
    for path in sorted(messages.glob("*.json")) if messages.is_dir() else ():
        value = _load(path)
        if path.name == "read.json":
            # Read receipts: which of the messages beside it were delivered, nothing more.
            if isinstance(value, dict) and all(
                (messages / f"{key}.json").is_file() for key in value
            ):
                covered.add(path)
        elif (
            isinstance(value, dict)
            and isinstance(value.get("content"), str)
            and f"content={_plain(value['content'])}" in delivered
        ):
            covered.add(path)
    tasks = generated / "tasks"
    for path in sorted(tasks.glob("*.log")) if tasks.is_dir() else ():
        if _plain(_read(path)) in everything:
            covered.add(path)
    database = context.logs_dir / CONVERSATIONS / f"{session_id}.db"
    steps_held = _database_steps(database)
    if steps_held is not None and steps_held <= len(
        {record.get("step_index") for record in records}
    ):
        # Its steps table holds the same step records; everything else in it is agy's
        # internal state or the model request the Gateway records. More steps than the
        # transcript means a turn the transcript lost, so it then stays raw evidence.
        covered.add(database)
        shared = database.with_name(database.name + "-shm")
        log = database.with_name(database.name + "-wal")
        covered.update(path for path in (shared,) if path.is_file())
        covered.update(path for path in (log,) if path.is_file() and not path.stat().st_size)
    definitions = {
        str(call["args"].get("Name")): call["args"]
        for record in records
        for call in record.get("tool_calls") or ()
        if isinstance(call, dict)
        and call.get("name") == "define_subagent"
        and isinstance(call.get("args"), dict)
    }
    for path in sorted((folder / ".agents" / "agents").glob("*/agent.md")):
        args = definitions.get(path.parent.name)
        text = _plain(_read(path))
        if args is not None and all(
            _plain(str(args.get(field) or "")) in text for field in ("Description", "SystemPrompt")
        ):
            covered.add(path)
    return covered


def _database_steps(path: Path) -> int | None:
    """How many steps a conversation database holds, read without writing beside it."""
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            (count,) = connection.execute("select count(*) from steps").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return count if isinstance(count, int) else None


def _derivable(abridged: list[str], full: list[str]) -> bool:
    """Whether every abridged record is a full record with arguments as JSON text, or cut."""
    complete: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for line in full:
        if (record := _json(line)) is not None:
            complete.setdefault(_key(record), []).append(record)
    for line in abridged:
        record = _json(line)
        if record is None or not any(
            _abridges(record, candidate) for candidate in complete.get(_key(record), ())
        ):
            return False
    return True


def _abridges(short: dict[str, Any], full: dict[str, Any]) -> bool:
    cut = set(short.get("truncated_fields") or ())
    for name in set(short) | set(full):
        value, original = short.get(name), full.get(name)
        if name == "truncated_fields" or value == original:
            continue
        if name == "tool_calls":
            if not _same_calls(value, original):
                return False
        elif name in cut and isinstance(value, str) and isinstance(original, str):
            head, *rest = _TRUNCATED.split(value)
            if not (original.startswith(head) and original.endswith(rest[-1] if rest else head)):
                return False
        else:
            return False
    return True


def _same_calls(short: Any, full: Any) -> bool:
    if not isinstance(short, list) or not isinstance(full, list) or len(short) != len(full):
        return False
    for call, original in zip(short, full, strict=True):
        if not isinstance(call, dict) or not isinstance(original, dict):
            return False
        args, expected = call.get("args") or {}, original.get("args") or {}
        if call.get("name") != original.get("name") or set(args) != set(expected):
            return False
        for name, value in args.items():
            decoded = _json_value(value) if isinstance(value, str) else value
            if decoded != expected[name] and value != expected[name]:
                return False
    return True


def _chunks(folder: Path, lines: list[str]) -> set[Path]:
    """Chunk files every line of which is a line of the transcript they split."""
    whole = set(lines)
    return {
        path
        for path in (sorted(folder.glob("*.jsonl")) if folder.is_dir() else ())
        if set(_lines(path)) <= whole
    }


def _key(record: dict[str, Any]) -> tuple[Any, ...]:
    return (record.get("step_index"), _type(record), str(record.get("source") or ""))


def _lines(path: Path) -> list[str]:
    return [line for line in _read(path).splitlines() if line.strip()] if path.is_file() else []


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", "replace")


def _load(path: Path) -> Any:
    return _json_value(_read(path))


def _json_value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


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
            extra={"ale": {"mcp": {"server": server, "tool": tool}, "native_tool": name}},
        )
    return AtifToolCall(tool_call_id=call_id, function_name=name, arguments=dict(arguments))


def _native_name(call: AtifToolCall) -> str:
    native = ((call.extra or {}).get("ale") or {}).get("native_tool")
    return native if isinstance(native, str) else call.function_name


def _metrics(record: dict[str, Any]) -> AtifMetrics | None:
    """agy reports uncached input apart from cache reads; ATIF's prompt includes both."""
    fields = ("input_tokens", "cache_read_tokens", "output_tokens")
    values = {name: record.get(name) for name in fields}
    if not any(isinstance(value, int) and not isinstance(value, bool) for value in values.values()):
        return None
    counts = {
        name: value if isinstance(value, int) and not isinstance(value, bool) else 0
        for name, value in values.items()
    }
    return AtifMetrics(
        prompt_tokens=counts["input_tokens"] + counts["cache_read_tokens"],
        completion_tokens=counts["output_tokens"],
        cached_tokens=counts["cache_read_tokens"],
    )


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


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


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
    return {"ale": data} if data else None
