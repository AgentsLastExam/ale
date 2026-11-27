"""Pure conversion of pi coding agent evidence to ATIF."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import PurePosixPath
from typing import Any

from orbit.core.harness import TrajectoryParseContext
from orbit.core.trajectory import (
    AtifAgent,
    AtifContentPart,
    AtifImageSource,
    AtifMetrics,
    AtifObservation,
    AtifObservationResult,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from orbit.run.harnesses.base import (
    NativeResult,
    call_metrics,
    epoch_time,
    invocations,
    iso_time,
    json_lines,
    mirrored_files,
    native_events,
    requested_session_id,
    stdout_events,
    token_count,
    unconverted,
)

#: ``--session-dir``: one flat directory of ``<timestamp>_<session id>.jsonl`` files.
SESSIONS = ".pi-orbit/sessions"
#: Stop reasons pi's own print mode reports as failures.
_FAILED = {"error", "aborted", "length"}


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    return path.parent == PurePosixPath(SESSIONS) and path.name.endswith(f"_{session_id}.jsonl")


def read_result(stdout: str) -> NativeResult:
    events, _ = json_lines(stdout)
    session_id = next(
        (
            event["id"]
            for event in events
            if event.get("type") == "session" and isinstance(event.get("id"), str)
        ),
        None,
    )
    ended = next((event for event in reversed(events) if event.get("type") == "agent_end"), None)
    messages = ended.get("messages") if ended else None
    last = next(
        (
            message
            for message in reversed(messages if isinstance(messages, list) else [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ),
        None,
    )
    if ended is None:
        # Pi exits 0 when a prompt names a command it registered, such as `/mcp`, and the
        # command consumes it, or when the prompt is empty: no turn ran.
        return NativeResult(session_id, None, "pi ran no agent turn for the prompt")
    if last is None:
        return NativeResult(session_id, None)
    return NativeResult(session_id, _text(last.get("content")) or None, _failure(last))


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    streams, issues = stdout_events(recorded)
    expected = requested_session_id(context, recorded) or next(
        (
            event["id"]
            for stream in streams
            for event in stream
            if event.get("type") == "session" and isinstance(event.get("id"), str)
        ),
        None,
    )
    transcripts = {
        path
        for path in mirrored_files(context, SESSIONS)
        if path.suffix == ".jsonl" and path.parent == context.logs_dir / SESSIONS
    }
    matches = sorted(
        path for path in transcripts if expected and path.name.endswith(f"_{expected}.jsonl")
    )
    entries: list[dict[str, Any]] = []
    if len(matches) == 1:
        entries, problems = native_events(context, matches[0])
        issues.extend(problems)
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
    issues.extend(unconverted(context, transcripts - set(matches[:1])))

    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=expected,
        agent=AtifAgent(
            name="pi-cli", version=context.agent_version, model_name=context.model or None
        ),
    )
    native = _active_branch(entries)
    turns = _convert(builder, native, context, issues)
    if native and turns < len(recorded):
        issues.append({"reason": "prompt_not_in_native_session", "invocation": turns + 1})
    # A turn the session lacks is rebuilt from its own JSON events, which carry the same
    # messages as they ended but no times of their own. A prompt takes its message's
    # creation time; an assistant message's is when its model call began, so its step
    # stays untimed.
    for item, stream in zip(recorded[turns:], streams[turns:], strict=True):
        messages = [
            event["message"]
            for event in stream
            if event.get("type") == "message_end" and isinstance(event.get("message"), dict)
        ]
        if not any(message.get("role") == "user" for message in messages):
            builder.add(source="user", message=item.prompt)
        _convert(
            builder,
            [
                {
                    "type": "message",
                    "message": message,
                    "timestamp": None
                    if message.get("role") == "assistant"
                    else epoch_time(message.get("timestamp"), 1000),
                }
                for message in messages
            ],
            context,
            issues,
        )
    if not recorded and not builder.steps:
        builder.add(source="user", message=context.instruction)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    trajectory = builder.build()
    extra = _extra(context, issues, native=bool(native))
    return trajectory.model_copy(update={"extra": extra}) if extra else trajectory


def _active_branch(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries from the session's leaf back to its root, in native order.

    Headless runs append one linear branch; this also holds after an interactive branch.
    """
    by_id = {entry["id"]: entry for entry in entries if isinstance(entry.get("id"), str)}
    leaf = next(
        (entry for entry in reversed(entries) if entry.get("type") != "session" and "id" in entry),
        None,
    )
    active: set[str] = set()
    while leaf is not None and leaf["id"] not in active:
        active.add(leaf["id"])
        parent = leaf.get("parentId")
        leaf = by_id.get(parent) if isinstance(parent, str) else None
    return [
        entry for entry in entries if entry.get("type") != "session" and entry.get("id") in active
    ]


def _convert(
    builder: TrajectoryBuilder,
    entries: list[dict[str, Any]],
    context: TrajectoryParseContext,
    issues: list[dict[str, Any]],
) -> int:
    """Add the entries' steps; the number of prompts among them.

    Each assistant message is one model call: its step lists the call's response ID after
    the entry's and carries the call's usage.
    """
    results: dict[str, AtifObservationResult] = {}
    servers: dict[str, dict[str, Any]] = {}
    for entry in entries:
        message = entry.get("message") if entry.get("type") == "message" else None
        if isinstance(message, dict) and message.get("role") == "toolResult":
            call_id = str(message.get("toolCallId") or "")
            details = message.get("details")
            if isinstance(details, dict) and {"server", "tool"} <= details.keys():
                # A direct MCP call names its logical server and tool.
                servers[call_id] = {
                    "orbit": {
                        "mcp": {"server": str(details["server"]), "tool": str(details["tool"])}
                    }
                }
            results[call_id] = AtifObservationResult(
                source_call_id=call_id,
                content=_content(message.get("content"), context),
                extra={"orbit": {"is_error": True}} if message.get("isError") else None,
            )
    prompts = 0
    for entry in entries:
        kind = entry.get("type")
        timestamp = iso_time(entry.get("timestamp"))
        ids = [entry["id"]] if isinstance(entry.get("id"), str) else []
        extra = {"orbit": {"native_event_ids": ids}} if ids else None
        if kind in {"compaction", "branch_summary"} and isinstance(entry.get("summary"), str):
            builder.add(source="system", message=entry["summary"], timestamp=timestamp, extra=extra)
            continue
        message = entry.get("message") if kind == "message" else None
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            prompts += 1
            builder.add(
                source="user",
                message=_content(message.get("content"), context),
                timestamp=timestamp,
                extra=extra,
            )
        elif role == "assistant":
            text: list[str] = []
            reasoning: list[str] = []
            calls: list[AtifToolCall] = []
            for block in message.get("content") or ():
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text.append(str(block.get("text") or ""))
                elif block.get("type") == "thinking":
                    reasoning.append(str(block.get("thinking") or ""))
                elif block.get("type") == "toolCall":
                    call_id = str(block.get("id") or "")
                    name = str(block.get("name") or "")
                    calls.append(
                        AtifToolCall(
                            tool_call_id=call_id,
                            function_name=name,
                            arguments=_arguments(block.get("arguments")),
                            extra=servers.get(call_id) or _mcp_extra(name),
                        )
                    )
            observed = [
                results[call.tool_call_id] for call in calls if call.tool_call_id in results
            ]
            response_id = message.get("responseId")
            response_id = response_id if isinstance(response_id, str) and response_id else None
            if response_id:
                extra = {"orbit": {"native_event_ids": [*ids, response_id]}}
            if "".join(text) or reasoning or calls:
                metrics = None
                if message.get("usage") is not None:
                    try:
                        metrics = _metrics(message["usage"])
                    except ValueError:
                        issues.append(
                            {
                                "reason": "invalid_token_usage",
                                "entry_id": entry.get("id"),
                                "response_id": response_id,
                            }
                        )
                builder.add(
                    source="agent",
                    message="".join(text),
                    timestamp=timestamp,
                    model_name=message.get("model")
                    if isinstance(message.get("model"), str)
                    else None,
                    reasoning_content="".join(reasoning) or None,
                    tool_calls=calls or None,
                    observation=AtifObservation(results=observed) if observed else None,
                    metrics=metrics,
                    extra=extra,
                )
            if failure := _failure(message):
                builder.add(source="system", message=failure, timestamp=timestamp)
        elif role in {"compactionSummary", "branchSummary", "custom"}:
            builder.add(
                source="system",
                message=_content(message.get("content") or message.get("summary") or "", context),
                timestamp=timestamp,
                extra=extra,
            )
    return prompts


def _failure(message: dict[str, Any]) -> str | None:
    reason = message.get("stopReason")
    if reason not in _FAILED:
        return None
    detail = message.get("errorMessage")
    return (
        f"pi stopped ({reason}): {detail}"
        if isinstance(detail, str) and detail
        else f"pi stopped ({reason})"
    )


def _metrics(usage: Any) -> AtifMetrics | None:
    """An assistant message's ``usage``; raises ``ValueError`` when it cannot be read.

    Pi's ``input`` leaves out the cache reads and writes, which the prompt adds back, and
    its ``output`` already includes reasoning.
    """
    if not isinstance(usage, dict):
        raise ValueError(f"not a usage object: {usage!r}")
    cached = token_count(usage.get("cacheRead"), optional=True)
    written = token_count(usage.get("cacheWrite"), optional=True)
    prompt = token_count(usage.get("input")) + cached + written
    return call_metrics(prompt, token_count(usage.get("output")), cached)


def _content(value: Any, context: TrajectoryParseContext) -> str | list[AtifContentPart]:
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
            try:
                data = base64.b64decode(block["data"], validate=True)
            except (binascii.Error, ValueError):
                parts.append(AtifContentPart(type="text", text="[image with invalid base64]"))
                continue
            blob = context.blobs.put(data, media_type=media_type)
            parts.append(
                AtifContentPart(
                    type="image",
                    source=AtifImageSource(media_type=media_type, path=blob.path),  # type: ignore[arg-type]
                )
            )
    if all(part.type == "text" for part in parts):
        return "".join(part.text or "" for part in parts)
    return parts


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return "".join(
        str(block.get("text") or "")
        for block in (value if isinstance(value, list) else ())
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {} if value is None else {"value": value}


def _mcp_extra(name: str) -> dict[str, Any] | None:
    _, server, tool = (
        [*name.split("__", 2), "", ""][:3] if name.startswith("mcp__") else ("", "", "")
    )
    if not server or not tool:
        return None
    return {"orbit": {"mcp": {"server": server, "tool": tool}}}


def _extra(
    context: TrajectoryParseContext, issues: list[dict[str, Any]], *, native: bool
) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if not native:
        data["limitations"] = [
            "Only the CLI's JSON events are available; the native session is absent."
        ]
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"orbit": data} if data else None
