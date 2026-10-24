"""Pure conversion of native evidence to ATIF 1.8."""

from __future__ import annotations

import base64
import json
from typing import Any

from ale.core.harness import (
    TrajectoryParseContext,
)
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
from ale.run.harnesses.evidence import jsonl_events, unconverted_native_files


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    events, issues = jsonl_events(context)
    expected_session_id = context.session_id or _thread_id_from_events(events)
    session_events: list[dict[str, Any]] = []
    if context.native_logs_enabled:
        root = context.logs_dir / "native/.codex-ale/sessions"
        unused = []
        for path in sorted(root.rglob("*.jsonl")):
            candidate, candidate_issues = jsonl_events(context, path)
            if expected_session_id and _session_id_from_events(candidate) == expected_session_id:
                session_events.extend(candidate)
                issues.extend(
                    {"source": path.relative_to(root).as_posix(), **issue}
                    for issue in candidate_issues
                )
            else:
                unused.append(path)
        issues.extend(unconverted_native_files(context, unused))
        if not session_events and context.incomplete_reason != "running":
            issues.append(
                {"reason": "native_session_unavailable", "session_id": expected_session_id}
            )
    native_calls = _session_tool_calls(session_events, events, context)
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=context.session_id or _thread_id_from_events(events),
        agent=AtifAgent(
            name="codex-cli",
            version=context.agent_version,
            model_name=context.model or None,
        ),
        extra=_extra(context, issues),
    )
    builder.add(source="user", message=context.instruction)
    usage: dict[str, int] = {}
    final_message = ""
    for event in events:
        kind = event.get("type")
        if kind == "turn.completed":
            raw = event.get("usage")
            if isinstance(raw, dict):
                usage = {
                    key: int(value or 0) for key, value in raw.items() if isinstance(value, int)
                }
            continue
        if kind == "error":
            builder.add(source="system", message=str(event.get("message") or event))
            continue
        if kind != "item.completed":
            continue
        item = event.get("item") or {}
        item_type = item.get("type")
        item_id = str(item.get("id") or "")
        if item_type == "agent_message":
            final_message = str(item.get("text") or "")
            if not native_calls:
                builder.add(source="agent", message=final_message)
        elif item_type == "reasoning":
            if not native_calls:
                builder.add(
                    source="agent",
                    message="",
                    reasoning_content=str(item.get("text") or ""),
                )
        elif item_type == "command_execution":
            if native_calls:
                continue
            call = AtifToolCall(
                tool_call_id=item_id,
                function_name="shell",
                arguments={"command": str(item.get("command") or "")},
            )
            builder.add(
                source="agent",
                message="",
                tool_calls=[call],
                observation=AtifObservation(
                    results=[
                        AtifObservationResult(
                            source_call_id=item_id,
                            content=str(item.get("aggregated_output") or ""),
                            extra=(
                                {"ale": {"is_error": True}}
                                if int(item.get("exit_code") or 0) != 0
                                else None
                            ),
                        )
                    ]
                ),
            )
        elif item_type == "mcp_tool_call":
            if native_calls:
                continue
            server = str(item.get("server") or "")
            tool = str(item.get("tool") or "")
            call = AtifToolCall(
                tool_call_id=item_id,
                function_name=f"mcp__{server}__{tool}",
                arguments=_dict(item.get("arguments")),
                extra={"ale": {"mcp": {"server": server, "tool": tool}}},
            )
            result = item.get("result")
            error = item.get("error")
            builder.add(
                source="agent",
                message="",
                tool_calls=[call],
                observation=(
                    AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id=item_id,
                                content=_result_content(result or error or "", context),
                                extra=({"ale": {"is_error": True}} if error is not None else None),
                            )
                        ]
                    )
                    if result is not None or error is not None
                    else None
                ),
            )
        elif item_type == "file_change":
            if native_calls:
                continue
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
                observation=AtifObservation(
                    results=[
                        AtifObservationResult(
                            source_call_id=item_id,
                            content=str(item.get("status") or "completed"),
                        )
                    ]
                ),
            )
        elif item_type == "web_search":
            if not any(call.function_name == "web.run" for call, _ in native_calls):
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
                    observation=AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id=item_id,
                                content=json.dumps(
                                    {
                                        "status": item.get("status") or "completed",
                                        "action": item.get("action"),
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                ),
                            )
                        ]
                    ),
                )
    for call, observation in native_calls:
        builder.add(
            source="agent",
            message="",
            tool_calls=[call],
            observation=observation,
        )
    if native_calls and final_message:
        builder.add(source="agent", message=final_message)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    cached = usage.get("cached_input_tokens") or (usage.get("input_tokens_details_cached_tokens"))
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
    return builder.build(final_metrics=final_metrics)


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


def _session_tool_calls(
    session_events: list[dict[str, Any]],
    transcript_events: list[dict[str, Any]],
    context: TrajectoryParseContext,
) -> list[tuple[AtifToolCall, AtifObservation | None]]:
    outputs: dict[str, Any] = {}
    for event in session_events:
        payload = event.get("payload") or {}
        if event.get("type") != "response_item":
            continue
        if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            outputs[str(payload.get("call_id") or "")] = payload.get("output")
        elif payload.get("type") == "tool_search_output":
            outputs[str(payload.get("call_id") or "")] = {
                "status": payload.get("status"),
                "tools": payload.get("tools") or [],
            }
    transcript_mcp = [
        event.get("item") or {}
        for event in transcript_events
        if event.get("type") == "item.completed"
        if (event.get("item") or {}).get("type") == "mcp_tool_call"
    ]
    used_mcp: set[int] = set()
    calls: list[tuple[AtifToolCall, AtifObservation | None]] = []
    for event in session_events:
        payload = event.get("payload") or {}
        if event.get("type") != "response_item":
            continue
        if payload.get("type") == "web_search_call":
            call_id = str(payload.get("id") or "")
            calls.append(
                (
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name="web.run",
                        arguments=_dict(payload.get("action")),
                    ),
                    AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id=call_id,
                                content=json.dumps(
                                    {
                                        "status": payload.get("status") or "completed",
                                        "action": payload.get("action"),
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                ),
                            )
                        ]
                    ),
                )
            )
            continue
        if payload.get("type") == "tool_search_call":
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            calls.append(
                (
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name="tool_search_tool",
                        arguments=_dict(payload.get("arguments")),
                    ),
                    AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id=call_id,
                                content=_session_result_content(outputs.get(call_id), context),
                            )
                        ]
                    )
                    if call_id in outputs
                    else None,
                )
            )
            continue
        if payload.get("type") == "custom_tool_call":
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            calls.append(
                (
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name=str(payload.get("name") or "custom_tool"),
                        arguments={"input": payload.get("input")},
                    ),
                    AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id=call_id,
                                content=_session_result_content(outputs.get(call_id), context),
                            )
                        ]
                    )
                    if call_id in outputs
                    else None,
                )
            )
            continue
        if payload.get("type") != "function_call":
            continue
        call_id = str(payload.get("call_id") or payload.get("id") or "")
        name = str(payload.get("name") or "")
        namespace = str(payload.get("namespace") or "")
        arguments = _dict(payload.get("arguments"))
        extra = None
        if namespace.startswith("mcp__"):
            match = next(
                (
                    (index, item)
                    for index, item in enumerate(transcript_mcp)
                    if index not in used_mcp
                    and item.get("tool") == name
                    and _dict(item.get("arguments")) == arguments
                ),
                None,
            )
            if match is not None:
                index, item = match
                used_mcp.add(index)
                server = str(item.get("server") or namespace.removeprefix("mcp__"))
                result = item.get("result")
                error = item.get("error")
                output = result if result is not None else error
            else:
                server = namespace.removeprefix("mcp__")
                error = None
                output = outputs.get(call_id)
            function_name = f"mcp__{server}__{name}"
            extra = {"ale": {"mcp": {"server": server, "tool": name}}}
        else:
            function_name = (
                f"multi_agent.{name}"
                if namespace == "multi_agent_v1"
                else f"{namespace}.{name}"
                if namespace
                else name
            )
            error = None
            output = outputs.get(call_id)
        observation = (
            AtifObservation(
                results=[
                    AtifObservationResult(
                        source_call_id=call_id,
                        content=_session_result_content(output, context),
                        extra={"ale": {"is_error": True}} if error is not None else None,
                    )
                ]
            )
            if output is not None
            else None
        )
        calls.append(
            (
                AtifToolCall(
                    tool_call_id=call_id,
                    function_name=function_name,
                    arguments=arguments,
                    extra=extra,
                ),
                observation,
            )
        )
    return calls


def _session_result_content(
    value: Any, context: TrajectoryParseContext
) -> str | list[AtifContentPart]:
    if not isinstance(value, list):
        return _result_content(value, context)
    parts = [
        AtifContentPart(type="text", text=str(block.get("text") or ""))
        for block in value
        if isinstance(block, dict) and block.get("type") in {"input_text", "output_text", "text"}
    ]
    return parts or json.dumps(value, ensure_ascii=False, default=str)


def _extra(context: TrajectoryParseContext, issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"ale": data} if data else None
