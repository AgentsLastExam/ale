"""Pure conversion of native evidence to ATIF 1.8."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from ale.core.harness import (
    TrajectoryParseContext,
)
from ale.core.trajectory import (
    AtifAgent,
    AtifContentPart,
    AtifImageSource,
    AtifObservation,
    AtifObservationResult,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from ale.run.harnesses._diagnostics import error_text
from ale.run.harnesses.evidence import jsonl_events, unconverted_native_files

_DATA_URL = re.compile(r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=_-]+)")


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    stream, issues = jsonl_events(context)
    chat: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    expected = context.session_id or _terminal_session_id_from_events(stream)
    if context.native_logs_enabled:
        root = context.logs_dir / "native/.grok-ale/sessions"
        paths = sorted(
            path
            for path in root.rglob("*.jsonl")
            if path.name in {"chat_history.jsonl", "updates.jsonl"}
        )
        sessions = sorted({path.parent for path in paths if path.parent.name == expected})
        if len(sessions) == 1:
            chat, chat_issues = jsonl_events(context, sessions[0] / "chat_history.jsonl")
            updates, update_issues = jsonl_events(context, sessions[0] / "updates.jsonl")
            issues.extend(chat_issues + update_issues)
        elif context.incomplete_reason != "running":
            issues.append({"reason": "native_session_unavailable", "session_id": expected})
        issues.extend(
            unconverted_native_files(
                context,
                (path for path in paths if len(sessions) != 1 or path.parent != sessions[0]),
            )
        )
    results = {
        str(event.get("tool_call_id")): event
        for event in chat
        if event.get("type") == "tool_result" and event.get("tool_call_id")
    }
    errors = _tool_errors(updates)
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=context.session_id or _terminal_session_id_from_events(stream),
        agent=AtifAgent(
            name="grok-build",
            version=context.agent_version,
            model_name=context.model or None,
        ),
        extra=_parse_extra(context, chat, stream, issues),
    )
    has_native_prompts = any(
        event.get("type") == "user" and isinstance(event.get("prompt_index"), int) for event in chat
    )
    if not has_native_prompts:
        builder.add(source="user", message=context.instruction)
    reasoning: list[str] = []
    for event in chat:
        if event.get("type") == "user" and isinstance(event.get("prompt_index"), int):
            builder.add(source="user", message=_user_prompt(event.get("content")))
            continue
        if event.get("type") == "reasoning":
            reasoning.extend(
                str(item.get("text") or "")
                for item in event.get("summary") or ()
                if isinstance(item, dict)
            )
            continue
        if event.get("type") != "assistant":
            continue
        calls: list[AtifToolCall] = []
        observations: list[AtifObservationResult] = []
        for raw_call in event.get("tool_calls") or ():
            if not isinstance(raw_call, dict):
                continue
            call_id = str(raw_call.get("id") or "")
            arguments = _arguments(raw_call.get("arguments"))
            name = str(raw_call.get("name") or "")
            if name == "use_tool" and isinstance(arguments.get("tool_name"), str):
                name = arguments.pop("tool_name")
                arguments = arguments.get("tool_input") or {}
            calls.append(
                AtifToolCall(
                    tool_call_id=call_id,
                    function_name=name,
                    arguments=arguments,
                    extra=_mcp_extra(name),
                )
            )
            if call_id in results:
                observations.append(
                    AtifObservationResult(
                        source_call_id=call_id,
                        content=_content(results[call_id].get("content"), context),
                        extra=({"ale": {"is_error": True}} if errors.get(call_id, False) else None),
                    )
                )
        message = _message(event.get("content"))
        if message or reasoning or calls:
            builder.add(
                source="agent",
                message=message or "",
                reasoning_content="\n".join(reasoning).strip() or None,
                tool_calls=calls or None,
                observation=AtifObservation(results=observations) if observations else None,
            )
            reasoning.clear()
    if not any(step.source == "agent" for step in builder.steps):
        message = _stream_final_message_from_events(stream) or context.final_message
        if message is not None:
            builder.add(source="agent", message=message)
    return builder.build()


def _terminal_session_id(text: str) -> str | None:
    return _terminal_session_id_from_events(
        [event for line in text.splitlines() if (event := _json(line)) is not None]
    )


def _native_error(text: str) -> str | None:
    for line in reversed(text.splitlines()):
        event = _json(line)
        if event is None:
            continue
        if event.get("type") == "error":
            return (
                error_text(event.get("error") or event.get("message") or event.get("data"))
                or "native error"
            )
        if event.get("type") == "end":
            return error_text(event.get("error")) or None
    return None


def _terminal_session_id_from_events(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        value = event.get("sessionId")
        if event.get("type") in {"end", "error"} and isinstance(value, str):
            return value
    return None


def _stream_final_message(text: str) -> str | None:
    return _stream_final_message_from_events(
        [event for line in text.splitlines() if (event := _json(line)) is not None]
    )


def _stream_final_message_from_events(events: list[dict[str, Any]]) -> str | None:
    text = "".join(
        str(event.get("data") or "") for event in events if event.get("type") == "text"
    ).strip()
    return text or None


def _json(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _tool_errors(events: list[dict[str, Any]]) -> dict[str, bool]:
    errors: dict[str, bool] = {}
    for event in events:
        update = (event.get("params") or {}).get("update") or {}
        if update.get("sessionUpdate") == "tool_call_update":
            call_id = update.get("toolCallId")
            if isinstance(call_id, str):
                errors[call_id] = str(update.get("status") or "").lower() in {
                    "failed",
                    "error",
                    "cancelled",
                }
    return errors


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {"value": value}


def _message(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        )
    return ""


def _user_prompt(value: Any) -> str:
    text = _message(value)
    match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
    return match.group(1) if match else text


def _content(value: Any, context: TrajectoryParseContext) -> str | list[AtifContentPart]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    parts: list[AtifContentPart] = []
    cursor = 0
    for match in _DATA_URL.finditer(text):
        if match.start() > cursor:
            parts.append(AtifContentPart(type="text", text=text[cursor : match.start()]))
        blob = context.blobs.put(base64.b64decode(match.group(2)), media_type=match.group(1))
        parts.append(
            AtifContentPart(
                type="image",
                source=AtifImageSource(media_type=match.group(1), path=blob.path),
            )
        )
        cursor = match.end()
    if not parts:
        return text
    if cursor < len(text):
        parts.append(AtifContentPart(type="text", text=text[cursor:]))
    return parts


def _mcp_extra(name: str) -> dict[str, Any] | None:
    logical = name.removeprefix("mcp__")
    if "__" not in logical:
        return None
    server, tool = logical.split("__", 1)
    return {"ale": {"mcp": {"server": server, "tool": tool}}}


def _parse_extra(
    context: TrajectoryParseContext,
    chat: list[dict[str, Any]],
    stream: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    if issues:
        data["parse_issues"] = issues
    if not chat:
        data["limitations"] = [
            "Native tool and conversation records are unavailable; stdout text only."
        ]
    return {"ale": data} if data else None
