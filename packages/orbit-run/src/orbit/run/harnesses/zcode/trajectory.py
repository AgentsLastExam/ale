"""Pure conversion of ZCode evidence to ATIF."""

from __future__ import annotations

import base64
import binascii
import json
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ale.core.harness import TrajectoryParseContext
from ale.core.trajectory import (
    AtifAgent,
    AtifContentPart,
    AtifFinalMetrics,
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
    NativeResult,
    invocations,
    json_lines,
    requested_session_id,
    stdout_events,
)

DB_DIR = ".zcode/cli/db"
DB = f"{DB_DIR}/db.sqlite"
ARTIFACTS = ".zcode/cli/artifacts"
AGENTS = ".zcode/cli/agents"
#: Where the adapter keeps the provider file holding the episode token; never evidence.
PRIVATE = ".zcode-ale"
_IMAGE_TYPES = frozenset(("image/png", "image/jpeg", "image/gif", "image/webp"))
_DATA_URL = re.compile(r"data:([^;,]+);base64,(.*)", re.DOTALL)


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    # A session's attachments and subagents live in directories named by its ID; its
    # conversation is in the shared SQLite store.
    return any(
        path.is_relative_to(root)
        and path.relative_to(root).parts[:1] == (session_id,)
        and len(path.relative_to(root).parts) > 1
        for root in (ARTIFACTS, AGENTS)
    )


def read_result(stdout: str) -> NativeResult:
    events, _ = json_lines(stdout)
    result = _result(events)
    final = result.get("response") if result else None
    return NativeResult(
        result.get("sessionId") if result and isinstance(result.get("sessionId"), str) else None,
        final if isinstance(final, str) and final else None,
        _turn_error(events),
    )


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    turns, issues = stdout_events(recorded)
    expected = requested_session_id(context, recorded) or next(
        (
            event["sessionId"]
            for turn in reversed(turns)
            for event in reversed(turn)
            if event.get("type") == "result" and isinstance(event.get("sessionId"), str)
        ),
        None,
    )
    store = _read_store(context.logs_dir / DB, issues)
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id, session_id=expected, agent=_agent(context)
    )
    totals = {"input": 0, "output": 0, "cached": 0}
    subagents: list[AtifTrajectory] = []
    messages = store.messages.get(expected or "", []) if store else []
    if store is not None and expected in store.parents and store.parents[expected] is None:
        conversation = _Conversation(context, store, issues, set())
        subagents = conversation.walk(builder, expected, turns=turns, totals=totals)
        # Every descendant, so a child under an unlinked one is reported rather than lost.
        issues.extend(
            {"reason": "unlinked_subagent", "session_id": child}
            for child in sorted(store.parents)
            if child not in conversation.linked and _descends(store.parents, child, expected)
        )
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
        messages = []
    # Each Task invocation is one prompt in order; any the session lacks is rebuilt from
    # its own prompt and stdout.
    native_turns = sum(1 for message in messages if _is_prompt(message, root=True))
    if messages and native_turns < len(recorded):
        issues.append({"reason": "prompt_not_in_native_session", "invocation": native_turns + 1})
    for item, stream in zip(recorded[native_turns:], turns[native_turns:], strict=True):
        builder.add(source="user", message=item.prompt)
        result = _result(stream)
        if result and isinstance(result.get("response"), str) and result["response"]:
            builder.add(source="agent", message=result["response"])
        if error := _turn_error(stream):
            builder.add(source="system", message=error)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    final_metrics = (
        AtifFinalMetrics(
            total_prompt_tokens=totals["input"],
            total_completion_tokens=totals["output"],
            total_cached_tokens=totals["cached"],
            total_steps=len(builder.steps),
        )
        if any(totals.values())
        else None
    )
    trajectory = builder.build(final_metrics=final_metrics, subagent_trajectories=subagents or None)
    extra = _extra(context, issues, has_session=bool(messages))
    return trajectory.model_copy(update={"extra": extra}) if extra else trajectory


def _descends(parents: dict[str, str | None], session: str, root: str) -> bool:
    """Whether ``root`` is among ``session``'s ancestors in the store."""
    seen: set[str] = set()
    parent = parents.get(session)
    while parent is not None and parent not in seen:
        if parent == root:
            return True
        seen.add(parent)
        parent = parents.get(parent)
    return False


@dataclass
class _Message:
    role: str
    data: dict[str, Any]
    parts: list[dict[str, Any]]


@dataclass
class _Store:
    """The sessions in ZCode's store: each one's parent, and its messages in order."""

    parents: dict[str, str | None]
    messages: dict[str, list[_Message]]


def _read_store(path: Path, issues: list[dict[str, Any]]) -> _Store | None:
    """Read the mirrored store through a private copy, so the mirror is never touched.

    The copy takes the write-ahead log along: SQLite folds in what a stopped process left
    there.
    """
    if not path.is_file():
        return None
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / path.name
        shutil.copyfile(path, copy)
        wal = path.with_name(path.name + "-wal")
        if wal.is_file():
            shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        connection = sqlite3.connect(copy)
        try:
            parents = dict(connection.execute("select id, parent_id from session"))
            ordered: dict[str, list[_Message]] = {}
            by_id: dict[str, _Message] = {}
            for message_id, session_id, data in connection.execute(
                "select id, session_id, data from message order by session_id, sequence, id"
            ):
                value = _json(data)
                message = _Message(str(value.get("role") or ""), value, [])
                by_id[message_id] = message
                ordered.setdefault(session_id, []).append(message)
            for message_id, data in connection.execute(
                "select message_id, data from part order by message_id, sequence, id"
            ):
                if message_id in by_id:
                    by_id[message_id].parts.append(_json(data))
        except sqlite3.Error as error:
            issues.append({"reason": "native_store_unreadable", "detail": str(error)[:500]})
            return None
        finally:
            connection.close()
    return _Store(parents, ordered)


@dataclass
class _Step:
    source: str
    message: str = ""
    timestamp: str | None = None
    reasoning: list[str] = field(default_factory=list)
    calls: list[AtifToolCall] = field(default_factory=list)
    results: list[AtifObservationResult] = field(default_factory=list)
    metrics: AtifMetrics | None = None


@dataclass
class _Conversation:
    """Converts a session; a subagent is embedded in the session whose call spawned it."""

    context: TrajectoryParseContext
    store: _Store
    issues: list[dict[str, Any]]
    linked: set[str]

    def walk(
        self,
        builder: TrajectoryBuilder,
        session_id: str,
        *,
        turns: list[list[dict[str, Any]]] | None = None,
        totals: dict[str, int] | None = None,
    ) -> list[AtifTrajectory]:
        """Add one session's conversation to ``builder``; return its embedded subagents.

        ``turns`` holds the root's per-invocation stdout, whose failures the store lacks;
        each is added at the end of its turn.
        """
        messages = self.store.messages.get(session_id, [])
        embedded, refs = self._children(builder.trajectory_id, session_id, messages)
        root = turns is not None
        steps: list[_Step] = []
        prompts = 0
        for message in messages:
            created = (message.data.get("time") or {}).get("created")
            if _is_prompt(message, root=root):
                if turns is not None and prompts and (error := _turn_error(turns[prompts - 1])):
                    steps.append(_Step("system", error))
                prompts += 1
                steps.append(_Step("user", _text(message.parts), _timestamp(created)))
                continue
            if message.role != "assistant":
                continue
            step = _Step("agent", timestamp=_timestamp(created))
            texts: list[str] = []
            for part in message.parts:
                kind = part.get("type")
                if kind == "text":
                    texts.append(str(part.get("text") or ""))
                elif kind == "reasoning":
                    step.reasoning.append(str(part.get("text") or ""))
                elif kind == "tool":
                    call, result = self._tool(part, session_id, refs)
                    step.calls.append(call)
                    if result is not None:
                        step.results.append(result)
                elif kind == "step-finish":
                    step.metrics = _metrics(part.get("tokens"), totals)
            step.message = "".join(texts)
            steps.append(step)
        if turns is not None and prompts and (error := _turn_error(turns[prompts - 1])):
            steps.append(_Step("system", error))
        for step in steps:
            _emit(builder, step)
        return embedded

    def _tool(
        self,
        part: dict[str, Any],
        session_id: str,
        refs: dict[str, list[AtifSubagentTrajectoryRef]],
    ) -> tuple[AtifToolCall, AtifObservationResult | None]:
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        call_id = str(part.get("callID") or "")
        name = str(part.get("tool") or "")
        arguments = state.get("input")
        display = (state.get("metadata") or {}).get("display") or {}
        extra = None
        if display.get("kind") == "mcp_tool":
            extra = {
                "ale": {
                    "mcp": {"server": display.get("serverName"), "tool": display.get("toolName")}
                }
            }
        call = AtifToolCall(
            tool_call_id=call_id,
            function_name=name,
            arguments=arguments if isinstance(arguments, dict) else {"value": arguments},
            extra=extra,
        )
        status = state.get("status")
        if status not in ("completed", "error"):
            # A call the store holds no outcome for is not recorded as having succeeded.
            return call, None
        output = state.get("output")
        text = output if isinstance(output, str) else error_text(state.get("error"))
        content: str | list[AtifContentPart] = text or ""
        media = [
            self._attachment(item, session_id)
            for item in state.get("attachments") or ()
            if isinstance(item, dict)
        ]
        if media:
            content = [AtifContentPart(type="text", text=text or ""), *media]
        return call, AtifObservationResult(
            source_call_id=call_id,
            content=content,
            subagent_trajectory_ref=refs.get(call_id),
            extra={"ale": {"is_error": True}} if status == "error" else None,
        )

    def _attachment(self, item: dict[str, Any], session_id: str) -> AtifContentPart:
        """An attachment ZCode keeps as a data URL in the session's artifacts directory."""
        url = str(item.get("url") or "")
        name = url.removeprefix(f"zcode-artifact://{session_id}/")
        media_type, data = item.get("mime"), None
        directory = self.context.logs_dir / ARTIFACTS / session_id
        candidates = (
            sorted(path for path in directory.glob(f"*{name}.txt") if path.is_file())
            if name and name != url and "/" not in name and directory.is_dir()
            else []
        )
        if len(candidates) == 1 and (
            match := _DATA_URL.fullmatch(candidates[0].read_text().strip())
        ):
            media_type = match.group(1)
            try:
                data = base64.b64decode(match.group(2), validate=False)
            except (binascii.Error, ValueError):
                data = None
        if data is None or media_type not in _IMAGE_TYPES:
            self.issues.append({"reason": "missing_attachment", "reference": url[:200]})
            return AtifContentPart(type="text", text=f"[attachment unavailable: {url[:200]}]")
        stored = self.context.blobs.put(data, media_type=media_type)
        return AtifContentPart(
            type="image", source=AtifImageSource(media_type=media_type, path=stored.path)
        )

    def _children(
        self, trajectory_id: str, session_id: str, messages: list[_Message]
    ) -> tuple[list[AtifTrajectory], dict[str, list[AtifSubagentTrajectoryRef]]]:
        """Subagents a session spawned, keyed by the spawning call.

        Each subagent's ``metadata.json`` names its parent session, the spawning call and
        its child session, whose row in the store names the same parent. The reference sits
        on that call's result, so a spawn whose call has no outcome stays unlinked.
        """
        answered = {
            str(part.get("callID"))
            for message in messages
            for part in message.parts
            if part.get("type") == "tool"
            and (part.get("state") or {}).get("status") in ("completed", "error")
        }
        directory = self.context.logs_dir / AGENTS / session_id
        embedded: list[AtifTrajectory] = []
        refs: dict[str, list[AtifSubagentTrajectoryRef]] = {}
        for path in sorted(directory.glob("*/metadata.json")) if directory.is_dir() else ():
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            call_id, child_id = meta.get("parentToolUseId"), meta.get("childSessionId")
            if (
                meta.get("parentSessionId") != session_id
                or not isinstance(call_id, str)
                or call_id not in answered
                or not isinstance(child_id, str)
                or self.store.parents.get(child_id) != session_id
                or child_id in self.linked
            ):
                continue
            self.linked.add(child_id)
            child = TrajectoryBuilder(
                trajectory_id=f"{trajectory_id}-subagent-{child_id}",
                session_id=child_id,
                agent=_agent(self.context),
                extra=_extra(self.context, [], has_session=True),
            )
            nested = self.walk(child, child_id)
            if not child.steps:
                self.linked.discard(child_id)
                continue
            embedded.append(child.build(subagent_trajectories=nested or None))
            refs.setdefault(call_id, []).append(
                AtifSubagentTrajectoryRef(trajectory_id=child.trajectory_id, session_id=child_id)
            )
        return embedded, refs


def _emit(builder: TrajectoryBuilder, step: _Step) -> None:
    if step.source != "agent":
        builder.add(source=step.source, message=step.message, timestamp=step.timestamp)
        return
    reasoning = "".join(step.reasoning).strip() or None
    if not (step.message or reasoning or step.calls):
        return
    builder.add(
        source="agent",
        message=step.message,
        timestamp=step.timestamp,
        reasoning_content=reasoning,
        tool_calls=step.calls or None,
        observation=AtifObservation(results=step.results) if step.results else None,
        metrics=step.metrics,
    )


def _is_prompt(message: _Message, *, root: bool) -> bool:
    """A prompt sent to the session: ALE's to the root, the parent's to a subagent.

    ZCode also records context it injects, such as its agent listing, as model-only user
    messages; those are not turns.
    """
    if message.role != "user":
        return False
    metadata = message.data.get("metadata") or {}
    if metadata.get("visibility") == "model-only":
        return False
    return not root or "source" not in metadata


def _text(parts: list[dict[str, Any]]) -> str:
    return "".join(str(part.get("text") or "") for part in parts if part.get("type") == "text")


def _metrics(tokens: Any, totals: dict[str, int] | None) -> AtifMetrics | None:
    if not isinstance(tokens, dict):
        return None

    def count(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    cached = count(cache.get("read"))
    # Input includes prompt-cache reads and writes, as the Gateway accounts it.
    prompt = count(tokens.get("input")) + cached + count(cache.get("write"))
    output = count(tokens.get("output"))
    if totals is not None:
        totals["input"] += prompt
        totals["output"] += output
        totals["cached"] += cached
    return AtifMetrics(prompt_tokens=prompt, completion_tokens=output, cached_tokens=cached)


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _json(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value) if isinstance(value, str | bytes) else None
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((event for event in reversed(events) if event.get("type") == "result"), None)


def _turn_error(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") == "result":
            return None
        if event.get("type") == "turn.failed":
            error = (event.get("payload") or {}).get("error")
            if isinstance(error, dict):
                return (
                    error_text(
                        {
                            "message": error.get("underlyingErrorMessage") or error.get("message"),
                            "code": error.get("code"),
                        }
                    )
                    or "ZCode turn failed"
                )
            return error_text(error) or "ZCode turn failed"
    return None


def _agent(context: TrajectoryParseContext) -> AtifAgent:
    return AtifAgent(name="zcode", version=context.agent_version, model_name=context.model or None)


def _extra(
    context: TrajectoryParseContext, issues: list[dict[str, Any]], *, has_session: bool
) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    if issues:
        data["parse_issues"] = issues
    if not has_session:
        data["limitations"] = [
            "Native tool and conversation records are unavailable; stdout text only."
        ]
    return {"ale": data} if data else None
