"""Pure conversion of Factory Droid evidence to ATIF."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any

from ale.core.harness import TrajectoryParseContext
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

#: Droid's state directory below the agent home: ``<FACTORY_HOME_OVERRIDE>/.factory``.
STATE = ".droid-ale/.factory"
#: Sessions are ``<cwd, with each separator a dash>/<session id>.jsonl`` below this.
SESSIONS = f"{STATE}/sessions"
#: How Droid names an MCP tool: ``<server>___<tool>``.
_MCP_SEPARATOR = "___"


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    return path.parent.parent == PurePosixPath(SESSIONS) and path.name == f"{session_id}.jsonl"


def read_result(stdout: str) -> NativeResult:
    events, _ = json_lines(stdout)
    session_id = next(
        (
            event["session_id"]
            for event in reversed(events)
            if isinstance(event.get("session_id"), str) and event["session_id"]
        ),
        None,
    )
    completion = next((event for event in events if event.get("type") == "completion"), None)
    if completion is not None:
        final = completion.get("finalText")
        return NativeResult(session_id, final if isinstance(final, str) and final else None)
    return NativeResult(session_id, None, _stream_error(events))


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    streams, issues = stdout_events(recorded)
    expected = (
        requested_session_id(context, recorded)
        or read_result("".join(item.stdout for item in recorded)).session_id
    )
    files = mirrored_files(context, SESSIONS)
    transcripts = {
        path
        for path in files
        if path.suffix == ".jsonl" and path.parent.parent == context.logs_dir / SESSIONS
    }
    matches = sorted(path for path in transcripts if expected and path.name == f"{expected}.jsonl")
    events: list[dict[str, Any]] = []
    if len(matches) == 1:
        events, problems = native_events(context, matches[0])
        issues.extend(problems)
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
    issues.extend(unconverted(context, transcripts - set(matches[:1])))

    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=expected,
        agent=AtifAgent(
            name="droid", version=context.agent_version, model_name=context.model or None
        ),
    )
    turns = list(_native_turns(events, issues))
    if events and len(turns) < len(recorded):
        issues.append({"reason": "prompt_not_in_native_session", "invocation": len(turns) + 1})
    for index, item in enumerate(recorded):
        stream = streams[index]
        if index < len(turns):
            reported = _native_steps(builder, turns[index], context)
            if not reported and (error := _stream_error(stream)):
                builder.add(source="system", message=error)
        else:
            # The session lacks this invocation; its own stream-json is the record.
            builder.add(source="user", message=item.prompt)
            _stream_steps(builder, stream, context)
    if not recorded:
        builder.add(source="user", message=context.instruction)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    usage = [
        event["usage"]
        for stream in streams
        for event in stream
        if event.get("type") == "completion" and isinstance(event.get("usage"), dict)
    ]
    final_metrics = (
        AtifFinalMetrics(
            total_prompt_tokens=sum(_count(item, "input_tokens") for item in usage),
            total_completion_tokens=sum(_count(item, "output_tokens") for item in usage),
            total_cached_tokens=sum(_count(item, "cache_read_input_tokens") for item in usage),
            total_steps=len(builder.steps),
        )
        if usage
        else None
    )
    trajectory = builder.build(final_metrics=final_metrics)
    extra = _extra(context, issues, native=bool(events))
    return trajectory.model_copy(update={"extra": extra}) if extra else trajectory


# --- the native session ---


def _native_turns(
    events: list[dict[str, Any]], issues: list[dict[str, Any]]
) -> Iterator[list[dict[str, Any]]]:
    """The session's turns, each opened by one prompt ALE sent, in native order.

    Droid injects its own context before each prompt as ``llm_only`` messages; those are
    not task input and are skipped.
    """
    turn: list[dict[str, Any]] | None = None
    unknown: set[str] = set()
    for event in events:
        kind = event.get("type")
        if kind == "session_start":
            continue
        if kind == "message":
            message = event.get("message")
            if not isinstance(message, dict) or message.get("visibility") == "llm_only":
                continue
            if _is_prompt(message):
                if turn is not None:
                    yield turn
                turn = [event]
                continue
        elif kind != "agent_turn_outcome":
            unknown.add(str(kind))
            continue
        if turn is not None:
            turn.append(event)
    if turn is not None:
        yield turn
    if unknown:
        issues.append({"reason": "unknown_native_event_types", "types": sorted(unknown)[:10]})


def _native_steps(
    builder: TrajectoryBuilder, turn: list[dict[str, Any]], context: TrajectoryParseContext
) -> bool:
    """Add one native turn; whether the session itself reports how the turn failed."""
    results: dict[str, AtifObservationResult] = {}
    for event in turn:
        for block in _blocks(event, "user"):
            if block.get("type") == "tool_result":
                call_id = str(block.get("tool_use_id") or "")
                results[call_id] = AtifObservationResult(
                    source_call_id=call_id,
                    content=_content(block.get("content"), context),
                    extra={"ale": {"is_error": True}} if block.get("is_error") else None,
                )
    reported = False
    for event in turn:
        if event.get("type") == "agent_turn_outcome":
            reason = event.get("reason")
            if reason != "completed" and not reported:
                builder.add(source="system", message=f"droid turn ended: {reason}")
                reported = True
            continue
        message = event["message"]
        role = message.get("role")
        timestamp = event.get("timestamp") if isinstance(event.get("timestamp"), str) else None
        extra = {"ale": {"native_event_ids": [str(event.get("id"))]}} if event.get("id") else None
        if role == "user" and message.get("visibility") == "user_only":
            # A notice Droid shows its user, such as an unreachable provider.
            builder.add(source="system", message=_text(message), timestamp=timestamp, extra=extra)
            reported = True
        elif role == "user" and _is_prompt(message):
            builder.add(source="user", message=_text(message), timestamp=timestamp, extra=extra)
        elif role == "assistant":
            text: list[str] = []
            reasoning: list[str] = []
            calls: list[AtifToolCall] = []
            for block in _blocks(event, "assistant"):
                kind = block.get("type")
                if kind == "text":
                    text.append(str(block.get("text") or ""))
                elif kind == "thinking":
                    reasoning.append(str(block.get("thinking") or ""))
                elif kind == "tool_use":
                    name = str(block.get("name") or "")
                    calls.append(
                        AtifToolCall(
                            tool_call_id=str(block.get("id") or ""),
                            function_name=name,
                            arguments=_arguments(block.get("input")),
                            extra=_mcp_extra(name),
                        )
                    )
            observed = [
                results[call.tool_call_id] for call in calls if call.tool_call_id in results
            ]
            builder.add(
                source="agent",
                message="".join(text),
                timestamp=timestamp,
                reasoning_content="".join(reasoning) or None,
                tool_calls=calls or None,
                observation=AtifObservation(results=observed) if observed else None,
                extra=extra,
            )
    return reported


def _is_prompt(message: dict[str, Any]) -> bool:
    if message.get("role") != "user" or message.get("visibility") in {"llm_only", "user_only"}:
        return False
    content = message.get("content")
    return isinstance(content, list) and not any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def _blocks(event: dict[str, Any], role: str) -> list[dict[str, Any]]:
    message = event.get("message")
    if event.get("type") != "message" or not isinstance(message, dict):
        return []
    if message.get("role") != role or not isinstance(message.get("content"), list):
        return []
    return [block for block in message["content"] if isinstance(block, dict)]


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(
        str(block.get("text") or "")
        for block in content or ()
        if isinstance(block, dict) and block.get("type") == "text"
    )


# --- one invocation's stream-json ---


def _stream_steps(
    builder: TrajectoryBuilder, stream: list[dict[str, Any]], context: TrajectoryParseContext
) -> None:
    results = {
        str(event.get("id") or ""): AtifObservationResult(
            source_call_id=str(event.get("id") or ""),
            content=_content(
                event["value"] if "value" in event else (event.get("error") or {}).get("message"),
                context,
            ),
            extra={"ale": {"is_error": True}} if event.get("isError") else None,
        )
        for event in stream
        if event.get("type") == "tool_result"
    }
    pending: list[AtifToolCall] = []
    pending_message: object = None

    def flush() -> None:
        nonlocal pending
        if pending:
            observed = [
                results[call.tool_call_id] for call in pending if call.tool_call_id in results
            ]
            builder.add(
                source="agent",
                message="",
                tool_calls=pending,
                observation=AtifObservation(results=observed) if observed else None,
            )
            pending = []

    for event in stream:
        kind = event.get("type")
        if kind == "tool_call":
            if event.get("messageId") != pending_message:
                flush()
                pending_message = event.get("messageId")
            name = str(event.get("toolName") or event.get("toolId") or "")
            pending.append(
                AtifToolCall(
                    tool_call_id=str(event.get("id") or ""),
                    function_name=name,
                    arguments=_arguments(event.get("parameters")),
                    extra=_mcp_extra(name),
                )
            )
        elif kind == "message" and event.get("role") == "assistant":
            flush()
            builder.add(source="agent", message=str(event.get("text") or ""))
    flush()
    if error := _stream_error(stream):
        builder.add(source="system", message=error)


def _stream_error(events: list[dict[str, Any]]) -> str | None:
    """The invocation's failure, unless it completed despite recovered errors."""
    if any(event.get("type") == "completion" for event in events):
        return None
    errors = [
        str(event.get("message") or "native error")
        for event in events
        if event.get("type") == "error"
    ]
    return errors[-1] if errors else None


# --- shared ---


def _content(value: Any, context: TrajectoryParseContext) -> str | list[AtifContentPart]:
    if isinstance(value, str):
        return value
    parts: list[AtifContentPart] = []
    for block in value if isinstance(value, list) else ():
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(AtifContentPart(type="text", text=str(block.get("text") or "")))
            continue
        source = block.get("source") if isinstance(block.get("source"), dict) else {}
        if block.get("type") == "image" and isinstance(source.get("data"), str):
            media_type = str(source.get("media_type") or "image/png")
            try:
                data = base64.b64decode(source["data"], validate=True)
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
            continue
        parts.append(AtifContentPart(type="text", text=json.dumps(block, ensure_ascii=False)))
    return parts or json.dumps(value, ensure_ascii=False, default=str)


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
    server, separator, tool = name.partition(_MCP_SEPARATOR)
    if not separator or not server or not tool:
        return None
    return {"ale": {"mcp": {"server": server, "tool": tool}}}


def _count(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) else 0


def _extra(
    context: TrajectoryParseContext, issues: list[dict[str, Any]], *, native: bool
) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if not native:
        data["limitations"] = [
            "Only the CLI's stream-json is available; the native session is absent."
        ]
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"ale": data} if data else None
