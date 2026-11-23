"""Pure conversion of ForgeCode evidence to ATIF."""

from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import PurePosixPath
from typing import Any

from ale.core.harness import TrajectoryParseContext
from ale.core.trajectory import (
    AtifAgent,
    AtifContentPart,
    AtifImageSource,
    AtifMetrics,
    AtifObservation,
    AtifObservationResult,
    AtifSubagentTrajectoryRef,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from ale.run.harnesses.base import NativeResult, invocations, requested_session_id

#: ``FORGE_CONFIG``.
STATE = ".forge-ale"
#: One JSON export of the whole conversation per invocation: ``<conversation id>/<time>.json``.
DUMPS = f"{STATE}/dumps"
#: Forge's MCP configuration, which names each server as declared.
MCP_CONFIG = f"{STATE}/.mcp.json"
_MARKER = re.compile(
    r"^● \[\d\d:\d\d:\d\d\] (Initialize|Continue|Finished) "
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*$",
    re.MULTILINE,
)
_EVENT = re.compile(r"^● \[\d\d:\d\d:\d\d\] ", re.MULTILINE)
_REQUEST_LIMIT = re.compile(r"Maximum request \((\d+)\) per turn achieved")
_IMAGE_URL = re.compile(r"data:(image/[a-z0-9.+-]+);base64,(.*)", re.DOTALL)


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    return path.parent == PurePosixPath(DUMPS, session_id) and path.suffix == ".json"


def read_result(stdout: str) -> NativeResult:
    """Forge exits 0 on most failures; a finished turn ends with its ``Finished`` line."""
    markers = _MARKER.findall(stdout)
    session_id = markers[-1][1] if markers else None
    if limit := _REQUEST_LIMIT.search(stdout):
        return NativeResult(
            session_id, None, f"forge reached its request limit ({limit.group(1)} per turn)"
        )
    if markers and markers[-1][0] == "Finished":
        return NativeResult(session_id, None)
    return NativeResult(session_id, None, _stdout_error(stdout) or "forge did not finish the turn")


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    expected = requested_session_id(context, recorded) or next(
        (read_result(item.stdout).session_id for item in reversed(recorded)), None
    )
    issues: list[dict[str, Any]] = []
    dump = _latest_dump(context, expected, issues)
    servers = _mcp_servers(context)
    root = dump.get("conversation") if isinstance(dump, dict) else None
    related = {
        str(item.get("id")): item
        for item in (dump or {}).get("related_conversations") or ()
        if isinstance(item, dict)
    }
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=expected,
        agent=AtifAgent(
            name="forgecode", version=context.agent_version, model_name=context.model or None
        ),
    )
    failures = [read_result(item.stdout).error for item in recorded]
    children: list[AtifTrajectory] = []
    claimed: set[str] = set()
    turns = 0
    if isinstance(root, dict):
        turns = _convert(
            builder, root, context, servers, related, children, claimed, failures=failures
        )
        if turns < len(recorded):
            issues.append({"reason": "prompt_not_in_native_session", "invocation": turns + 1})
    # Without an export, each turn is its prompt and how its stdout says it ended.
    for item, failure in zip(recorded[turns:], failures[turns:], strict=True):
        builder.add(source="user", message=item.prompt)
        if failure:
            builder.add(source="system", message=failure)
    if not recorded and not builder.steps:
        builder.add(source="user", message=context.instruction)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    issues.extend(
        {"reason": "unlinked_subagent", "session_id": child_id}
        for child_id in sorted(set(related) - claimed)
    )
    trajectory = builder.build(subagent_trajectories=children or None)
    extra = _extra(context, issues, native=isinstance(root, dict))
    return trajectory.model_copy(update={"extra": extra}) if extra else trajectory


def _latest_dump(
    context: TrajectoryParseContext, session_id: str | None, issues: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The newest export of the conversation; each one holds the whole conversation."""
    base = context.logs_dir / DUMPS
    if not session_id or not (base / session_id).is_dir():
        issues.append({"reason": "native_session_unavailable", "session_id": session_id})
        return None
    for path in sorted((base / session_id).glob("*.json"), reverse=True):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            issues.append(
                {
                    "source": path.relative_to(context.logs_dir).as_posix(),
                    "reason": "malformed_json",
                    "detail": str(exc)[:500],
                }
            )
            continue
        if isinstance(value, dict) and isinstance(value.get("conversation"), dict):
            return value
    issues.append({"reason": "native_session_unavailable", "session_id": session_id})
    return None


def _mcp_servers(context: TrajectoryParseContext) -> dict[str, str]:
    """Forge's sanitized server name for each server as it was declared."""
    path = context.logs_dir / MCP_CONFIG
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    names = (value.get("mcpServers") or {}) if isinstance(value, dict) else {}
    return {_sanitized(str(name)): str(name) for name in names}


def _convert(
    builder: TrajectoryBuilder,
    conversation: dict[str, Any],
    context: TrajectoryParseContext,
    servers: dict[str, str],
    related: dict[str, dict[str, Any]],
    children: list[AtifTrajectory],
    claimed: set[str],
    *,
    failures: list[str | None],
) -> int:
    """Add a conversation's steps; the number of prompts it holds."""
    messages = (conversation.get("context") or {}).get("messages") or []
    results: dict[str, dict[str, Any]] = {}
    for message in messages:
        tool = message.get("tool") if isinstance(message, dict) else None
        if isinstance(tool, dict):
            results[str(tool.get("call_id") or "")] = tool
    prompts = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if isinstance(message.get("image"), dict):
            builder.add(source="system", message=_image(message["image"], context))
            continue
        text = message.get("text")
        if not isinstance(text, dict):
            continue
        role = text.get("role")
        if role == "User":
            raw = (text.get("raw_content") or {}).get("Text")
            if isinstance(raw, str):
                _failure(builder, failures, prompts)
                prompts += 1
                builder.add(source="user", message=raw)
            else:
                # Content forge adds under the user role, such as a reminder.
                builder.add(source="system", message=str(text.get("content") or ""))
        elif role == "Assistant":
            calls: list[AtifToolCall] = []
            observed: list[AtifObservationResult] = []
            for call in text.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("call_id") or "")
                name = str(call.get("name") or "")
                calls.append(
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name=name,
                        arguments=_arguments(call.get("arguments")),
                        extra=_mcp_extra(name, servers),
                    )
                )
                if call_id in results:
                    observed.append(
                        _result(
                            results[call_id],
                            builder.trajectory_id,
                            context,
                            servers,
                            related,
                            children,
                            claimed,
                        )
                    )
            reasoning = "".join(
                str(item.get("text") or "")
                for item in text.get("reasoning_details") or ()
                if isinstance(item, dict)
            )
            builder.add(
                source="agent",
                message=str(text.get("content") or ""),
                model_name=text.get("model") if isinstance(text.get("model"), str) else None,
                reasoning_content=reasoning or None,
                tool_calls=calls or None,
                observation=AtifObservation(results=observed) if observed else None,
                metrics=_metrics(message.get("usage")),
            )
    _failure(builder, failures, prompts)
    return prompts


def _failure(builder: TrajectoryBuilder, failures: list[str | None], turn: int) -> None:
    """How a turn ended when only its stdout says so, after the turn's own steps."""
    if 0 < turn <= len(failures) and (failure := failures[turn - 1]):
        builder.add(source="system", message=failure)


def _result(
    tool: dict[str, Any],
    spawner: str,
    context: TrajectoryParseContext,
    servers: dict[str, str],
    related: dict[str, dict[str, Any]],
    children: list[AtifTrajectory],
    claimed: set[str],
) -> AtifObservationResult:
    """A tool result; a ``task`` result names the child conversation it delegated to,
    which embeds in the ``spawner`` trajectory."""
    output = tool.get("output") if isinstance(tool.get("output"), dict) else {}
    parts: list[AtifContentPart] = []
    refs: list[AtifSubagentTrajectoryRef] = []
    for value in output.get("values") or ():
        if not isinstance(value, dict):
            continue
        if isinstance(value.get("text"), str):
            parts.append(AtifContentPart(type="text", text=value["text"]))
        elif isinstance(value.get("aI"), dict):
            delegated = value["aI"]
            parts.append(AtifContentPart(type="text", text=str(delegated.get("value") or "")))
            child_id = str(delegated.get("conversation_id") or "")
            if child_id in related and child_id not in claimed:
                claimed.add(child_id)
                child = _child(spawner, child_id, related, context, servers, claimed)
                children.append(child)
                refs.append(
                    AtifSubagentTrajectoryRef(
                        trajectory_id=child.trajectory_id, session_id=child_id
                    )
                )
        elif isinstance(value.get("image"), dict):
            image = _image(value["image"], context)
            parts.extend(image if isinstance(image, list) else [])
    content: str | list[AtifContentPart] = (
        "".join(part.text or "" for part in parts)
        if all(part.type == "text" for part in parts)
        else parts
    )
    return AtifObservationResult(
        source_call_id=str(tool.get("call_id") or ""),
        content=content,
        subagent_trajectory_ref=refs or None,
        extra={"ale": {"is_error": True}} if output.get("is_error") else None,
    )


def _child(
    spawner: str,
    child_id: str,
    related: dict[str, dict[str, Any]],
    context: TrajectoryParseContext,
    servers: dict[str, str],
    claimed: set[str],
) -> AtifTrajectory:
    """A delegated conversation as an embedded subagent trajectory; its own delegations
    embed in it."""
    builder = TrajectoryBuilder(
        trajectory_id=f"{spawner}-subagent-{child_id}",
        session_id=child_id,
        agent=AtifAgent(
            name="forgecode", version=context.agent_version, model_name=context.model or None
        ),
    )
    nested: list[AtifTrajectory] = []
    _convert(builder, related[child_id], context, servers, related, nested, claimed, failures=[])
    if not builder.steps:
        builder.add(source="user", message=str(related[child_id].get("title") or ""))
    return builder.build(subagent_trajectories=nested or None)


def _stdout_error(stdout: str) -> str | None:
    """The last ``ERROR:`` block forge printed, up to its next event line."""
    errors = []
    for match in _EVENT.finditer(stdout):
        rest = stdout[match.end() :]
        if not rest.startswith("ERROR: "):
            continue
        following = _EVENT.search(rest)
        errors.append(rest[7 : following.start() if following else None].strip())
    return errors[-1][:1600] if errors else None


def _image(image: dict[str, Any], context: TrajectoryParseContext) -> str | list[AtifContentPart]:
    match = _IMAGE_URL.fullmatch(str(image.get("url") or ""))
    if not match:
        return f"[image {image.get('mime_type') or ''} {image.get('url') or ''}]".strip()
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        return "[image with invalid base64]"
    blob = context.blobs.put(data, media_type=match.group(1))
    return [
        AtifContentPart(
            type="image",
            source=AtifImageSource(media_type=match.group(1), path=blob.path),  # type: ignore[arg-type]
        )
    ]


def _metrics(usage: Any) -> AtifMetrics | None:
    if not isinstance(usage, dict):
        return None

    def actual(key: str) -> int | None:
        value = usage.get(key)
        count = value.get("actual") if isinstance(value, dict) else None
        return count if isinstance(count, int) else None

    prompt, completion = actual("prompt_tokens"), actual("completion_tokens")
    if prompt is None and completion is None:
        return None
    return AtifMetrics(
        prompt_tokens=prompt, completion_tokens=completion, cached_tokens=actual("cached_tokens")
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


def _mcp_extra(name: str, servers: dict[str, str]) -> dict[str, Any] | None:
    """``mcp_<server>_tool_<tool>``, with both names as forge sanitized them."""
    if not name.startswith("mcp_"):
        return None
    for sanitized in sorted(servers, key=len, reverse=True):
        prefix = f"mcp_{sanitized}_tool_"
        if name.startswith(prefix) and len(name) > len(prefix):
            return {"ale": {"mcp": {"server": servers[sanitized], "tool": name[len(prefix) :]}}}
    server, separator, tool = name[4:].partition("_tool_")
    if not separator or not server or not tool:
        return None
    return {"ale": {"mcp": {"server": server, "tool": tool}}}


def _sanitized(name: str) -> str:
    """Forge's own ``ToolName::sanitized``."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]+", "_", name.lower())).strip("_")


def _extra(
    context: TrajectoryParseContext, issues: list[dict[str, Any]], *, native: bool
) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if not native:
        data["limitations"] = [
            "No conversation export is available; each turn keeps only its prompt and outcome."
        ]
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"ale": data} if data else None
