"""Pure conversion of Codex CLI evidence to ATIF."""

from __future__ import annotations

import base64
import json
import re
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
from ale.run.harnesses._diagnostics import error_text
from ale.run.harnesses.base import (
    NativeResult,
    invocations,
    mirrored_files,
    native_events,
    requested_session_id,
    stdout_events,
    unconverted,
)

SESSIONS = ".codex-ale/sessions"
_CALL_TYPES = frozenset(
    ("function_call", "custom_tool_call", "web_search_call", "tool_search_call")
)
_OUTPUT_TYPES = frozenset(("function_call_output", "custom_tool_call_output", "tool_search_output"))
_DATA_URL = re.compile(r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=]+)")


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    # Rollouts are named `rollout-<timestamp>-<thread id>.jsonl`.
    return path.is_relative_to(SESSIONS) and path.name.endswith(f"-{session_id}.jsonl")


def read_result(stdout: str) -> NativeResult:
    return NativeResult(_thread_id(stdout), _final_message(stdout), _native_error(stdout))


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    turns, issues = stdout_events(recorded)
    stream = [event for turn in turns for event in turn]
    expected = requested_session_id(context, recorded) or _thread_id_from_events(stream)
    files = mirrored_files(context, SESSIONS)
    matches = [
        path
        for path in files
        if expected
        and is_session_file(PurePosixPath(path.relative_to(context.logs_dir).as_posix()), expected)
    ]
    rollout: list[dict[str, Any]] = []
    if len(matches) == 1:
        rollout, native_issues = native_events(context, matches[0])
        issues.extend(native_issues)
        observed = _session_id_from_events(rollout)
        if observed and observed != expected:
            issues.append(
                {"reason": "session_id_mismatch", "expected": expected, "actual": observed}
            )
            rollout = []
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
    issues.extend(unconverted(context, set(files) - set(matches[:1])))
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=expected,
        agent=AtifAgent(
            name="codex-cli", version=context.agent_version, model_name=context.model or None
        ),
    )
    # The rollout is the complete ordered conversation. Its user messages include context
    # Codex injects, so a turn starts only at the exact prompt ALE sent. Turns the rollout
    # does not contain fall back to that invocation's stdout events.
    prompts = [item.prompt.strip() for item in recorded]
    native_turns = 0
    transcript_mcp = [
        event.get("item") or {}
        for event in stream
        if event.get("type") == "item.completed"
        and (event.get("item") or {}).get("type") == "mcp_tool_call"
    ]
    outputs = {
        str(payload.get("call_id") or ""): payload
        for event in rollout
        if event.get("type") == "response_item"
        and (payload := event.get("payload") or {}).get("type") in _OUTPUT_TYPES
    }
    used_mcp: set[int] = set()
    for event in rollout:
        payload = event.get("payload") or {}
        if event.get("type") != "response_item":
            continue
        kind = payload.get("type")
        if kind == "message" and payload.get("role") == "user":
            if native_turns < len(prompts) and _text(payload) == prompts[native_turns]:
                if native_turns:
                    _errors(builder, turns[native_turns - 1])
                builder.add(source="user", message=recorded[native_turns].prompt)
                native_turns += 1
            continue
        if kind not in _CALL_TYPES and kind != "reasoning" and payload.get("role") != "assistant":
            continue
        if not native_turns:
            # The conversation began without the prompt ALE sent; keep it and say so.
            issues.append({"reason": "prompt_not_in_native_session", "invocation": 1})
            builder.add(source="user", message=recorded[0].prompt)
            native_turns = 1
        if kind == "message":
            if text := _text(payload):
                builder.add(source="agent", message=text)
        elif kind == "reasoning":
            if summary := _summary(payload):
                builder.add(source="agent", message="", reasoning_content=summary)
        elif kind in _CALL_TYPES:
            call, observation = _native_call(payload, outputs, transcript_mcp, used_mcp, context)
            builder.add(source="agent", message="", tool_calls=[call], observation=observation)
    if native_turns:
        _errors(builder, turns[native_turns - 1])
    if rollout and native_turns < len(recorded):
        issues.append({"reason": "prompt_not_in_native_session", "invocation": native_turns + 1})
    for item, turn in zip(recorded[native_turns:], turns[native_turns:], strict=True):
        builder.add(source="user", message=item.prompt)
        _stream_steps(builder, turn, context)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    usage: dict[str, int] = {}
    for event in stream:
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = {key: value for key, value in event["usage"].items() if isinstance(value, int)}
    cached = usage.get("cached_input_tokens") or usage.get("input_tokens_details_cached_tokens")
    final_metrics = (
        AtifFinalMetrics(
            total_prompt_tokens=usage.get("input_tokens"),
            total_completion_tokens=usage.get("output_tokens"),
            total_cached_tokens=cached,
            total_steps=len(builder.steps),
        )
        if usage
        else None
    )
    trajectory = builder.build(final_metrics=final_metrics)
    extra = _extra(context, issues)
    return trajectory.model_copy(update={"extra": extra}) if extra else trajectory


def _errors(builder: TrajectoryBuilder, events: list[dict[str, Any]]) -> None:
    """Failures only stdout reports, at the end of their turn."""
    for event in events:
        if event.get("type") in {"error", "turn.failed"}:
            detail = error_text(event.get("error") or event.get("message")) or str(event)
            builder.add(source="system", message=detail)


def _stream_steps(
    builder: TrajectoryBuilder, events: list[dict[str, Any]], context: TrajectoryParseContext
) -> None:
    """One turn from ``codex exec --json`` events alone."""
    for event in events:
        kind = event.get("type")
        if kind in {"error", "turn.failed"}:
            detail = error_text(event.get("error") or event.get("message")) or str(event)
            builder.add(source="system", message=detail)
            continue
        if kind != "item.completed":
            continue
        item = event.get("item") or {}
        item_type = item.get("type")
        item_id = str(item.get("id") or "")
        if item_type == "agent_message":
            builder.add(source="agent", message=str(item.get("text") or ""))
        elif item_type == "reasoning":
            builder.add(source="agent", message="", reasoning_content=str(item.get("text") or ""))
        elif item_type == "command_execution":
            builder.add(
                source="agent",
                message="",
                tool_calls=[
                    AtifToolCall(
                        tool_call_id=item_id,
                        function_name="shell",
                        arguments={"command": str(item.get("command") or "")},
                    )
                ],
                observation=_observation(
                    item_id,
                    str(item.get("aggregated_output") or ""),
                    error=int(item.get("exit_code") or 0) != 0,
                ),
            )
        elif item_type == "mcp_tool_call":
            server = str(item.get("server") or "")
            tool = str(item.get("tool") or "")
            result, error = item.get("result"), item.get("error")
            builder.add(
                source="agent",
                message="",
                tool_calls=[
                    AtifToolCall(
                        tool_call_id=item_id,
                        function_name=f"mcp__{server}__{tool}",
                        arguments=_dict(item.get("arguments")),
                        extra={"ale": {"mcp": {"server": server, "tool": tool}}},
                    )
                ],
                observation=(
                    _observation(
                        item_id,
                        _result_content(result or error or "", context),
                        error=error is not None,
                    )
                    if result is not None or error is not None
                    else None
                ),
            )
        elif item_type == "file_change":
            builder.add(
                source="agent",
                message="",
                tool_calls=[
                    AtifToolCall(
                        tool_call_id=item_id,
                        function_name="apply_patch",
                        arguments={"changes": item.get("changes") or []},
                    )
                ],
                observation=_observation(item_id, str(item.get("status") or "completed")),
            )
        elif item_type == "web_search":
            builder.add(
                source="agent",
                message="",
                tool_calls=[
                    AtifToolCall(
                        tool_call_id=item_id,
                        function_name="web.run",
                        arguments={"query": str(item.get("query") or "")},
                    )
                ],
                observation=_observation(
                    item_id,
                    json.dumps(
                        {"status": item.get("status") or "completed", "action": item.get("action")},
                        ensure_ascii=False,
                        default=str,
                    ),
                ),
            )


def _native_call(
    payload: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    transcript_mcp: list[dict[str, Any]],
    used_mcp: set[int],
    context: TrajectoryParseContext,
) -> tuple[AtifToolCall, AtifObservation | None]:
    kind = payload.get("type")
    if kind == "web_search_call":
        call_id = str(payload.get("id") or "")
        return (
            AtifToolCall(
                tool_call_id=call_id,
                function_name="web.run",
                arguments=_dict(payload.get("action")),
            ),
            _observation(
                call_id,
                json.dumps(
                    {
                        "status": payload.get("status") or "completed",
                        "action": payload.get("action"),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            ),
        )
    call_id = str(payload.get("call_id") or payload.get("id") or "")
    output = outputs.get(call_id)
    if kind == "tool_search_call":
        value = (
            {"status": output.get("status"), "tools": output.get("tools") or []} if output else None
        )
        return (
            AtifToolCall(
                tool_call_id=call_id,
                function_name="tool_search_tool",
                arguments=_dict(payload.get("arguments")),
            ),
            _observation(call_id, _session_result_content(value, context)) if output else None,
        )
    if kind == "custom_tool_call":
        return (
            AtifToolCall(
                tool_call_id=call_id,
                function_name=str(payload.get("name") or "custom_tool"),
                arguments={"input": payload.get("input")},
            ),
            _observation(call_id, _session_result_content(output.get("output"), context))
            if output
            else None,
        )
    name = str(payload.get("name") or "")
    namespace = str(payload.get("namespace") or "")
    arguments = _dict(payload.get("arguments"))
    value = output.get("output") if output else None
    error = None
    extra = None
    if namespace.startswith("mcp__"):
        # The namespace carries a sanitized server name; stdout carries the real one and
        # the structured result, so the two are matched on server, tool and arguments.
        server = namespace.removeprefix("mcp__")
        match = next(
            (
                (index, item)
                for index, item in enumerate(transcript_mcp)
                if index not in used_mcp
                and _sanitized(str(item.get("server") or "")) == server
                and item.get("tool") == name
                and _dict(item.get("arguments")) == arguments
            ),
            None,
        )
        if match is not None:
            index, item = match
            used_mcp.add(index)
            server = str(item.get("server") or server)
            error = item.get("error")
            value = item.get("result") if item.get("result") is not None else error
        function_name = f"mcp__{server}__{name}"
        extra = {"ale": {"mcp": {"server": server, "tool": name}}}
    elif namespace == "multi_agent_v1":
        function_name = f"multi_agent.{name}"
    else:
        function_name = f"{namespace}.{name}" if namespace else name
    return (
        AtifToolCall(
            tool_call_id=call_id, function_name=function_name, arguments=arguments, extra=extra
        ),
        _observation(call_id, _session_result_content(value, context), error=error is not None)
        if value is not None
        else None,
    )


def _observation(
    call_id: str, content: str | list[AtifContentPart], *, error: bool = False
) -> AtifObservation:
    return AtifObservation(
        results=[
            AtifObservationResult(
                source_call_id=call_id,
                content=content,
                extra={"ale": {"is_error": True}} if error else None,
            )
        ]
    )


def _sanitized(server: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", server)


def _text(payload: dict[str, Any]) -> str:
    return "".join(
        str(part.get("text") or "")
        for part in payload.get("content") or ()
        if isinstance(part, dict) and part.get("type") in {"input_text", "output_text", "text"}
    ).strip()


def _summary(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(part.get("text") or "")
        for part in payload.get("summary") or ()
        if isinstance(part, dict) and part.get("text")
    )


def _thread_id(text: str) -> str | None:
    return _thread_id_from_events(_stream_events(text))


def _stream_events(text: str) -> list[dict[str, Any]]:
    events = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _native_error(text: str) -> str | None:
    failure = None
    for event in _stream_events(text):
        kind = event.get("type")
        if kind in {"thread.started", "turn.started", "turn.completed"}:
            failure = None
        elif kind in {"error", "turn.failed"}:
            failure = error_text(event.get("error") or event.get("message")) or str(kind)
    return failure


def _thread_id_from_events(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


def _session_id_from_events(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "session_meta" and isinstance(payload.get("id"), str):
            return payload["id"]
    return None


def _final_message(text: str) -> str | None:
    final = None
    for event in _stream_events(text):
        item = _dict(event.get("item"))
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            final = str(item.get("text") or "")
    return final


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {"value": value}


def _result_content(value: Any, context: TrajectoryParseContext) -> str | list[AtifContentPart]:
    if not isinstance(value, dict):
        return str(value)
    parts: list[AtifContentPart] = []
    for block in value.get("content") or ():
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


def _session_result_content(
    value: Any, context: TrajectoryParseContext
) -> str | list[AtifContentPart]:
    if not isinstance(value, list):
        return _result_content(value, context)
    parts: list[AtifContentPart] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in {"input_text", "output_text", "text"}:
            parts.append(AtifContentPart(type="text", text=str(block.get("text") or "")))
        elif kind == "input_image":
            if match := _DATA_URL.fullmatch(str(block.get("image_url") or "")):
                media_type = match.group(1)
                blob = context.blobs.put(base64.b64decode(match.group(2)), media_type=media_type)
                parts.append(
                    AtifContentPart(
                        type="image",
                        source=AtifImageSource(media_type=media_type, path=blob.path),
                    )
                )
            else:
                # An image ALE cannot store stays visible as the record it was.
                text = json.dumps(block, ensure_ascii=False, default=str)
                parts.append(AtifContentPart(type="text", text=text))
    return parts or json.dumps(value, ensure_ascii=False, default=str)


def _extra(context: TrajectoryParseContext, issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"ale": data} if data else None
