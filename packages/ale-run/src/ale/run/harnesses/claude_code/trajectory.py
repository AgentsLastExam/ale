"""Pure conversion of native evidence to ATIF."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from ale.core.blob import InlineText
from ale.core.errors import (
    TrajectoryReferenceError,
)
from ale.core.harness import (
    TrajectoryParseContext,
)
from ale.core.trace import DesktopAction
from ale.core.trajectory import (
    AtifAgent,
    AtifContentPart,
    AtifImageSource,
    AtifObservation,
    AtifObservationResult,
    AtifSubagentTrajectoryRef,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from ale.run.harnesses._diagnostics import error_text
from ale.run.harnesses.base import (
    Invocation,
    NativeResult,
    invocations,
    mirrored_files,
    native_events,
    requested_session_id,
    stdout_events,
    unconverted,
)
from ale.run.tools import CUA_DESKTOP_NAME

PROJECTS = ".claude-config/projects"


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    return path.parent.parent == PurePosixPath(PROJECTS) and path.name == f"{session_id}.jsonl"


def read_result(stdout: str) -> NativeResult:
    return NativeResult(_native_session_id(stdout), _final_message(stdout), _native_error(stdout))


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    turns, issues = stdout_events(recorded)
    turns = [[_attributed(event) for event in turn] for turn in turns]
    stream = [event for turn in turns for event in turn]
    expected = requested_session_id(context, recorded) or _event_session(stream)
    files = mirrored_files(context, PROJECTS)
    roots = [
        path
        for path in files
        if expected
        and is_session_file(PurePosixPath(path.relative_to(context.logs_dir).as_posix()), expected)
    ]
    consumed: set[Path] = set()
    events = _with_prompts(recorded, turns)
    if len(roots) == 1:
        native, native_issues = native_events(context, roots[0])
        consumed.add(roots[0])
        issues.extend(native_issues)
        observed = _event_session(native)
        if observed and observed != expected:
            issues.append(
                {"reason": "session_id_mismatch", "expected": expected, "actual": observed}
            )
        else:
            # Native records carry the conversation, prompts included; stdout adds the
            # terminal events only it has, each after the record that preceded it there.
            events = _merge(native, stream)
            if not any(_is_prompt(event) for event in native):
                events = _with_prompts(recorded[:1], [[]]) + events
            sidechains: list[dict[str, Any]] = []
            covered: set[str] = set()
            for child_path in sorted(roots[0].with_suffix("").rglob("*.jsonl")):
                children, child_issues = native_events(context, child_path)
                consumed.add(child_path)
                issues.extend(child_issues)
                meta = child_path.with_suffix(".meta.json")
                if not (parent := _spawning_call(meta)):
                    issues.append(
                        {
                            "reason": "unlinked_subagent",
                            "source": child_path.relative_to(context.logs_dir).as_posix(),
                        }
                    )
                    continue
                consumed.add(meta)
                covered.add(parent)
                agent_id = child_path.stem.removeprefix("agent-")
                sidechains.extend(
                    {
                        **event,
                        "isSidechain": True,
                        "agentId": event.get("agentId") or agent_id,
                        "parentToolUseID": parent,
                    }
                    for event in children
                )
            # A subagent's own record replaces what stdout showed of it.
            events = [
                event for event in events if event.get("parent_tool_use_id") not in covered
            ] + sidechains
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
    issues.extend(unconverted(context, set(files) - consumed))
    return _parse_events(context, events, issues, instruction=None)


def _with_prompts(
    recorded: list[Invocation], turns: list[list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Stdout alone, with each invocation's prompt where the CLI did not echo it."""
    return [
        event
        for item, turn in zip(recorded, turns, strict=True)
        for event in (
            turn
            if any(_is_prompt(event) for event in turn)
            else [{"type": "user", "message": {"role": "user", "content": item.prompt}}, *turn]
        )
    ]


def _attributed(event: dict[str, Any]) -> dict[str, Any]:
    """A stdout event, marked as its subagent's when a subagent produced it.

    Stream-json names the delegating tool call instead of setting ``isSidechain``. One call
    runs one subagent, so that call's ID also names the agent.
    """
    parent = event.get("parent_tool_use_id")
    if isinstance(parent, str) and parent and not event.get("isSidechain"):
        return {**event, "isSidechain": True, "agentId": event.get("agentId") or parent}
    return event


def _is_prompt(event: dict[str, Any]) -> bool:
    if event.get("isSidechain"):
        return False
    if event.get("type") != "user" or not isinstance(event.get("message"), dict):
        return False
    content = event["message"].get("content")
    return not (
        isinstance(content, list)
        and any(isinstance(part, dict) and part.get("type") == "tool_result" for part in content)
    )


def _merge(native: list[dict[str, Any]], stream: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positions = {_message_key(event): index for index, event in enumerate(native)}
    after: dict[int, list[dict[str, Any]]] = {}
    anchor = -1
    for event in stream:
        key = _message_key(event)
        if key in positions:
            anchor = max(anchor, positions[key])
        else:
            after.setdefault(anchor, []).append(event)
    merged = list(after.get(-1, ()))
    for index, event in enumerate(native):
        merged.append(event)
        merged.extend(after.get(index, ()))
    return merged


def _message_key(event: dict[str, Any]) -> str:
    message = event.get("message")
    if isinstance(message, dict):
        if message.get("id"):
            return f"{event.get('type')}:{message['id']}"
        return json.dumps(
            [event.get("type"), message.get("role"), message.get("content")], sort_keys=True
        )
    return json.dumps(event, sort_keys=True)


def _parse_events(
    context: TrajectoryParseContext,
    events: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    *,
    instruction: str | None,
) -> AtifTrajectory:
    """Convert ordered events; a subagent's delegated ``instruction`` opens its trajectory."""
    sidechains = _sidechain_groups(events)
    events = [event for event in events if not event.get("isSidechain")]
    results: dict[str, tuple[Any, bool]] = {}
    for event in events:
        if event.get("type") != "user":
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool_result":
                continue
            call_id = str(part.get("tool_use_id") or "")
            if not call_id:
                raise TrajectoryReferenceError("Claude tool result has no tool_use_id")
            if call_id in results:
                raise TrajectoryReferenceError(f"duplicate Claude tool result for {call_id!r}")
            results[call_id] = (
                event.get("tool_use_result", part.get("content")),
                part.get("is_error") is True,
            )

    root_extra: dict[str, Any] = {}
    if issues:
        root_extra["parse_issues"] = issues
    if context.incomplete:
        root_extra.update(
            {
                "incomplete": True,
                "incomplete_reason": context.incomplete_reason or "interrupted",
            }
        )
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=context.session_id,
        agent=AtifAgent(
            name="claude-code",
            version=context.agent_version,
            model_name=context.model or None,
        ),
        extra={"ale": root_extra} if root_extra else None,
    )
    instruction_added = False

    def ensure_instruction() -> None:
        nonlocal instruction_added
        if instruction is not None and not instruction_added:
            builder.add(source="user", message=instruction)
            instruction_added = True

    known_calls: set[str] = set()
    native_session_id = context.session_id
    for event in events:
        kind = event.get("type")
        timestamp = event.get("timestamp") if isinstance(event.get("timestamp"), str) else None
        if kind == "system":
            message, attachments = _atif_content(
                event.get("message") or event.get("content") or "",
                context,
            )
            if message:
                builder.add(
                    source="system",
                    message=message,
                    timestamp=timestamp,
                    extra=_step_extra(event, attachments=attachments),
                )
            continue
        if kind == "user":
            content = (event.get("message") or {}).get("content")
            if isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "tool_result" for part in content
            ):
                continue
            message, attachments = _atif_content(content or "", context)
            if message:
                if instruction is not None and _content_text(message) == instruction:
                    if instruction_added:
                        continue
                    instruction_added = True
                else:
                    ensure_instruction()
                builder.add(
                    source="user",
                    message=message,
                    timestamp=timestamp,
                    extra=_step_extra(event, attachments=attachments),
                )
            continue
        if kind == "assistant":
            ensure_instruction()
            message = event.get("message") or {}
            content = message.get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            calls: list[AtifToolCall] = []
            observations: list[AtifObservationResult] = []
            attachments: list[dict[str, Any]] = []
            for part in content if isinstance(content, list) else ():
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "text":
                    rendered, refs = _atif_content(str(part.get("text") or ""), context)
                    text_parts.append(rendered if isinstance(rendered, str) else "")
                    attachments.extend(refs)
                    continue
                if part_type in {"thinking", "reasoning"}:
                    reasoning_parts.append(str(part.get("thinking") or part.get("text") or ""))
                    continue
                if part_type != "tool_use":
                    continue
                call_id = str(part.get("id") or "")
                if not call_id:
                    raise TrajectoryReferenceError("Claude tool call has no id")
                if call_id in known_calls:
                    raise TrajectoryReferenceError(f"duplicate Claude tool call {call_id!r}")
                known_calls.add(call_id)
                name = str(part.get("name") or "")
                arguments = part.get("input") if isinstance(part.get("input"), dict) else {}
                extra = _tool_extra(name, arguments)
                calls.append(
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name=name,
                        arguments=arguments,
                        extra=extra,
                    )
                )
                if call_id in results:
                    value, is_error = results[call_id]
                    rendered, refs = _atif_content(value, context)
                    result_extra: dict[str, Any] = {}
                    if refs:
                        result_extra["attachments"] = refs
                    if is_error:
                        result_extra["is_error"] = True
                    observations.append(
                        AtifObservationResult(
                            source_call_id=call_id,
                            content=rendered,
                            extra={"ale": result_extra} if result_extra else None,
                        )
                    )
            builder.add(
                source="agent",
                message="".join(text_parts),
                timestamp=timestamp,
                model_name=message.get("model") if isinstance(message.get("model"), str) else None,
                reasoning_content="".join(reasoning_parts) or None,
                tool_calls=calls or None,
                observation=AtifObservation(results=observations) if observations else None,
                extra=_step_extra(event, attachments=attachments),
            )
            continue
        if kind == "result":
            ensure_instruction()
            if isinstance(event.get("session_id"), str):
                native_session_id = event["session_id"]
            final = _text_of(event)
            if final and (
                not builder.steps
                or builder.steps[-1].source != "agent"
                or builder.steps[-1].message != final
            ):
                builder.add(
                    source="agent",
                    message=final,
                    timestamp=timestamp,
                    extra=_step_extra(event),
                )

    orphan_results = sorted(set(results) - known_calls)
    if orphan_results:
        raise TrajectoryReferenceError(
            f"Claude tool results reference unknown calls: {', '.join(orphan_results)}"
        )
    ensure_instruction()
    if (
        not any(step.source == "agent" for step in builder.steps)
        and context.final_message is not None
    ):
        builder.add(source="agent", message=context.final_message)
    trajectory = builder.build()
    if native_session_id and trajectory.session_id != native_session_id:
        trajectory = trajectory.model_copy(update={"session_id": native_session_id})
    if sidechains:
        trajectory = _embed_sidechains(trajectory, sidechains, context)
        return AtifTrajectory.model_validate(trajectory.model_dump())
    return trajectory


def _embed_sidechains(
    root: AtifTrajectory,
    groups: dict[str, list[dict[str, Any]]],
    context: TrajectoryParseContext,
) -> AtifTrajectory:
    embedded: list[AtifTrajectory] = []
    refs_by_call: dict[str, list[AtifSubagentTrajectoryRef]] = {}
    for agent_id, events in sorted(groups.items()):
        parent_call = _sidechain_parent(events)
        if parent_call is None:
            raise TrajectoryReferenceError(f"Claude subagent {agent_id!r} has no parent tool call")
        trajectory_id = f"{context.trajectory_id}-subagent-{_safe_id(agent_id)}"
        normalized = [
            {
                key: value
                for key, value in event.items()
                if key
                not in {
                    "isSidechain",
                    "agentId",
                    "parentToolUseID",
                    "parent_tool_use_id",
                }
            }
            for event in events
        ]
        child = _parse_events(
            replace(context, trajectory_id=trajectory_id, session_id=_event_session(events)),
            normalized,
            [],
            instruction=_delegated_instruction(root, parent_call),
        )
        embedded.append(child)
        refs_by_call.setdefault(parent_call, []).append(
            AtifSubagentTrajectoryRef(
                trajectory_id=trajectory_id,
                session_id=child.session_id,
            )
        )

    linked: set[str] = set()
    steps = []
    for step in root.steps:
        calls = {call.tool_call_id for call in step.tool_calls or ()}
        matching = calls & refs_by_call.keys()
        if not matching:
            steps.append(step)
            continue
        results = list(step.observation.results if step.observation else ())
        by_call = {
            result.source_call_id: index
            for index, result in enumerate(results)
            if result.source_call_id is not None
        }
        for call_id in sorted(matching):
            linked.add(call_id)
            refs = refs_by_call[call_id]
            if call_id in by_call:
                index = by_call[call_id]
                current = results[index]
                results[index] = current.model_copy(
                    update={
                        "subagent_trajectory_ref": [
                            *(current.subagent_trajectory_ref or ()),
                            *refs,
                        ]
                    }
                )
            else:
                results.append(
                    AtifObservationResult(
                        source_call_id=call_id,
                        subagent_trajectory_ref=refs,
                    )
                )
        steps.append(step.model_copy(update={"observation": AtifObservation(results=results)}))
    unresolved = sorted(refs_by_call.keys() - linked)
    if unresolved:
        raise TrajectoryReferenceError(
            "Claude subagents reference unknown parent calls: " + ", ".join(unresolved)
        )
    return root.model_copy(
        update={
            "steps": steps,
            "subagent_trajectories": [
                *(root.subagent_trajectories or ()),
                *embedded,
            ],
        }
    )


def _final_message(transcript: str) -> str | None:
    """The last thing the agent said, which is what a human reads first."""
    for event in reversed(_stream_events(transcript)):
        if event.get("type") in {"assistant", "result"}:
            return _text_of(event)
    return None


def _stream_events(transcript: str) -> list[dict[str, Any]]:
    events = []
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _native_error(transcript: str) -> str | None:
    for event in reversed(_stream_events(transcript)):
        if event.get("type") == "result":
            subtype = str(event.get("subtype") or "")
            if event.get("is_error") or subtype.startswith("error"):
                detail = error_text(
                    event.get("errors") or event.get("error") or event.get("result")
                )
                return f"{subtype or 'native error'}: {detail}".rstrip(": ")
            return None
        if event.get("type") == "error" or (
            event.get("type") == "assistant" and event.get("error")
        ):
            return error_text(event.get("error") or event.get("message")) or "native error"
    return None


def _atif_content(
    value: Any, context: TrajectoryParseContext
) -> tuple[str | list[AtifContentPart], list[dict[str, Any]]]:
    attachments: list[dict[str, Any]] = []
    if isinstance(value, str):
        retained = context.blobs.text(value)
        if isinstance(retained, InlineText):
            return retained.inline, attachments
        attachments.append(retained.model_dump(mode="json"))
        return f"[large text retained at {retained.path}]", attachments
    if isinstance(value, dict) and value.get("type") == "image":
        value = [value]
    if isinstance(value, list):
        parts: list[AtifContentPart] = []
        for part in value:
            if not isinstance(part, dict):
                parts.append(AtifContentPart(type="text", text=str(part)))
                continue
            if part.get("type") == "text":
                text, refs = _atif_content(str(part.get("text") or ""), context)
                attachments.extend(refs)
                if isinstance(text, str):
                    parts.append(AtifContentPart(type="text", text=text))
                else:
                    parts.extend(text)
                continue
            if part.get("type") == "image":
                source = part.get("source") if isinstance(part.get("source"), dict) else {}
                media_type = str(source.get("media_type") or "image/png")
                encoded = source.get("data")
                if isinstance(encoded, str):
                    data = _decode_base64(encoded, "image")
                    ref = context.blobs.put(data, media_type=media_type)
                    parts.append(
                        AtifContentPart(
                            type="image",
                            source=AtifImageSource(
                                media_type=media_type,  # type: ignore[arg-type]
                                path=ref.path,
                            ),
                        )
                    )
                    continue
            source = (
                part.get("source")
                if isinstance(part.get("source"), dict)
                else part.get("resource")
                if isinstance(part.get("resource"), dict)
                else {}
            )
            encoded = source.get("data") or source.get("blob")
            media_type = str(
                source.get("media_type")
                or source.get("mime_type")
                or source.get("mimeType")
                or part.get("media_type")
                or "application/octet-stream"
            )
            if isinstance(encoded, str):
                data = _decode_base64(encoded, str(part.get("type") or "attachment"))
                ref = context.blobs.put(data, media_type=media_type)
                if media_type.startswith("image/"):
                    parts.append(
                        AtifContentPart(
                            type="image",
                            source=AtifImageSource(
                                media_type=media_type,  # type: ignore[arg-type]
                                path=ref.path,
                            ),
                        )
                    )
                else:
                    attachments.append(ref.model_dump(mode="json"))
                    parts.append(
                        AtifContentPart(
                            type="text",
                            text=f"[{media_type} retained at {ref.path}]",
                        )
                    )
                continue
            rendered = json.dumps(part, ensure_ascii=False, sort_keys=True)
            text, refs = _atif_content(rendered, context)
            attachments.extend(refs)
            parts.append(
                AtifContentPart(
                    type="text",
                    text=text if isinstance(text, str) else rendered,
                )
            )
        return parts, attachments
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return _atif_content(rendered, context)


def _step_extra(
    event: dict[str, Any], *, attachments: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    ale: dict[str, Any] = {}
    message = event.get("message")
    native_id = (message.get("id") if isinstance(message, dict) else None) or event.get("uuid")
    if native_id:
        ale["native_event_ids"] = [str(native_id)]
    if attachments:
        ale["attachments"] = attachments
    return {"ale": ale} if ale else None


def _tool_extra(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if not name.startswith("mcp__"):
        return None
    _, server, tool = [*name.split("__", 2), "", ""][:3]
    ale: dict[str, Any] = {"mcp": {"server": server, "tool": tool}}
    if server == CUA_DESKTOP_NAME:
        try:
            action = _cua_action(tool, arguments)
        except ValidationError:
            action = None
        if action is not None:
            ale["normalized_action"] = action.model_dump(mode="json", exclude_none=True)
    return {"ale": ale}


def _text_of(event: dict[str, Any]) -> str:
    message = event.get("message") or event
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(event.get("result", ""))


def _native_session_id(transcript: str) -> str | None:
    for event in reversed(_stream_events(transcript)):
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def _decode_base64(value: str, kind: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise TrajectoryReferenceError(f"Claude {kind} contains invalid base64") from exc


def _sidechain_groups(
    events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if not event.get("isSidechain"):
            continue
        agent_id = event.get("agentId") or event.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise TrajectoryReferenceError("Claude sidechain event has no agentId")
        groups.setdefault(agent_id, []).append(event)
    return groups


def _spawning_call(meta: Path) -> str | None:
    """The tool call that started a native subagent, as its ``.meta.json`` names it."""
    try:
        value = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    call = value.get("toolUseId") if isinstance(value, dict) else None
    return call if isinstance(call, str) and call else None


def _sidechain_parent(events: list[dict[str, Any]]) -> str | None:
    parents = {
        value
        for event in events
        for value in (
            event.get("parentToolUseID"),
            event.get("parent_tool_use_id"),
        )
        if isinstance(value, str) and value
    }
    if len(parents) > 1:
        raise TrajectoryReferenceError("Claude subagent has multiple parent tool calls")
    return next(iter(parents), None)


def _delegated_instruction(root: AtifTrajectory, call_id: str) -> str:
    for step in root.steps:
        for call in step.tool_calls or ():
            if call.tool_call_id != call_id:
                continue
            for key in ("prompt", "instruction", "task", "description"):
                value = call.arguments.get(key)
                if isinstance(value, str):
                    return value
            return json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
    return ""


def _event_session(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        value = event.get("session_id") or event.get("sessionId")
        if isinstance(value, str) and value:
            return value
    return None


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return normalized or "unknown"


def _content_text(value: str | list[AtifContentPart]) -> str:
    if isinstance(value, str):
        return value
    return "".join(part.text or "" for part in value if part.type == "text")


def _cua_action(tool: str, arguments: dict[str, Any]) -> DesktopAction | None:
    action_type = {
        "screenshot": "screenshot",
        "click": "click",
        "mouse_move": "move",
        "drag": "drag",
        "mouse_down": "mouse_down",
        "mouse_up": "mouse_up",
        "scroll": "scroll",
        "type": "type",
        "key": "key",
        "key_down": "key_down",
        "key_up": "key_up",
        "hold_key": "hold_key",
        "cursor_position": "cursor_position",
        "wait": "wait",
    }.get(tool)
    if action_type is None:
        return None
    coordinate = arguments.get("coordinate")
    destination = None
    if tool == "drag":
        coordinate = arguments.get("start_coordinate")
        destination = arguments.get("coordinate")
    duration = arguments.get("duration")
    return DesktopAction(
        type=action_type,  # type: ignore[arg-type]
        coordinate=coordinate,
        to=destination,
        text=arguments.get("text"),
        keys=arguments.get("keys"),
        button=arguments.get("button"),
        clicks=arguments.get("clicks"),
        direction=arguments.get("direction"),
        amount=arguments.get("amount"),
        duration_ms=round(float(duration) * 1000) if duration is not None else None,
    )
