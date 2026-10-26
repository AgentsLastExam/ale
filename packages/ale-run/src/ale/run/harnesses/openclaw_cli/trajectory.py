"""Pure conversion of OpenClaw CLI evidence to ATIF."""

from __future__ import annotations

import base64
import json
from pathlib import PurePosixPath
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
    unconverted,
)

#: ALE always runs OpenClaw as agent `main`.
SESSIONS = ".openclaw-ale/agents/main/sessions"


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    return path == PurePosixPath(SESSIONS, f"{session_id}.jsonl")


def _is_transcript(path: PurePosixPath) -> bool:
    """A conversation, ``<session id>.jsonl``, rather than something OpenClaw keeps beside it.

    The directory also holds the session index, a runtime trace and its pointer per session,
    write locks and cached prompts. A transcript is append-only and carries its own
    compaction summaries, so nothing else holds conversation.
    """
    return path.parent == PurePosixPath(SESSIONS) and path.suffixes == [".jsonl"]


def read_result(stdout: str) -> NativeResult:
    envelope = _json_object(stdout)
    if envelope is None:
        # Not a result at all; the exit code or the missing session ID reports it.
        return NativeResult(None, None)
    return NativeResult(
        _envelope_session(envelope), _envelope_message(envelope), _native_error(envelope)
    )


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    envelopes = [_json_object(item.stdout) for item in recorded]
    expected = requested_session_id(context, recorded) or next(
        (
            session_id
            for envelope in reversed(envelopes)
            if envelope is not None and (session_id := _envelope_session(envelope))
        ),
        None,
    )
    files = mirrored_files(context, SESSIONS)
    matches = [
        path
        for path in files
        if expected
        and is_session_file(PurePosixPath(path.relative_to(context.logs_dir).as_posix()), expected)
    ]
    events: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    if len(matches) == 1:
        events, issues = native_events(context, matches[0])
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
    transcripts = {
        path
        for path in files
        if _is_transcript(PurePosixPath(path.relative_to(context.logs_dir).as_posix()))
    }
    issues.extend(unconverted(context, transcripts - set(matches[:1])))
    # Each native prompt is one invocation, in order; any invocation after the last one the
    # session holds is rebuilt from its own stdout.
    native_turns = max(
        sum(1 for event in events if _user_text(event)),
        1 if any(_is_message(event, "assistant") for event in events) else 0,
    )
    if events and native_turns < len(recorded):
        issues.append({"reason": "prompt_not_in_native_session", "invocation": native_turns + 1})
    results = _results(events, context)
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=expected,
        agent=AtifAgent(
            name="openclaw-cli",
            version=context.agent_version,
            model_name=context.model or None,
        ),
        extra=_extra(context, issues),
    )
    turn = 0
    totals = {"input": 0, "output": 0, "cacheRead": 0, "cost": 0.0}
    for event in events:
        if text := _user_text(event):
            if turn:
                _failure(builder, envelopes[min(turn, len(envelopes)) - 1])
            builder.add(source="user", message=text)
            turn += 1
            continue
        if not _is_message(event, "assistant"):
            continue
        message = event.get("message") or {}
        blocks = message.get("content") or []
        if not turn:
            builder.add(source="user", message=recorded[0].prompt)
            turn = 1
        text: list[str] = []
        reasoning: list[str] = []
        calls: list[AtifToolCall] = []
        observations: list[AtifObservationResult] = []
        for block in blocks if isinstance(blocks, list) else ():
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text.append(str(block.get("text") or ""))
            elif kind == "thinking":
                reasoning.append(str(block.get("thinking") or ""))
            elif kind in {"toolCall", "tool_use"}:
                call_id = str(block.get("id") or "")
                name = str(block.get("name") or "")
                calls.append(
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name=name,
                        arguments=_arguments(block.get("arguments") or block.get("input")),
                        extra=_mcp_extra(name),
                    )
                )
                if call_id in results:
                    observations.append(results[call_id])
        usage = message.get("usage") or {}
        cost = (usage.get("cost") or {}).get("total") if isinstance(usage, dict) else None
        for key in ("input", "output", "cacheRead"):
            totals[key] += int(usage.get(key) or 0)
        totals["cost"] += float(cost or 0.0)
        builder.add(
            source="agent",
            message="\n".join(text),
            reasoning_content="\n".join(reasoning) or None,
            tool_calls=calls or None,
            observation=AtifObservation(results=observations) if observations else None,
            metrics=(
                AtifMetrics(
                    prompt_tokens=int(usage.get("input") or 0),
                    completion_tokens=int(usage.get("output") or 0),
                    cached_tokens=int(usage.get("cacheRead") or 0),
                    cost_usd=float(cost) if cost is not None else None,
                )
                if usage
                else None
            ),
        )
    if events and not turn:
        builder.add(source="user", message=recorded[0].prompt)
        turn = 1
    if turn:
        _failure(builder, envelopes[min(turn, len(envelopes)) - 1])
    # Without the native session, each turn is its prompt and the reply it printed.
    for item, envelope in zip(recorded[turn:], envelopes[turn:], strict=True):
        builder.add(source="user", message=item.prompt)
        if envelope is not None and (message := _envelope_message(envelope)):
            builder.add(source="agent", message=message)
        _failure(builder, envelope)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    final_metrics = (
        AtifFinalMetrics(
            total_prompt_tokens=int(totals["input"]),
            total_completion_tokens=int(totals["output"]),
            total_cached_tokens=int(totals["cacheRead"]),
            total_cost_usd=float(totals["cost"]),
            total_steps=len(builder.steps),
        )
        if any(totals.values())
        else None
    )
    trajectory = builder.build(final_metrics=final_metrics)
    if not events:
        extra = dict(trajectory.extra or {})
        ale = dict(extra.get("ale") or {})
        ale["limitations"] = [
            "Only CLI response summaries are available; native tool history is absent."
        ]
        extra["ale"] = ale
        trajectory = trajectory.model_copy(update={"extra": extra})
    return trajectory


def _is_message(event: dict[str, Any], role: str) -> bool:
    return event.get("type") == "message" and (event.get("message") or {}).get("role") == role


def _user_text(event: dict[str, Any]) -> str:
    if not _is_message(event, "user"):
        return ""
    return _blocks_text(event["message"].get("content") or [])


def _failure(builder: TrajectoryBuilder, envelope: dict[str, Any] | None) -> None:
    """A failure only the invocation's printed result reports, at the end of its turn."""
    if envelope is not None and (error := _native_error(envelope)):
        builder.add(source="system", message=error)


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _envelope_session(envelope: dict[str, Any]) -> str | None:
    value = ((envelope.get("meta") or {}).get("agentMeta") or {}).get("sessionId")
    return value if isinstance(value, str) else None


def _envelope_message(envelope: dict[str, Any]) -> str | None:
    text = "\n".join(
        str(payload.get("text") or "")
        for payload in envelope.get("payloads") or ()
        if isinstance(payload, dict)
    ).strip()
    return text or None


def _native_error(envelope: dict[str, Any]) -> str | None:
    if envelope.get("error") or envelope.get("status") in {"error", "failed"}:
        return error_text(envelope.get("error") or envelope.get("message")) or "native error"
    meta = envelope.get("meta")
    if isinstance(meta, dict) and (meta.get("aborted") or meta.get("error")):
        return error_text(meta.get("error") or meta.get("abortReason")) or "native run aborted"
    failures = [
        str(payload.get("text") or "native error")
        for payload in envelope.get("payloads") or ()
        if isinstance(payload, dict) and payload.get("isError") is True
    ]
    return "; ".join(failures) or None


def _results(
    events: list[dict[str, Any]], context: TrajectoryParseContext
) -> dict[str, AtifObservationResult]:
    results: dict[str, AtifObservationResult] = {}
    for event in events:
        if event.get("type") == "tool_result":
            call_id = str(event.get("call_id") or event.get("tool_use_id") or "")
            value = event.get("output") or ""
            error = bool(event.get("is_error"))
        elif event.get("type") == "message":
            message = event.get("message") or {}
            role = message.get("role")
            if role not in {"user", "toolResult"}:
                continue
            for block in message.get("content") or ():
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = str(block.get("tool_use_id") or block.get("call_id") or "")
                results[call_id] = AtifObservationResult(
                    source_call_id=call_id,
                    content=_block_content(block.get("content"), context),
                    extra={"ale": {"is_error": True}} if block.get("is_error") else None,
                )
            if role != "toolResult":
                continue
            call_id = str(message.get("toolCallId") or "")
            value = message.get("content") or event.get("output") or ""
            error = bool(message.get("isError"))
        else:
            continue
        if call_id:
            results[call_id] = AtifObservationResult(
                source_call_id=call_id,
                content=_block_content(value, context),
                extra={"ale": {"is_error": True}} if error else None,
            )
    return results


def _block_content(value: Any, context: TrajectoryParseContext) -> str | list[AtifContentPart]:
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
            blob = context.blobs.put(base64.b64decode(block["data"]), media_type=media_type)
            parts.append(
                AtifContentPart(
                    type="image",
                    source=AtifImageSource(media_type=media_type, path=blob.path),
                )
            )
    return parts or json.dumps(value, ensure_ascii=False, default=str)


def _blocks_text(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks.strip()
    return "\n".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(blocks, list)
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {"value": value}


def _mcp_extra(name: str) -> dict[str, Any] | None:
    logical = name.removeprefix("mcp__")
    if "__" not in logical:
        return None
    server, tool = logical.split("__", 1)
    return {"ale": {"mcp": {"server": server, "tool": tool}}}


def _extra(context: TrajectoryParseContext, issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"ale": data} if data else None
