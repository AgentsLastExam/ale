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
    AtifMetrics,
    AtifObservation,
    AtifObservationResult,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from ale.run.harnesses._diagnostics import error_text
from ale.run.harnesses.evidence import jsonl_events, stdout_segments, unconverted_native_files


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    envelopes = [
        value
        for segment in stdout_segments(context)
        if (value := _json_object(segment)) is not None
    ]
    expected = context.session_id
    if expected is None and envelopes:
        expected = ((envelopes[-1].get("meta") or {}).get("agentMeta") or {}).get("sessionId")
    events: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    if context.native_logs_enabled:
        root = context.logs_dir / "native/.openclaw-ale/agents"
        paths = sorted(root.glob("*/sessions/*.jsonl"))
        matches = [path for path in paths if path.name == f"{expected}.jsonl"] if expected else []
        if len(matches) == 1:
            events, issues = jsonl_events(context, matches[0])
        elif context.incomplete_reason != "running":
            issues.append({"reason": "native_session_unavailable", "session_id": expected})
        issues.extend(
            unconverted_native_files(
                context, (path for path in paths if len(matches) != 1 or path != matches[0])
            )
        )
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
    user_seen = False
    totals = {"input": 0, "output": 0, "cacheRead": 0, "cost": 0.0}
    for event in events:
        if event.get("type") != "message":
            continue
        message = event.get("message") or {}
        role = message.get("role")
        blocks = message.get("content") or []
        if role == "user":
            text = _blocks_text(blocks)
            if text:
                builder.add(source="user", message=text)
                user_seen = True
            continue
        if role != "assistant":
            continue
        if not user_seen:
            builder.add(source="user", message=context.instruction)
            user_seen = True
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
    if not user_seen:
        builder.add(source="user", message=context.instruction)
    if not any(step.source == "agent" for step in builder.steps):
        for envelope in envelopes:
            if message := _envelope_message(envelope):
                builder.add(source="agent", message=message)
        if not envelopes and context.final_message:
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


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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
