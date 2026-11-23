"""Pure conversion of Kimi Code evidence to ATIF."""

from __future__ import annotations

import base64
import binascii
import json
import re
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
    mirrored_files,
    native_events,
    requested_session_id,
    stdout_events,
    unconverted,
)

HOME = ".kimi-ale"
SESSIONS = f"{HOME}/sessions"
MAIN = "main"
WIRE = "wire.jsonl"
_IMAGE_TYPES = frozenset(("image/png", "image/jpeg", "image/gif", "image/webp"))
_DATA_URL = re.compile(r"data:([^;,]+);base64,(.*)", re.DOTALL)


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    # sessions/<work directory bucket>/<session id>/...
    if not path.is_relative_to(SESSIONS):
        return False
    parts = path.relative_to(SESSIONS).parts
    return len(parts) > 2 and parts[1] == session_id


def read_result(stdout: str) -> NativeResult:
    events, _ = json_lines(stdout)
    return NativeResult(_session_id(events), _final_message(events), _native_error(events))


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    turns, issues = stdout_events(recorded)
    expected = requested_session_id(context, recorded) or _session_id(
        [event for turn in turns for event in turn]
    )
    files = mirrored_files(context, SESSIONS)
    root = context.logs_dir / SESSIONS
    directories = sorted(
        {
            root.joinpath(*PurePosixPath(path.relative_to(root).as_posix()).parts[:2])
            for path in files
            if expected
            and is_session_file(
                PurePosixPath(path.relative_to(context.logs_dir).as_posix()), expected
            )
        }
    )
    session = directories[0] if len(directories) == 1 else None
    wire = session / "agents" / MAIN / WIRE if session else None
    events: list[dict[str, Any]] = []
    used: set[Path] = set()
    if wire is not None and wire.is_file():
        events, native_issues = native_events(context, wire)
        issues.extend(native_issues)
        used.add(wire)
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})

    agent = AtifAgent(
        name="kimi-code", version=context.agent_version, model_name=context.model or None
    )
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id, session_id=expected, agent=agent
    )
    subagents: list[AtifTrajectory] = []
    totals = {"input": 0, "output": 0, "cached": 0}
    if session is not None and events:
        conversation = _Conversation(context, session, issues, used)
        subagents = conversation.walk(builder, MAIN, events, root=True, totals=totals)
    # Each Task invocation is one user prompt in order; any the session lacks is rebuilt
    # from its own prompt and stdout.
    native_turns = sum(1 for event in events if _is_user_prompt(event, MAIN))
    if events and native_turns < len(recorded):
        issues.append({"reason": "prompt_not_in_native_session", "invocation": native_turns + 1})
    for item, stream in zip(recorded[native_turns:], turns[native_turns:], strict=True):
        builder.add(source="user", message=item.prompt)
        if message := _final_message(stream):
            builder.add(source="agent", message=message)
        if error := _native_error(stream):
            builder.add(source="system", message=error)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    wires = [path for path in files if path.name == WIRE]
    linked = {path for path in used if path.name == WIRE}
    if session is not None:
        issues.extend(
            {
                "reason": "unlinked_subagent",
                "source": path.relative_to(context.logs_dir).as_posix(),
            }
            for path in sorted(set(wires) - linked)
            if path.is_relative_to(session)
        )
    issues.extend(unconverted(context, set(wires) - linked))
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
    extra = _extra(context, issues, has_session=bool(events))
    return trajectory.model_copy(update={"extra": extra}) if extra else trajectory


@dataclass
class _Step:
    """One native step, or a prompt or notice between steps, before it becomes ATIF."""

    source: str
    message: str | list[AtifContentPart] = ""
    timestamp: str | None = None
    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    calls: list[AtifToolCall] = field(default_factory=list)
    results: dict[str, AtifObservationResult] = field(default_factory=dict)
    metrics: AtifMetrics | None = None


@dataclass
class _Conversation:
    """Converts one session's agents; subagents are embedded in the agent that spawned them."""

    context: TrajectoryParseContext
    session: Path
    issues: list[dict[str, Any]]
    used: set[Path]

    def walk(
        self,
        builder: TrajectoryBuilder,
        agent_id: str,
        events: list[dict[str, Any]],
        *,
        root: bool,
        totals: dict[str, int] | None = None,
    ) -> list[AtifTrajectory]:
        """Add one agent's conversation to ``builder``; return the subagents it spawned."""
        embedded, refs = self._children(builder.trajectory_id, agent_id, events)
        steps: list[_Step] = []
        owner: dict[str, _Step] = {}
        current: _Step | None = None
        for event in events:
            kind = event.get("type")
            if not _belongs(event, agent_id):
                continue
            if kind == "turn.prompt":
                current = None
                origin = event.get("origin") or {}
                # In a subagent every prompt is the parent's delegation.
                source = "user" if not root or origin.get("kind") == "user" else "system"
                steps.append(
                    _Step(
                        source,
                        message=self._content(agent_id, event.get("input")),
                        timestamp=_timestamp(event),
                    )
                )
            elif kind == "context.append_loop_event":
                loop = event.get("event") or {}
                loop_kind = loop.get("type")
                if loop_kind == "step.begin":
                    current = _Step("agent", timestamp=_timestamp(event))
                    steps.append(current)
                elif loop_kind == "content.part" and current is not None:
                    part = loop.get("part") or {}
                    if part.get("type") == "text":
                        current.text.append(str(part.get("text") or ""))
                    elif part.get("type") == "think":
                        current.reasoning.append(str(part.get("think") or ""))
                elif loop_kind == "tool.call" and current is not None:
                    call_id = str(loop.get("toolCallId") or "")
                    name = str(loop.get("name") or "")
                    current.calls.append(
                        AtifToolCall(
                            tool_call_id=call_id,
                            function_name=name,
                            arguments=_arguments(loop.get("args")),
                            extra=_mcp_extra(name),
                        )
                    )
                    owner[call_id] = current
                elif loop_kind == "tool.result":
                    call_id = str(loop.get("toolCallId") or "")
                    holder = owner.get(call_id)
                    if holder is None or call_id in holder.results:
                        continue
                    result = loop.get("result") or {}
                    content = self._content(agent_id, result.get("output"))
                    if isinstance(result.get("note"), str) and result["note"]:
                        content = _append_text(content, result["note"])
                    holder.results[call_id] = AtifObservationResult(
                        source_call_id=call_id,
                        content=content,
                        subagent_trajectory_ref=refs.get(call_id),
                        extra={"ale": {"is_error": True}} if result.get("isError") else None,
                    )
                elif loop_kind == "step.end" and current is not None:
                    current.metrics = _metrics(loop.get("usage"), totals)
                    current = None
            elif kind == "turn.ended" and event.get("reason") != "completed":
                current = None
                error = error_text(event.get("error"))
                steps.append(
                    _Step(
                        "system",
                        message=error or f"turn ended: {event.get('reason') or 'unknown'}",
                        timestamp=_timestamp(event),
                    )
                )
            elif kind == "context.apply_compaction":
                current = None
                summary = event.get("summary") or event.get("contextSummary")
                steps.append(
                    _Step(
                        "system",
                        message=(
                            f"Context compacted: {summary}"
                            if isinstance(summary, str) and summary
                            else "Context compacted."
                        ),
                        timestamp=_timestamp(event),
                    )
                )
        for step in steps:
            _emit(builder, step)
        return embedded

    def _children(
        self, trajectory_id: str, agent_id: str, events: list[dict[str, Any]]
    ) -> tuple[list[AtifTrajectory], dict[str, list[AtifSubagentTrajectoryRef]]]:
        """Subagents this agent spawned, keyed by the call that spawned each.

        The spawn record names the calling agent, its tool call and the subagent; the
        subagent's own wire, in the session's ``agents/<id>`` directory, is its
        conversation. A spawn whose call has no result, or whose wire is absent, stays
        unlinked: the reference has to sit on that call's result.
        """
        answered = {
            str((event.get("event") or {}).get("toolCallId"))
            for event in events
            if _belongs(event, agent_id)
            and event.get("type") == "context.append_loop_event"
            and (event.get("event") or {}).get("type") == "tool.result"
        }
        embedded: dict[str, AtifTrajectory] = {}
        refs: dict[str, list[AtifSubagentTrajectoryRef]] = {}
        for event in events:
            if event.get("type") != "subagent.spawned" or event.get("parentAgentId") != agent_id:
                continue
            call_id, child_id = event.get("parentToolCallId"), event.get("subagentId")
            if not isinstance(call_id, str) or not isinstance(child_id, str) or not child_id:
                continue
            if call_id not in answered or child_id == agent_id or "/" in child_id:
                continue
            if child_id not in embedded:
                wire = self.session / "agents" / child_id / WIRE
                if wire in self.used or not wire.is_file():
                    continue
                child_events, problems = native_events(self.context, wire)
                if not any(item.get("agentId") == child_id for item in child_events):
                    continue
                self.used.add(wire)
                self.issues.extend(problems)
                child = TrajectoryBuilder(
                    trajectory_id=f"{trajectory_id}-subagent-{child_id}",
                    session_id=child_id,
                    agent=AtifAgent(
                        name="kimi-code",
                        version=self.context.agent_version,
                        model_name=self.context.model or None,
                    ),
                    extra=_extra(self.context, [], has_session=True),
                )
                nested = self.walk(child, child_id, child_events, root=False)
                if not child.steps:
                    self.used.discard(wire)
                    continue
                embedded[child_id] = child.build(subagent_trajectories=nested or None)
            refs.setdefault(call_id, []).append(
                AtifSubagentTrajectoryRef(
                    trajectory_id=embedded[child_id].trajectory_id, session_id=child_id
                )
            )
        return list(embedded.values()), refs

    def _content(self, agent_id: str, value: Any) -> str | list[AtifContentPart]:
        """Native output or input parts, with media copied into ALE's blob store."""
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            return json.dumps(value, ensure_ascii=False, default=str)
        parts: list[AtifContentPart] = []
        for item in value:
            if not isinstance(item, dict):
                parts.append(AtifContentPart(type="text", text=str(item)))
            elif item.get("type") == "text":
                parts.append(AtifContentPart(type="text", text=str(item.get("text") or "")))
            elif item.get("type") == "image_url":
                image = item.get("imageUrl") or item.get("image_url") or {}
                url = image.get("url") if isinstance(image, dict) else None
                parts.append(self._image(agent_id, url))
            else:
                parts.append(
                    AtifContentPart(type="text", text=json.dumps(item, ensure_ascii=False))
                )
        if all(part.type == "text" for part in parts):
            return "".join(part.text or "" for part in parts)
        return parts

    def _image(self, agent_id: str, url: Any) -> AtifContentPart:
        media_type, data = None, None
        if isinstance(url, str) and url.startswith("blobref:"):
            # blobref:<media type>;<digest>, stored raw in the agent's blobs directory.
            media_type, _, digest = url.removeprefix("blobref:").partition(";")
            blob = self.session / "agents" / agent_id / "blobs" / digest
            if digest and "/" not in digest and blob.is_file():
                data = blob.read_bytes()
                self.used.add(blob)
            else:
                self.issues.append({"reason": "missing_blob", "reference": url[:200]})
        elif isinstance(url, str) and (match := _DATA_URL.fullmatch(url)):
            media_type = match.group(1)
            try:
                data = base64.b64decode(match.group(2), validate=False)
            except (binascii.Error, ValueError):
                data = None
        if data is None or media_type not in _IMAGE_TYPES:
            label = url[:200] if isinstance(url, str) and not url.startswith("data:") else "inline"
            return AtifContentPart(type="text", text=f"[image unavailable: {label}]")
        stored = self.context.blobs.put(data, media_type=media_type)
        return AtifContentPart(
            type="image", source=AtifImageSource(media_type=media_type, path=stored.path)
        )


def _emit(builder: TrajectoryBuilder, step: _Step) -> None:
    if step.source != "agent":
        builder.add(source=step.source, message=step.message, timestamp=step.timestamp)
        return
    message = "".join(step.text)
    reasoning = "".join(step.reasoning).strip() or None
    if not (message or reasoning or step.calls):
        return
    results = [
        step.results[call.tool_call_id] for call in step.calls if call.tool_call_id in step.results
    ]
    builder.add(
        source="agent",
        message=message,
        timestamp=step.timestamp,
        reasoning_content=reasoning,
        tool_calls=step.calls or None,
        observation=AtifObservation(results=results) if results else None,
        metrics=step.metrics,
    )


def _belongs(event: dict[str, Any], agent_id: str) -> bool:
    """Each agent has its own wire; a record naming another agent is not part of it."""
    return event.get("agentId", agent_id) == agent_id


def _is_user_prompt(event: dict[str, Any], agent_id: str) -> bool:
    return (
        event.get("type") == "turn.prompt"
        and _belongs(event, agent_id)
        and (event.get("origin") or {}).get("kind") == "user"
    )


def _metrics(usage: Any, totals: dict[str, int] | None) -> AtifMetrics | None:
    if not isinstance(usage, dict):
        return None
    counts = {
        key: value if isinstance(value, int) else 0
        for key, value in (
            (key, usage.get(key))
            for key in ("inputOther", "inputCacheRead", "inputCacheCreation", "output")
        )
    }
    # Input includes prompt-cache reads and writes, as the Gateway accounts it.
    prompt = counts["inputOther"] + counts["inputCacheRead"] + counts["inputCacheCreation"]
    if totals is not None:
        totals["input"] += prompt
        totals["output"] += counts["output"]
        totals["cached"] += counts["inputCacheRead"]
    return AtifMetrics(
        prompt_tokens=prompt,
        completion_tokens=counts["output"],
        cached_tokens=counts["inputCacheRead"],
    )


def _append_text(content: str | list[AtifContentPart], text: str) -> str | list[AtifContentPart]:
    if isinstance(content, str):
        return f"{content}\n{text}" if content else text
    return [*content, AtifContentPart(type="text", text=text)]


def _timestamp(event: dict[str, Any]) -> str | None:
    value = event.get("time")
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {} if value is None else {"value": value}


def _mcp_extra(name: str) -> dict[str, Any] | None:
    if not name.startswith("mcp__"):
        return None
    server, separator, tool = name.removeprefix("mcp__").partition("__")
    if not separator or not server or not tool:
        return None
    return {"ale": {"mcp": {"server": server, "tool": tool}}}


def _session_id(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("role") == "meta" and event.get("type") == "session.resume_hint":
            value = event.get("session_id")
            return value if isinstance(value, str) and value else None
    return None


def _final_message(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        content = event.get("content")
        if event.get("role") == "assistant" and isinstance(content, str) and content.strip():
            return content
    return None


def _native_error(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") == "error" and "role" not in event:
            return error_text({"message": event.get("message"), "code": event.get("code")}) or (
                "native error"
            )
    return None


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
