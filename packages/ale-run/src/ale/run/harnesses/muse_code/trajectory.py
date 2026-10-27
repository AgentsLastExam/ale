"""Pure conversion of Muse Code evidence to ATIF."""

from __future__ import annotations

import base64
import binascii
import json
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

SESSIONS = ".local/share/muse/sessions"
SESSION_LOG = "session.jsonl"
_IMAGE_TYPES = frozenset(("image/png", "image/jpeg", "image/gif", "image/webp"))
_TERMINALS = ("run.terminal.completed", "run.terminal.failed")


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    # sessions/<year>/<month>/<day>/<session id>/...; a child lives below its parent.
    if not path.is_relative_to(SESSIONS):
        return False
    parts = path.relative_to(SESSIONS).parts
    return len(parts) > 4 and not parts[0].startswith(".") and parts[3] == session_id


def read_result(stdout: str) -> NativeResult:
    events, _ = json_lines(stdout)
    return NativeResult(_session_id(events), _final_text(events), _turn_error(events))


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    turns, issues = stdout_events(recorded)
    expected = requested_session_id(context, recorded) or _session_id(
        [event for turn in turns for event in turn]
    )
    files = mirrored_files(context, SESSIONS)
    root = context.logs_dir / SESSIONS
    logs = [
        path
        for path in files
        if expected
        and (parts := PurePosixPath(path.relative_to(root).as_posix()).parts)
        and len(parts) == 5
        and parts[3] == expected
        and parts[4] == SESSION_LOG
    ]
    records: list[dict[str, Any]] = []
    used: set[Path] = set()
    if len(logs) == 1:
        records = _session_records(context, logs[0], issues)
        used.add(logs[0])
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})

    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id, session_id=expected, agent=_agent(context)
    )
    totals = {"input": 0, "output": 0, "cached": 0}
    subagents: list[AtifTrajectory] = []
    reminders: set[Path] = set()
    if records:
        conversation = _Conversation(context, issues, used)
        subagents = conversation.walk(builder, logs[0].parent, records, totals=totals)
        reminders = conversation.reminders
    # Each Task invocation starts one run with its prompt; any the session lacks is rebuilt
    # from its own prompt and stdout. A run Muse starts itself, such as delivering a
    # subagent's result, has no prompt.
    native_turns = sum(1 for record in records if _prompt(record))
    if records and native_turns < len(recorded):
        issues.append({"reason": "prompt_not_in_native_session", "invocation": native_turns + 1})
    for item, stream in zip(recorded[native_turns:], turns[native_turns:], strict=True):
        builder.add(source="user", message=item.prompt)
        if message := _final_text(stream):
            builder.add(source="agent", message=message)
        if error := _turn_error(stream):
            builder.add(source="system", message=error)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    transcripts = [path for path in files if path.name == SESSION_LOG]
    if len(logs) == 1:
        # A reminder agent Muse runs on its own has no spawning call to link, but its
        # session names it, so it stays raw evidence without being reported as unlinked.
        children = logs[0].parent / "subagent"
        issues.extend(
            {
                "reason": "unlinked_subagent",
                "source": path.relative_to(context.logs_dir).as_posix(),
            }
            for path in sorted(set(transcripts) - used - reminders)
            if path.is_relative_to(children)
        )
    issues.extend(unconverted(context, set(transcripts) - used))
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
    extra = _extra(context, issues, has_session=bool(records))
    return trajectory.model_copy(update={"extra": extra}) if extra else trajectory


@dataclass
class _Step:
    """One model response, or a prompt or notice between responses, before it becomes ATIF."""

    source: str
    message: str = ""
    timestamp: str | None = None
    reasoning: list[str] = field(default_factory=list)
    calls: list[AtifToolCall] = field(default_factory=list)
    results: dict[str, AtifObservationResult] = field(default_factory=dict)
    metrics: AtifMetrics | None = None


@dataclass
class _Conversation:
    """Converts a session; each subagent is embedded in the session whose call spawned it."""

    context: TrajectoryParseContext
    issues: list[dict[str, Any]]
    used: set[Path]
    reminders: set[Path] = field(default_factory=set)

    def walk(
        self,
        builder: TrajectoryBuilder,
        directory: Path,
        records: list[dict[str, Any]],
        *,
        totals: dict[str, int] | None = None,
    ) -> list[AtifTrajectory]:
        """Add one session's conversation to ``builder``; return its embedded subagents."""
        self.reminders.update(_reminders(directory, records))
        embedded, refs = self._children(builder.trajectory_id, directory, records)
        steps: list[_Step] = []
        responses: dict[str, _Step] = {}
        owner: dict[str, _Step] = {}
        tool_tasks: dict[str, str] = {}
        failures: dict[str, str] = {}
        awaiting: _Step | None = None
        usage: AtifMetrics | None = None
        # The response in progress, and the provider-run calls it made before its first commit.
        current: _Step | None = None
        provided: list[tuple[AtifToolCall, AtifObservationResult]] = []
        for record in records:
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
            kind = event.get("kind")
            if record.get("payload_type") != "runtime.session":
                continue
            if payload.get("kind") == "task":
                key = event.get("idempotency_key")
                if (
                    kind == "side_effect_intent"
                    and isinstance(key, str)
                    and key.startswith("tool:")
                ):
                    tool_tasks[str(event.get("task_id"))] = key.removeprefix("tool:")
                elif kind == "failed" and str(event.get("task_id")) in tool_tasks:
                    failures[tool_tasks[str(event["task_id"])]] = str(event.get("reason") or "")
                continue
            if payload.get("kind") != "run":
                continue
            if kind == "started":
                if prompt := event.get("prompt"):
                    steps.append(_Step("user", str(prompt), _timestamp(record)))
            elif kind in {
                "reasoning_summary_committed",
                "reasoning_committed",
                "assistant_message_committed",
                "assistant_tool_calls_committed",
            }:
                response = str(event.get("response_id") or event.get("message_id") or "")
                step = responses.get(response)
                if step is None:
                    step = responses[response] = _Step("agent", timestamp=_timestamp(record))
                    steps.append(step)
                    if usage is not None:
                        step.metrics, usage = usage, None
                    else:
                        awaiting = step
                current = step
                for call, result in provided:
                    step.calls.append(call)
                    step.results[call.tool_call_id] = result
                provided.clear()
                if kind == "assistant_message_committed":
                    step.message += str(event.get("text") or "")
                elif kind == "assistant_tool_calls_committed":
                    for raw in event.get("tool_calls") or ():
                        if isinstance(raw, dict):
                            call = _call(raw)
                            step.calls.append(call)
                            owner[call.tool_call_id] = step
                elif isinstance(event.get("text"), str):
                    step.reasoning.append(event["text"])
            elif kind == "model_response_created":
                # Calls of a response that committed nothing else stand on their own.
                if provided:
                    steps.append(_provided_step(provided))
                    provided = []
                current = None
            elif kind == "provider_tool_call_completed":
                # A tool the provider ran inside the response, such as hosted web search.
                call, result = _provided(event)
                if current is None:
                    provided.append((call, result))
                else:
                    current.calls.append(call)
                    current.results[call.tool_call_id] = result
            elif kind == "model_completed":
                # Muse commits a response's tool calls after its usage, so usage belongs
                # to the response in progress, or else to the next one.
                metrics = _metrics(event.get("usage"), totals)
                if awaiting is not None and awaiting.metrics is None:
                    awaiting.metrics = metrics
                else:
                    usage = metrics
                awaiting = None
            elif kind == "tool_result_batch_committed":
                for result in event.get("results") or ():
                    if not isinstance(result, dict):
                        continue
                    call_id = str(result.get("tool_call_id") or "")
                    holder = owner.get(call_id)
                    if holder is not None and call_id not in holder.results:
                        holder.results[call_id] = AtifObservationResult(
                            source_call_id=call_id,
                            content=str(result.get("text") or ""),
                            subagent_trajectory_ref=refs.get(call_id),
                        )
            elif kind == "tool_result_model_visible_content":
                call_id = str(event.get("call_id") or "")
                holder = owner.get(call_id)
                if holder is not None and call_id in holder.results:
                    current = holder.results[call_id]
                    parts = (
                        [AtifContentPart(type="text", text=current.content)]
                        if isinstance(current.content, str)
                        else list(current.content or ())
                    )
                    parts.extend(self._media(item) for item in event.get("content") or ())
                    holder.results[call_id] = current.model_copy(update={"content": parts})
            elif kind == "inbox_item_drained":
                # What Muse delivers to the model between turns, such as a subagent's result.
                body = (event.get("delivery_snapshot") or {}).get("body")
                if isinstance(body, str) and body:
                    steps.append(_Step("system", body, _timestamp(record)))
            elif kind == "terminal" and event.get("terminal") not in (None, "completed"):
                reason = event.get("reason") or f"Muse run {event.get('terminal')}"
                steps.append(_Step("system", str(reason), _timestamp(record)))
        if provided:
            steps.append(_provided_step(provided))
        for call_id, reason in failures.items():
            holder = owner.get(call_id)
            if holder is None:
                continue
            current = holder.results.get(call_id) or AtifObservationResult(
                source_call_id=call_id, content=reason
            )
            holder.results[call_id] = current.model_copy(
                update={"extra": {"ale": {"is_error": True}}}
            )
        for step in steps:
            _emit(builder, step)
        return embedded

    def _children(
        self, trajectory_id: str, directory: Path, records: list[dict[str, Any]]
    ) -> tuple[list[AtifTrajectory], dict[str, list[AtifSubagentTrajectoryRef]]]:
        """Subagents a session spawned, keyed by the spawning call.

        Muse attests each start with the call that dispatched it and the child session it
        bound, stored below the parent. The reference sits on that call's result, which
        names the subagent, so a start whose call has no result stays unlinked.
        """
        answered = {
            str(result.get("tool_call_id"))
            for record in records
            for event in (_run_event(record),)
            if event.get("kind") == "tool_result_batch_committed"
            for result in event.get("results") or ()
            if isinstance(result, dict)
        }
        embedded: dict[str, AtifTrajectory] = {}
        refs: dict[str, list[AtifSubagentTrajectoryRef]] = {}
        for record in records:
            if record.get("payload_type") != "subagent.control.start_attested":
                continue
            attested = (record.get("payload") or {}).get("record") or {}
            call_id = attested.get("dispatched_via_tool_call_id")
            child_id = attested.get("subagent_session_id")
            if not isinstance(call_id, str) or call_id not in answered:
                continue
            if not isinstance(child_id, str) or not child_id or "/" in child_id or ".." in child_id:
                continue
            if child_id not in embedded:
                log = directory / "subagent" / child_id / SESSION_LOG
                if log in self.used or not log.is_file():
                    continue
                self.used.add(log)
                child = TrajectoryBuilder(
                    trajectory_id=f"{trajectory_id}-subagent-{child_id}",
                    session_id=child_id,
                    agent=_agent(self.context),
                    extra=_extra(self.context, [], has_session=True),
                )
                nested = self.walk(
                    child, log.parent, _session_records(self.context, log, self.issues)
                )
                if not child.steps:
                    self.used.discard(log)
                    continue
                embedded[child_id] = child.build(subagent_trajectories=nested or None)
            refs.setdefault(call_id, []).append(
                AtifSubagentTrajectoryRef(
                    trajectory_id=embedded[child_id].trajectory_id, session_id=child_id
                )
            )
        return list(embedded.values()), refs

    def _media(self, item: Any) -> AtifContentPart:
        if not isinstance(item, dict):
            return AtifContentPart(type="text", text=str(item))
        media_type = item.get("media_type")
        try:
            data = base64.b64decode(str(item.get("base64_data") or ""), validate=True)
        except (binascii.Error, ValueError):
            data = b""
        if media_type not in _IMAGE_TYPES or not data:
            label = item.get("path") or media_type or item.get("kind") or "media"
            return AtifContentPart(type="text", text=f"[media unavailable: {label}]")
        stored = self.context.blobs.put(data, media_type=media_type)
        return AtifContentPart(
            type="image", source=AtifImageSource(media_type=media_type, path=stored.path)
        )


def _session_records(
    context: TrajectoryParseContext, path: Path, issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """A session log's records in order, with retained frames unwrapped and repeats dropped."""
    lines, problems = native_events(context, path)
    issues.extend(problems)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines:
        for record in _unwrap(line):
            identity = record.get("id")
            if isinstance(identity, str):
                if identity in seen:
                    continue
                seen.add(identity)
            records.append(record)
    return records


def _unwrap(record: dict[str, Any]) -> list[dict[str, Any]]:
    children = record.get("children")
    if not isinstance(children, list):
        return [] if "retained_marker" in record else [record]
    found: list[dict[str, Any]] = []
    for child in children:
        value = child.get("record_json") if isinstance(child, dict) else None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, dict):
            found.extend(_unwrap(value))
    return found


def _emit(builder: TrajectoryBuilder, step: _Step) -> None:
    if step.source != "agent":
        builder.add(source=step.source, message=step.message, timestamp=step.timestamp)
        return
    reasoning = "\n".join(step.reasoning).strip() or None
    if not (step.message or reasoning or step.calls):
        return
    results = [
        step.results[call.tool_call_id] for call in step.calls if call.tool_call_id in step.results
    ]
    builder.add(
        source="agent",
        message=step.message,
        timestamp=step.timestamp,
        reasoning_content=reasoning,
        tool_calls=step.calls or None,
        observation=AtifObservation(results=results) if results else None,
        metrics=step.metrics,
    )


def _call(raw: dict[str, Any]) -> AtifToolCall:
    name = str(raw.get("name") or "")
    try:
        arguments = json.loads(raw["args"]) if isinstance(raw.get("args"), str) else raw.get("args")
    except ValueError:
        arguments = {"raw": raw.get("args")}
    server, separator, tool = name.removeprefix("mcp__").partition("__")
    extra = (
        {"ale": {"mcp": {"server": server, "tool": tool}}}
        if name.startswith("mcp__") and separator and server and tool
        else None
    )
    return AtifToolCall(
        tool_call_id=str(raw.get("call_id") or raw.get("id") or ""),
        function_name=name,
        arguments=arguments if isinstance(arguments, dict) else {"value": arguments},
        extra=extra,
    )


def _provided(event: dict[str, Any]) -> tuple[AtifToolCall, AtifObservationResult]:
    """A provider-run call: its action as arguments, its status and results as the result."""
    call_id = str(event.get("call_id") or "")
    action = event.get("action")
    status = str(event.get("status") or "completed")
    outcome: dict[str, Any] = {"status": status}
    if event.get("results") is not None:
        outcome["results"] = event["results"]
    return (
        AtifToolCall(
            tool_call_id=call_id,
            function_name=str(event.get("name") or ""),
            arguments=action if isinstance(action, dict) else {},
        ),
        AtifObservationResult(
            source_call_id=call_id,
            content=json.dumps(outcome, ensure_ascii=False, default=str),
            extra=None if status == "completed" else {"ale": {"is_error": True}},
        ),
    )


def _provided_step(provided: list[tuple[AtifToolCall, AtifObservationResult]]) -> _Step:
    return _Step(
        "agent",
        calls=[call for call, _ in provided],
        results={call.tool_call_id: result for call, result in provided},
    )


def _reminders(directory: Path, records: list[dict[str, Any]]) -> set[Path]:
    """Logs of the reminder agents a session ran, which no call of its model started.

    Muse links each reminder's child session to the session that ran it, and keeps its log
    below that session as it does a subagent's.
    """
    logs: set[Path] = set()
    for record in records:
        event = _run_event(record)
        child_id = event.get("child_session_id")
        if event.get("kind") != "memory_reminder_child_session_linked":
            continue
        if isinstance(child_id, str) and child_id and "/" not in child_id and ".." not in child_id:
            logs.add(directory / "subagent" / child_id / SESSION_LOG)
    return logs


def _run_event(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    if record.get("payload_type") != "runtime.session" or payload.get("kind") != "run":
        return {}
    event = payload.get("event")
    return event if isinstance(event, dict) else {}


def _prompt(record: dict[str, Any]) -> bool:
    event = _run_event(record)
    return event.get("kind") == "started" and bool(event.get("prompt"))


def _metrics(usage: Any, totals: dict[str, int] | None) -> AtifMetrics | None:
    if not isinstance(usage, dict):
        return None
    counts = {
        key: value if isinstance(value, int) and not isinstance(value, bool) else 0
        for key, value in (
            (key, usage.get(key)) for key in ("input_tokens", "output_tokens", "cached_tokens")
        )
    }
    if totals is not None:
        totals["input"] += counts["input_tokens"]
        totals["output"] += counts["output_tokens"]
        totals["cached"] += counts["cached_tokens"]
    return AtifMetrics(
        prompt_tokens=counts["input_tokens"],
        completion_tokens=counts["output_tokens"],
        cached_tokens=counts["cached_tokens"],
    )


def _timestamp(record: dict[str, Any]) -> str | None:
    # Session logs record microseconds since the epoch.
    value = record.get("recorded_at")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1_000_000, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _terminal(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (event for event in reversed(events) if event.get("payload_type") in _TERMINALS), None
    )


def _final_text(events: list[dict[str, Any]]) -> str | None:
    terminal = _terminal(events)
    if not terminal or terminal.get("payload_type") != _TERMINALS[0]:
        return None
    text = (terminal.get("payload") or {}).get("text")
    return text if isinstance(text, str) and text else None


def _session_id(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        stream = event.get("stream")
        if isinstance(stream, dict) and stream.get("kind") == "session":
            value = stream.get("id")
            if isinstance(value, str) and value:
                return value
    return None


def _turn_error(events: list[dict[str, Any]]) -> str | None:
    terminal = _terminal(events)
    if terminal is None:
        return "Muse Code reported no terminal result" if events else None
    if terminal.get("payload_type") == _TERMINALS[0]:
        return None
    reason = (terminal.get("payload") or {}).get("reason")
    return str(reason) if reason else "Muse Code run failed"


def _agent(context: TrajectoryParseContext) -> AtifAgent:
    return AtifAgent(
        name="muse-code", version=context.agent_version, model_name=context.model or None
    )


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
