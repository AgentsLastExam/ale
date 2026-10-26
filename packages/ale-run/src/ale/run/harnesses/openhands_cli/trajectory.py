"""Pure conversion of OpenHands CLI evidence to ATIF."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path, PurePosixPath
from typing import Any

from ale.core.harness import TrajectoryParseContext
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
from ale.run.harnesses.base import (
    NativeResult,
    invocations,
    json_lines,
    requested_session_id,
    stdout_events,
)

#: ``OPENHANDS_PERSISTENCE_DIR``.
STATE = ".openhands-ale"
#: ``<conversation id>/events/event-<n>-<uuid>.json``, each written once, plus
#: ``base_state.json`` and delegated conversations below ``subagents/<id>/``.
CONVERSATIONS = f"{STATE}/conversations"
#: The MCP configuration, which names the servers the tools came from.
MCP_CONFIG = f"{STATE}/mcp.json"
_CONVERSATION_ID = re.compile(r"^Conversation ID: ([0-9a-f]{32})\s*$", re.MULTILINE)
#: How the CLI frames the contents of `--file` as the first user message.
_FILE_PROMPT = re.compile(
    r"\AStarting this session with file context\.\n\nFile path: [^\n]*\n\n"
    r"File contents:\n-{20}\n(.*)\n-{20}\n?\Z",
    re.DOTALL,
)
_DATA_URL = re.compile(r"data:(image/[a-z0-9.+-]+);base64,(.*)", re.DOTALL)


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    root = PurePosixPath(CONVERSATIONS, session_id)
    return path == root / "base_state.json" or (
        path.parent == root / "events" and path.suffix == ".json"
    )


def read_result(stdout: str) -> NativeResult:
    match = _CONVERSATION_ID.findall(stdout)
    events, _ = json_lines(_json_only(stdout))
    final = next(
        (
            _text((event.get("llm_message") or {}).get("content"))
            for event in reversed(events)
            if event.get("kind") == "MessageEvent" and event.get("source") == "agent"
        ),
        None,
    )
    return NativeResult(match[-1] if match else None, final or None, _failure(events))


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    streams, issues = stdout_events(
        [replace(item, stdout=_json_only(item.stdout)) for item in recorded]
    )
    expected = requested_session_id(context, recorded) or next(
        (read_result(item.stdout).session_id for item in reversed(recorded)), None
    )
    root = context.logs_dir / CONVERSATIONS / (expected or "")
    events = _conversation(context, root, issues) if expected and root.is_dir() else []
    if not events:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
    other = sorted(
        path.relative_to(context.logs_dir).as_posix()
        for path in (context.logs_dir / CONVERSATIONS).glob("*")
        if path.is_dir() and path.name != expected
    )
    if other:
        issues.append(
            {"reason": "unconverted_native_files", "count": len(other), "sources": other[:10]}
        )
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=expected,
        agent=AtifAgent(
            name="openhands-cli", version=context.agent_version, model_name=context.model or None
        ),
    )
    tree = _Tree(context, root, context.trajectory_id, _mcp_servers(context), issues)
    turns = _convert(builder, events, context, tree, recorded_prompts=True)
    if events and turns < len(recorded):
        issues.append({"reason": "prompt_not_in_native_session", "invocation": turns + 1})
    # A turn the conversation lacks is rebuilt from its own JSON events.
    for item, stream in zip(recorded[turns:], streams[turns:], strict=True):
        if not any(e.get("kind") == "MessageEvent" and e.get("source") == "user" for e in stream):
            builder.add(source="user", message=item.prompt)
        _convert(builder, stream, context, None, recorded_prompts=True)
    if not recorded and not builder.steps:
        builder.add(source="user", message=context.instruction)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    if events:
        tree.report_unlinked()
    children = tree.children
    trajectory = builder.build(subagent_trajectories=children or None)
    extra = _extra(context, issues, native=bool(events))
    return trajectory.model_copy(update={"extra": extra}) if extra else trajectory


def _json_only(stdout: str) -> str:
    """The CLI's JSON events, without the progress text printed among them."""
    return "".join(line for line in stdout.splitlines(keepends=True) if line.startswith("{"))


def _conversation(
    context: TrajectoryParseContext, root: Path, issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """A conversation's events in native order, by the index in each file name."""
    events: list[dict[str, Any]] = []
    for path in sorted((root / "events").glob("event-*.json")):
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
        if isinstance(value, dict):
            events.append(value)
    return events


@dataclass
class _Tree:
    """One conversation directory, and the delegated conversations its tasks link to.

    A task's ``ObservationEvent`` names its ``task_id``. When the task ends, the SDK
    replaces ``usage_to_metrics["task:<task_id>"]`` in the parent's ``base_state.json``
    with the child conversation's combined metrics, whose ``response_id`` values are the
    ``llm_response_id`` of the child's own events. A child below ``subagents/`` is linked
    to the call when exactly one has its own response IDs among that task's and an event
    between the call's action and observation: task IDs restart in every process, so a
    later task's metrics can carry an earlier task's ID.
    """

    context: TrajectoryParseContext
    root: Path
    trajectory_id: str
    servers: list[str]
    issues: list[dict[str, Any]]
    children: list[AtifTrajectory] = field(default_factory=list)
    _events: dict[str, list[dict[str, Any]]] = field(default_factory=dict, repr=False)

    @cached_property
    def subagents(self) -> dict[str, Path]:
        base = self.root / "subagents"
        return (
            {path.name: path for path in sorted(base.iterdir()) if path.is_dir()}
            if base.is_dir()
            else {}
        )

    @cached_property
    def tasks(self) -> dict[str, set[str]]:
        """The response IDs the parent recorded for each task, by task ID."""
        path = self.root / "base_state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            return {}
        usage = (
            ((state.get("stats") or {}).get("usage_to_metrics") or {})
            if isinstance(state, dict)
            else {}
        )
        found: dict[str, set[str]] = {}
        for key, metrics in usage.items() if isinstance(usage, dict) else ():
            if not str(key).startswith("task:") or not isinstance(metrics, dict):
                continue
            found[str(key)[5:]] = {
                str(entry["response_id"])
                for name in ("token_usages", "response_latencies")
                for entry in metrics.get(name) or ()
                if isinstance(entry, dict) and entry.get("response_id")
            }
        return found

    def events(self, name: str) -> list[dict[str, Any]]:
        return self._events.setdefault(
            name, _conversation(self.context, self.subagents[name], self.issues)
        )

    def link(self, action: dict[str, Any], observation: dict[str, Any]) -> AtifTrajectory | None:
        """The delegated conversation the evidence identifies for this task, embedded here."""
        task = (observation.get("observation") or {}).get("task_id")
        expected = self.tasks.get(str(task)) if task else None
        start, end = action.get("timestamp"), observation.get("timestamp")
        if not expected or not isinstance(start, str) or not isinstance(end, str):
            return None
        matches = []
        for name in self.subagents:
            events = self.events(name)
            own = {str(e["llm_response_id"]) for e in events if e.get("llm_response_id")}
            during = any(
                isinstance(e.get("timestamp"), str) and start <= e["timestamp"] <= end
                for e in events
            )
            if own and own <= expected and during:
                matches.append(name)
        if len(matches) != 1:
            return None
        name = matches[0]
        linked = next((child for child in self.children if child.session_id == name), None)
        if linked is not None:
            # A resumed task continues the same conversation.
            return linked
        tree = _Tree(
            self.context,
            self.subagents[name],
            f"{self.trajectory_id}-subagent-{name}",
            self.servers,
            self.issues,
        )
        builder = TrajectoryBuilder(
            trajectory_id=tree.trajectory_id,
            session_id=name,
            agent=AtifAgent(
                name="openhands-cli",
                version=self.context.agent_version,
                model_name=self.context.model or None,
            ),
        )
        _convert(builder, self.events(name), self.context, tree, recorded_prompts=False)
        tree.report_unlinked()
        child = builder.build(subagent_trajectories=tree.children or None)
        self.children.append(child)
        return child

    def report_unlinked(self) -> None:
        """One issue on the root per delegated conversation no call identified."""
        linked = {child.session_id for child in self.children}
        self.issues.extend(
            {
                "reason": "unlinked_subagent",
                "source": path.relative_to(self.context.logs_dir).as_posix(),
            }
            for name, path in self.subagents.items()
            if name not in linked
        )


def _convert(
    builder: TrajectoryBuilder,
    events: list[dict[str, Any]],
    context: TrajectoryParseContext,
    tree: _Tree | None,
    *,
    recorded_prompts: bool,
) -> int:
    """Add the events' steps; the number of user messages among them. ``tree`` links
    delegated conversations; stdout events have none."""
    servers = tree.servers if tree else []
    results = {
        str(event.get("tool_call_id") or ""): event
        for event in events
        if event.get("kind") in {"ObservationEvent", "AgentErrorEvent", "UserRejectObservation"}
    }
    prompts = 0
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if not pending:
            return
        calls: list[AtifToolCall] = []
        observed: list[AtifObservationResult] = []
        reasoning: list[str] = []
        for action in pending:
            call = action.get("tool_call") if isinstance(action.get("tool_call"), dict) else {}
            call_id = str(action.get("tool_call_id") or call.get("id") or "")
            name = str(action.get("tool_name") or call.get("name") or "")
            arguments = _arguments(call.get("arguments"))
            if not arguments and isinstance(action.get("action"), dict):
                arguments = {k: v for k, v in action["action"].items() if k != "kind"}
            calls.append(
                AtifToolCall(
                    tool_call_id=call_id,
                    function_name=name,
                    arguments=arguments,
                    extra=_mcp_extra(action, servers),
                )
            )
            reasoning.extend(_thought(action))
            if call_id in results:
                observed.append(_result(results[call_id], action, context, tree))
        builder.add(
            source="agent",
            message="",
            reasoning_content="\n".join(reasoning) or None,
            tool_calls=calls,
            observation=AtifObservation(results=observed) if observed else None,
        )
        pending.clear()

    for event in events:
        kind = event.get("kind")
        if kind == "ActionEvent":
            if pending and event.get("llm_response_id") != pending[0].get("llm_response_id"):
                flush()
            pending.append(event)
            continue
        flush()
        timestamp = event.get("timestamp") if isinstance(event.get("timestamp"), str) else None
        if kind == "MessageEvent":
            content = (event.get("llm_message") or {}).get("content")
            if event.get("source") == "user":
                prompts += 1
                message = _content(content, context)
                if recorded_prompts and isinstance(message, str):
                    framed = _FILE_PROMPT.match(message)
                    message = framed.group(1) if framed else message
                builder.add(source="user", message=message, timestamp=timestamp)
            else:
                message = (event.get("llm_message") or {}).get("reasoning_content")
                builder.add(
                    source="agent",
                    message=_content(content, context),
                    timestamp=timestamp,
                    reasoning_content=message if isinstance(message, str) and message else None,
                )
        elif kind == "ConversationErrorEvent":
            builder.add(
                source="system",
                message=f"{event.get('code') or 'error'}: {event.get('detail') or ''}".strip(),
                timestamp=timestamp,
            )
        elif kind == "CondensationSummaryEvent" and isinstance(event.get("summary"), str):
            builder.add(source="system", message=event["summary"], timestamp=timestamp)
    flush()
    return prompts


def _result(
    event: dict[str, Any],
    action: dict[str, Any],
    context: TrajectoryParseContext,
    tree: _Tree | None,
) -> AtifObservationResult:
    call_id = str(event.get("tool_call_id") or "")
    if event.get("kind") == "AgentErrorEvent":
        return AtifObservationResult(
            source_call_id=call_id,
            content=str(event.get("error") or ""),
            extra={"ale": {"is_error": True}},
        )
    if event.get("kind") == "UserRejectObservation":
        return AtifObservationResult(
            source_call_id=call_id,
            content=str(event.get("rejection_reason") or "rejected"),
            extra={"ale": {"is_error": True}},
        )
    observation = event.get("observation") if isinstance(event.get("observation"), dict) else {}
    refs = None
    if tree is not None and observation.get("kind") == "TaskObservation":
        child = tree.link(action, event)
        if child is not None:
            refs = [
                AtifSubagentTrajectoryRef(
                    trajectory_id=child.trajectory_id, session_id=child.session_id
                )
            ]
    return AtifObservationResult(
        source_call_id=call_id,
        content=_content(observation.get("content"), context),
        subagent_trajectory_ref=refs,
        extra={"ale": {"is_error": True}} if observation.get("is_error") else None,
    )


def _failure(events: list[dict[str, Any]]) -> str | None:
    """A conversation error ends the run; tool errors are observations the agent sees."""
    errors = [event for event in events if event.get("kind") == "ConversationErrorEvent"]
    if not errors:
        return None
    return f"{errors[-1].get('code') or 'error'}: {errors[-1].get('detail') or ''}".strip()


def _thought(action: dict[str, Any]) -> list[str]:
    parts = [
        str(item.get("text") or "")
        for item in action.get("thought") or ()
        if isinstance(item, dict) and item.get("text")
    ]
    if isinstance(action.get("reasoning_content"), str) and action["reasoning_content"]:
        parts.append(action["reasoning_content"])
    return parts


def _mcp_servers(context: TrajectoryParseContext) -> list[str]:
    path = context.logs_dir / MCP_CONFIG
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return []
    return sorted((value.get("mcpServers") or {}) if isinstance(value, dict) else {})


def _mcp_extra(action: dict[str, Any], servers: list[str]) -> dict[str, Any] | None:
    """The server and tool of an MCP call; with several servers, tools are ``<server>_<tool>``."""
    inner = action.get("action") if isinstance(action.get("action"), dict) else {}
    if inner.get("kind") != "MCPToolAction":
        return None
    name = str(action.get("tool_name") or "")
    if len(servers) == 1:
        return {"ale": {"mcp": {"server": servers[0], "tool": name}}}
    for server in sorted(servers, key=len, reverse=True):
        if name.startswith(f"{server}_") and len(name) > len(server) + 1:
            return {"ale": {"mcp": {"server": server, "tool": name[len(server) + 1 :]}}}
    return None


def _content(value: Any, context: TrajectoryParseContext) -> str | list[AtifContentPart]:
    if isinstance(value, str):
        return value
    parts: list[AtifContentPart] = []
    for block in value if isinstance(value, list) else ():
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(AtifContentPart(type="text", text=str(block.get("text") or "")))
        elif block.get("type") == "image":
            for url in block.get("image_urls") or ():
                match = _DATA_URL.fullmatch(str(url))
                if not match:
                    parts.append(AtifContentPart(type="text", text=f"[image {url}]"))
                    continue
                try:
                    data = base64.b64decode(match.group(2), validate=True)
                except (binascii.Error, ValueError):
                    parts.append(AtifContentPart(type="text", text="[image with invalid base64]"))
                    continue
                blob = context.blobs.put(data, media_type=match.group(1))
                parts.append(
                    AtifContentPart(
                        type="image",
                        source=AtifImageSource(media_type=match.group(1), path=blob.path),  # type: ignore[arg-type]
                    )
                )
    if all(part.type == "text" for part in parts):
        return "".join(part.text or "" for part in parts)
    return parts


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return "".join(
        str(block.get("text") or "")
        for block in (value if isinstance(value, list) else ())
        if isinstance(block, dict) and block.get("type") == "text"
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
    return {}


def _extra(
    context: TrajectoryParseContext, issues: list[dict[str, Any]], *, native: bool
) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if not native:
        data["limitations"] = [
            "Only the CLI's JSON events are available; the native conversation is absent."
        ]
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"ale": data} if data else None
