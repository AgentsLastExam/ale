"""Pure conversion of OpenClaw CLI evidence to ATIF."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
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
    mirrored_files,
    native_events,
    requested_session_id,
    unconverted,
)

#: ALE always runs OpenClaw as agent `main`.
SESSIONS = ".openclaw-ale/agents/main/sessions"
#: OpenClaw's index of the sessions in `SESSIONS`, by session key.
INDEX = f"{SESSIONS}/sessions.json"
#: The token counts of a native ``usage`` record; any native price report is ignored.
_USAGE_KEYS = ("input", "cacheRead", "cacheWrite", "output")


@dataclass(frozen=True)
class _Session:
    """What the session index says about one session key."""

    session_id: str
    spawned_by: str | None


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
    consumed: set[Path] = set()
    if len(matches) == 1:
        events, issues = native_events(context, matches[0])
        consumed.add(matches[0])
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
    transcripts = {
        path
        for path in files
        if _is_transcript(PurePosixPath(path.relative_to(context.logs_dir).as_posix()))
    }
    # Without the root transcript no spawn can be found, so every child stays raw evidence.
    index = _session_index(context, issues) if events else {}
    subagents, spawned = _subagents(context, expected, events, index, transcripts, issues, consumed)
    issues.extend(unconverted(context, transcripts - consumed))
    # Each native prompt is one invocation, in order; any invocation after the last one the
    # session holds is rebuilt from its own stdout.
    native_turns = max(
        sum(1 for event in events if _user_text(event)),
        1 if any(_is_message(event, "assistant") for event in events) else 0,
    )
    if events and native_turns < len(recorded):
        issues.append({"reason": "prompt_not_in_native_session", "invocation": native_turns + 1})
    results = _results(events, context, spawned)
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=expected,
        agent=_agent(context),
        extra=_extra(context, issues),
    )
    turn = 0
    totals = dict.fromkeys(_USAGE_KEYS, 0)
    for event in events:
        if text := _user_text(event):
            if turn:
                _failure(builder, envelopes[min(turn, len(envelopes)) - 1])
            builder.add(source="user", message=text)
            turn += 1
            continue
        if not _is_message(event, "assistant"):
            continue
        if not turn:
            builder.add(source="user", message=recorded[0].prompt)
            turn = 1
        _assistant_step(builder, event, results, totals)
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
    trajectory = builder.build(
        final_metrics=_final_metrics(builder, totals), subagent_trajectories=subagents or None
    )
    if not events:
        extra = dict(trajectory.extra or {})
        ale = dict(extra.get("ale") or {})
        ale["limitations"] = [
            "Only CLI response summaries are available; native tool history is absent."
        ]
        extra["ale"] = ale
        trajectory = trajectory.model_copy(update={"extra": extra})
    return trajectory


def _subagents(
    context: TrajectoryParseContext,
    session_id: str | None,
    events: list[dict[str, Any]],
    index: dict[str, _Session],
    transcripts: set[Path],
    issues: list[dict[str, Any]],
    consumed: set[Path],
) -> tuple[list[AtifTrajectory], dict[str, list[AtifSubagentTrajectoryRef]]]:
    """The sessions ``session_id`` spawned, converted, with references keyed by spawning call.

    The session index names each child's transcript and the session key that spawned it,
    and the ``sessions_spawn`` result names the key of the child it started. A child that
    no spawn result names stays raw evidence with a parse issue.
    """
    parents = {key for key, entry in index.items() if entry.session_id == session_id}
    # Only a mirrored transcript is read, whatever session ID the index claims.
    by_name = {path.name: path for path in transcripts}
    children = {
        key: path
        for key, entry in index.items()
        if entry.spawned_by in parents
        and (path := by_name.get(f"{entry.session_id}.jsonl")) is not None
        and path not in consumed
    }
    embedded: list[AtifTrajectory] = []
    refs: dict[str, list[AtifSubagentTrajectoryRef]] = {}
    for call_id, key in _spawn_results(events):
        # Two keys may name one transcript; it is converted once.
        if (path := children.pop(key, None)) is None or path in consumed:
            continue
        child_id = index[key].session_id
        consumed.add(path)
        child_events, child_issues = native_events(context, path)
        child = _session_trajectory(
            replace(
                context,
                trajectory_id=f"{context.trajectory_id}-subagent-{child_id}",
                session_id=child_id,
            ),
            child_events,
            index,
            transcripts,
            issues,
            consumed,
        )
        if child is None:
            # Spawned, but nothing of its conversation was recorded.
            consumed.discard(path)
            continue
        issues.extend(child_issues)
        embedded.append(child)
        refs.setdefault(call_id, []).append(
            AtifSubagentTrajectoryRef(trajectory_id=child.trajectory_id, session_id=child_id)
        )
    issues.extend(
        {"reason": "unlinked_subagent", "source": path.relative_to(context.logs_dir).as_posix()}
        for path in sorted(set(children.values()) - consumed)
    )
    return embedded, refs


def _session_trajectory(
    context: TrajectoryParseContext,
    events: list[dict[str, Any]],
    index: dict[str, _Session],
    transcripts: set[Path],
    issues: list[dict[str, Any]],
    consumed: set[Path],
) -> AtifTrajectory | None:
    """A spawned session's transcript, which no ALE invocation or printed result describes.

    Its first user message is the task its parent delegated, as OpenClaw framed it.
    """
    subagents, spawned = _subagents(
        context, context.session_id, events, index, transcripts, issues, consumed
    )
    results = _results(events, context, spawned)
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=context.session_id,
        agent=_agent(context),
        extra=_extra(context, []),
    )
    totals = dict.fromkeys(_USAGE_KEYS, 0)
    for event in events:
        if text := _user_text(event):
            builder.add(source="user", message=text)
        elif _is_message(event, "assistant"):
            _assistant_step(builder, event, results, totals)
    if not builder.steps:
        return None
    return builder.build(
        final_metrics=_final_metrics(builder, totals), subagent_trajectories=subagents or None
    )


def _spawn_results(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Each ``sessions_spawn`` call that started a session, in order, with that session's key."""
    calls = {
        str(block.get("id") or "")
        for event in events
        if _is_message(event, "assistant")
        for block in event["message"].get("content") or ()
        if isinstance(block, dict) and block.get("type") in {"toolCall", "tool_use"}
    }
    found = []
    for event in events:
        if not _is_message(event, "toolResult"):
            continue
        message = event["message"]
        call_id = str(message.get("toolCallId") or "")
        details = message.get("details")
        if (
            message.get("toolName") == "sessions_spawn"
            and call_id in calls
            and isinstance(details, dict)
            and isinstance(details.get("childSessionKey"), str)
        ):
            found.append((call_id, details["childSessionKey"]))
    return found


def _session_index(
    context: TrajectoryParseContext, issues: list[dict[str, Any]]
) -> dict[str, _Session]:
    """``sessions.json``: each session key's transcript and, if spawned, its parent's key."""
    path = context.logs_dir / INDEX
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError as exc:
        issues.append({"source": INDEX, "reason": "malformed_json", "detail": str(exc)[:500]})
        return {}
    return {
        key: _Session(
            entry["sessionId"],
            entry.get("spawnedBy") if isinstance(entry.get("spawnedBy"), str) else None,
        )
        for key, entry in (value.items() if isinstance(value, dict) else ())
        if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str)
    }


def _assistant_step(
    builder: TrajectoryBuilder,
    event: dict[str, Any],
    results: dict[str, AtifObservationResult],
    totals: dict[str, int],
) -> None:
    """One assistant message as an agent step, adding its usage to ``totals``.

    Native ``input`` excludes cache reads and writes; ATIF prompt tokens include both.
    """
    message = event.get("message") or {}
    blocks = message.get("content") or []
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
    tokens = dict.fromkeys(_USAGE_KEYS, 0)
    if isinstance(usage, dict):
        for key in _USAGE_KEYS:
            tokens[key] = int(usage.get(key) or 0)
            totals[key] += tokens[key]
    builder.add(
        source="agent",
        message="\n".join(text),
        reasoning_content="\n".join(reasoning) or None,
        tool_calls=calls or None,
        observation=AtifObservation(results=observations) if observations else None,
        metrics=(
            AtifMetrics(
                prompt_tokens=_prompt_tokens(tokens),
                completion_tokens=tokens["output"],
                cached_tokens=tokens["cacheRead"],
            )
            if usage
            else None
        ),
    )


def _prompt_tokens(tokens: dict[str, int]) -> int:
    return tokens["input"] + tokens["cacheRead"] + tokens["cacheWrite"]


def _final_metrics(builder: TrajectoryBuilder, totals: dict[str, int]) -> AtifFinalMetrics | None:
    if not any(totals.values()):
        return None
    return AtifFinalMetrics(
        total_prompt_tokens=_prompt_tokens(totals),
        total_completion_tokens=totals["output"],
        total_cached_tokens=totals["cacheRead"],
        total_steps=len(builder.steps),
    )


def _agent(context: TrajectoryParseContext) -> AtifAgent:
    return AtifAgent(
        name="openclaw-cli", version=context.agent_version, model_name=context.model or None
    )


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
    events: list[dict[str, Any]],
    context: TrajectoryParseContext,
    spawned: dict[str, list[AtifSubagentTrajectoryRef]],
) -> dict[str, AtifObservationResult]:
    """Each call's result; a call that spawned sessions also references their trajectories."""
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
    for call_id, refs in spawned.items():
        results[call_id] = results[call_id].model_copy(update={"subagent_trajectory_ref": refs})
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
