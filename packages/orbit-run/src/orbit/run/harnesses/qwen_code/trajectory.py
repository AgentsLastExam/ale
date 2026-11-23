"""Pure conversion of Qwen Code evidence to ATIF."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import datetime
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

HOME = ".qwen-ale"
PROJECTS = f"{HOME}/projects"
#: Qwen appends this to a prompt read from stdin before recording and sending it.
STDIN_SEPARATOR = "\n\n"
#: Qwen's wrapper for calling a deferred tool after ``tool_search`` reviewed it.
DEFERRED_CALL = "tool_call"
_IMAGE_TYPES = frozenset(("image/png", "image/jpeg", "image/gif", "image/webp"))


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    # projects/<project>/chats/<id>.jsonl, and projects/<project>/subagents/<id>/...
    if not path.is_relative_to(PROJECTS):
        return False
    parts = path.relative_to(PROJECTS).parts
    if len(parts) == 3 and parts[1] == "chats":
        return parts[2] == f"{session_id}.jsonl"
    return len(parts) > 3 and parts[1] == "subagents" and parts[2] == session_id


def read_result(stdout: str) -> NativeResult:
    events, _ = json_lines(stdout)
    result = _result(events)
    final = result.get("result") if result and not result.get("is_error") else None
    return NativeResult(
        _session_id(events),
        final if isinstance(final, str) and final else None,
        _turn_error(events),
    )


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    turns, issues = stdout_events(recorded)
    expected = requested_session_id(context, recorded) or _session_id(
        [event for turn in turns for event in turn]
    )
    files = mirrored_files(context, PROJECTS)
    root = context.logs_dir / PROJECTS
    chats = [
        path
        for path in files
        if expected
        and (parts := PurePosixPath(path.relative_to(root).as_posix()).parts)
        and len(parts) == 3
        and parts[1] == "chats"
        and parts[2] == f"{expected}.jsonl"
    ]
    records: list[dict[str, Any]] = []
    used: set[Path] = set()
    if len(chats) == 1:
        records, native_issues = native_events(context, chats[0])
        issues.extend(native_issues)
        used.add(chats[0])
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})

    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id, session_id=expected, agent=_agent(context)
    )
    totals = {"input": 0, "output": 0, "cached": 0}
    subagents: list[AtifTrajectory] = []
    children = chats[0].parent.parent / "subagents" / str(expected) if len(chats) == 1 else None
    if records and children is not None:
        conversation = _Conversation(context, str(expected), children, issues, used)
        subagents = conversation.walk(builder, None, records, turns=turns, totals=totals)
    # Each Task invocation is one prompt in order; any the session lacks is rebuilt from
    # its own prompt and stdout.
    native_turns = sum(1 for record in records if _is_prompt(record, root=True))
    if records and native_turns < len(recorded):
        issues.append({"reason": "prompt_not_in_native_session", "invocation": native_turns + 1})
    for item, stream in zip(recorded[native_turns:], turns[native_turns:], strict=True):
        builder.add(source="user", message=item.prompt)
        result = _result(stream)
        if result and not result.get("is_error") and isinstance(result.get("result"), str):
            builder.add(source="agent", message=result["result"])
        if error := _turn_error(stream):
            builder.add(source="system", message=error)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    transcripts = [path for path in files if path.suffix == ".jsonl"]
    if children is not None:
        issues.extend(
            {
                "reason": "unlinked_subagent",
                "source": path.relative_to(context.logs_dir).as_posix(),
            }
            for path in sorted(set(transcripts) - used)
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
    """One native record, before it becomes ATIF."""

    source: str
    message: str | list[AtifContentPart] = ""
    timestamp: str | None = None
    reasoning: list[str] = field(default_factory=list)
    calls: list[AtifToolCall] = field(default_factory=list)
    results: dict[str, AtifObservationResult] = field(default_factory=dict)
    metrics: AtifMetrics | None = None


@dataclass
class _Conversation:
    """Converts one session; a subagent is embedded in the agent whose call spawned it."""

    context: TrajectoryParseContext
    session_id: str
    children: Path
    issues: list[dict[str, Any]]
    used: set[Path]

    def walk(
        self,
        builder: TrajectoryBuilder,
        agent_id: str | None,
        records: list[dict[str, Any]],
        *,
        turns: list[list[dict[str, Any]]] | None = None,
        totals: dict[str, int] | None = None,
    ) -> list[AtifTrajectory]:
        """Add one agent's conversation to ``builder``; return the subagents it spawned.

        ``turns`` holds the root's per-invocation stdout, whose failures the session
        lacks; they are added at the end of their turn.
        """
        root = agent_id is None
        embedded, refs = self._children(builder.trajectory_id, agent_id, records)
        steps: list[_Step] = []
        owner: dict[str, _Step] = {}
        prompts = 0
        for record in records:
            kind = record.get("type")
            parts = _parts(record)
            if _is_prompt(record, root=root):
                if turns is not None and prompts and (error := _turn_error(turns[prompts - 1])):
                    steps.append(_Step("system", error))
                prompts += 1
                text = "".join(str(part.get("text") or "") for part in parts)
                if root and text.endswith(STDIN_SEPARATOR):
                    text = text.removesuffix(STDIN_SEPARATOR)
                steps.append(_Step("user", text, _timestamp(record)))
            elif kind == "user" or (kind == "system" and record.get("subtype") == "notification"):
                # Model-visible text the CLI injected rather than a prompt ALE sent.
                text = "".join(str(part.get("text") or "") for part in parts)
                if text:
                    steps.append(_Step("system", text, _timestamp(record)))
            elif kind == "system" and record.get("subtype") == "chat_compression":
                steps.append(_Step("system", "Context compressed.", _timestamp(record)))
            elif kind == "assistant":
                step = _Step("agent", timestamp=_timestamp(record))
                texts: list[str] = []
                for part in parts:
                    if isinstance(part.get("functionCall"), dict):
                        call = _call(part["functionCall"])
                        step.calls.append(call)
                        owner[call.tool_call_id] = step
                    elif part.get("thought"):
                        step.reasoning.append(str(part.get("text") or ""))
                    elif isinstance(part.get("text"), str):
                        texts.append(part["text"])
                step.message = "".join(texts)
                step.metrics = _metrics(record.get("usageMetadata"), totals)
                steps.append(step)
            elif kind == "tool_result":
                status = (record.get("toolCallResult") or {}).get("status")
                for part in parts:
                    response = part.get("functionResponse")
                    if not isinstance(response, dict):
                        continue
                    call_id = str(response.get("id") or "")
                    holder = owner.get(call_id)
                    if holder is None or call_id in holder.results:
                        continue
                    holder.results[call_id] = AtifObservationResult(
                        source_call_id=call_id,
                        content=self._content(response),
                        subagent_trajectory_ref=refs.get(call_id),
                        extra=(
                            {"ale": {"is_error": True}}
                            if status is not None and status != "success"
                            else None
                        ),
                    )
        if turns is not None and prompts and (error := _turn_error(turns[prompts - 1])):
            steps.append(_Step("system", error))
        for step in steps:
            _emit(builder, step)
        return embedded

    def _children(
        self, trajectory_id: str, agent_id: str | None, records: list[dict[str, Any]]
    ) -> tuple[list[AtifTrajectory], dict[str, list[AtifSubagentTrajectoryRef]]]:
        """Subagents spawned by ``agent_id`` (``None`` for the session), by spawning call.

        A subagent's ``.meta.json`` names the session, the agent that spawned it and the
        spawning call; its transcript beside it is its conversation. The reference sits on
        that call's result, so a spawn whose call has no result stays unlinked.
        """
        answered = {
            str(part["functionResponse"].get("id"))
            for record in records
            if record.get("type") == "tool_result"
            for part in _parts(record)
            if isinstance(part.get("functionResponse"), dict)
        }
        embedded: list[AtifTrajectory] = []
        refs: dict[str, list[AtifSubagentTrajectoryRef]] = {}
        metas = sorted(self.children.glob("*.meta.json")) if self.children.is_dir() else []
        for meta_path in metas:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            call_id, child_id = meta.get("toolUseId"), meta.get("agentId")
            transcript = meta_path.with_name(meta_path.name.removesuffix(".meta.json") + ".jsonl")
            if (
                meta.get("parentSessionId") != self.session_id
                or meta.get("parentAgentId") != agent_id
                or not isinstance(call_id, str)
                or call_id not in answered
                or not isinstance(child_id, str)
                or not child_id
                or transcript in self.used
                or not transcript.is_file()
            ):
                continue
            child_records, problems = native_events(self.context, transcript)
            if not any(record.get("agentId") == child_id for record in child_records):
                continue
            self.used.add(transcript)
            self.issues.extend(problems)
            child = TrajectoryBuilder(
                trajectory_id=f"{trajectory_id}-subagent-{child_id}",
                session_id=child_id,
                agent=_agent(self.context),
                extra=_extra(self.context, [], has_session=True),
            )
            nested = self.walk(child, child_id, child_records)
            if not child.steps:
                self.used.discard(transcript)
                continue
            embedded.append(child.build(subagent_trajectories=nested or None))
            refs.setdefault(call_id, []).append(
                AtifSubagentTrajectoryRef(trajectory_id=child.trajectory_id, session_id=child_id)
            )
        return embedded, refs

    def _content(self, response: dict[str, Any]) -> str | list[AtifContentPart]:
        """A function response's output, with its inline media copied to ALE's blobs."""
        body = response.get("response")
        if isinstance(body, dict) and set(body) == {"output"} and isinstance(body["output"], str):
            text = body["output"]
        else:
            text = json.dumps(body, ensure_ascii=False, default=str)
        media = [
            part["inlineData"]
            for part in response.get("parts") or ()
            if isinstance(part, dict) and isinstance(part.get("inlineData"), dict)
        ]
        if not media:
            return text
        parts = [AtifContentPart(type="text", text=text)]
        for item in media:
            media_type = item.get("mimeType")
            try:
                data = base64.b64decode(str(item.get("data") or ""), validate=True)
            except (binascii.Error, ValueError):
                data = b""
            if media_type in _IMAGE_TYPES and data:
                stored = self.context.blobs.put(data, media_type=media_type)
                parts.append(
                    AtifContentPart(
                        type="image",
                        source=AtifImageSource(media_type=media_type, path=stored.path),
                    )
                )
            else:
                label = item.get("displayName") or media_type or "media"
                parts.append(AtifContentPart(type="text", text=f"[media unavailable: {label}]"))
        return parts


def _emit(builder: TrajectoryBuilder, step: _Step) -> None:
    if step.source != "agent":
        builder.add(source=step.source, message=step.message, timestamp=step.timestamp)
        return
    reasoning = "".join(step.reasoning).strip() or None
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
    """A native call; a deferred-tool wrapper becomes the call it made."""
    call_id = str(raw.get("id") or "")
    name = str(raw.get("name") or "")
    arguments = raw.get("args") if isinstance(raw.get("args"), dict) else {}
    extra: dict[str, Any] = {}
    if name == DEFERRED_CALL and isinstance(arguments.get("name"), str):
        name = arguments["name"]
        inner = arguments.get("arguments")
        arguments = inner if isinstance(inner, dict) else {}
        extra["native_tool"] = DEFERRED_CALL
    server, separator, tool = name.removeprefix("mcp__").partition("__")
    if name.startswith("mcp__") and separator and server and tool:
        extra["mcp"] = {"server": server, "tool": tool}
    return AtifToolCall(
        tool_call_id=call_id,
        function_name=name,
        arguments=dict(arguments),
        extra={"ale": extra} if extra else None,
    )


def _is_prompt(record: dict[str, Any], *, root: bool) -> bool:
    """A prompt sent to this agent: ALE's for the session, the parent's for a subagent."""
    if record.get("type") != "user" or record.get("subtype"):
        return False
    return record.get("provenance") == "real_user" if root else True


def _parts(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    parts = message.get("parts") if isinstance(message, dict) else None
    return [part for part in parts or () if isinstance(part, dict)]


def _metrics(usage: Any, totals: dict[str, int] | None) -> AtifMetrics | None:
    if not isinstance(usage, dict):
        return None
    counts = {
        key: value if isinstance(value, int) and not isinstance(value, bool) else 0
        for key, value in (
            (key, usage.get(key))
            for key in ("promptTokenCount", "candidatesTokenCount", "cachedContentTokenCount")
        )
    }
    if totals is not None:
        totals["input"] += counts["promptTokenCount"]
        totals["output"] += counts["candidatesTokenCount"]
        totals["cached"] += counts["cachedContentTokenCount"]
    return AtifMetrics(
        prompt_tokens=counts["promptTokenCount"],
        completion_tokens=counts["candidatesTokenCount"],
        cached_tokens=counts["cachedContentTokenCount"],
    )


def _timestamp(record: dict[str, Any]) -> str | None:
    value = record.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((event for event in reversed(events) if event.get("type") == "result"), None)


def _session_id(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") == "result" or (
            event.get("type") == "system" and event.get("subtype") == "init"
        ):
            value = event.get("session_id")
            return value if isinstance(value, str) and value else None
    return None


def _turn_error(events: list[dict[str, Any]]) -> str | None:
    result = _result(events)
    if not result or not result.get("is_error"):
        return None
    return error_text(result.get("error")) or f"qwen-code ended with {result.get('subtype')}"


def _agent(context: TrajectoryParseContext) -> AtifAgent:
    return AtifAgent(
        name="qwen-code", version=context.agent_version, model_name=context.model or None
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
