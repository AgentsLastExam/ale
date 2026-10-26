"""Pure conversion of Hermes Agent evidence to ATIF."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import yaml

from ale.core.harness import TrajectoryParseContext
from ale.core.trajectory import (
    AtifAgent,
    AtifFinalMetrics,
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

#: ``HERMES_HOME``.
STATE = ".hermes-ale"
#: Every session of the home, exported after each invocation:
#: ``<session id>/<time>.jsonl``, one session per line.
EXPORTS = f"{STATE}/exports"
#: The configuration, which names each MCP server as declared.
CONFIG = f"{STATE}/config.yaml"


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    return path.parent == PurePosixPath(EXPORTS, session_id) and path.suffix == ".jsonl"


def read_result(stdout: str) -> NativeResult:
    events, _ = json_lines(stdout)
    result = next((event for event in reversed(events) if event.get("type") == "result"), None)
    if result is None:
        return NativeResult(None, None, "hermes reported no result")
    session_id = result.get("session_id") if isinstance(result.get("session_id"), str) else None
    text = result.get("text") if isinstance(result.get("text"), str) else None
    return NativeResult(session_id or None, text or None, _failure(result))


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    streams, issues = stdout_events(recorded)
    expected = requested_session_id(context, recorded) or next(
        (read_result(item.stdout).session_id for item in reversed(recorded)), None
    )
    sessions = _Sessions(_latest_export(context, expected, issues), context, _mcp_servers(context))
    root = sessions.records.get(expected or "")
    if root is None:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=expected,
        agent=AtifAgent(
            name="hermes", version=context.agent_version, model_name=context.model or None
        ),
    )
    failures = [_failure(_result(stream)) for stream in streams]
    children: list[AtifTrajectory] = []
    turns = 0
    if expected is not None and root is not None:
        turns = sessions.convert(builder, expected, children, failures)
        if turns < len(recorded):
            issues.append({"reason": "prompt_not_in_native_session", "invocation": turns + 1})
    # A turn the export lacks is rebuilt from its own stream-json events.
    for item, stream, failure in zip(
        recorded[turns:], streams[turns:], failures[turns:], strict=True
    ):
        builder.add(source="user", message=item.prompt)
        _stream_steps(builder, stream, sessions.servers)
        if failure:
            builder.add(source="system", message=failure)
    if not recorded and not builder.steps:
        builder.add(source="user", message=context.instruction)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    if expected is not None and root is not None:
        issues.extend(sessions.unlinked(expected))
    tokens = _result(streams[-1]).get("tokens") if streams else None
    final_metrics = (
        AtifFinalMetrics(
            total_prompt_tokens=_count(tokens, "input"),
            total_completion_tokens=_count(tokens, "output"),
            total_cached_tokens=_count(tokens, "cache_read"),
            total_steps=len(builder.steps),
        )
        if isinstance(tokens, dict)
        else None
    )
    trajectory = builder.build(final_metrics=final_metrics, subagent_trajectories=children or None)
    extra = _extra(context, issues, native=root is not None)
    return trajectory.model_copy(update={"extra": extra}) if extra else trajectory


def _latest_export(
    context: TrajectoryParseContext, session_id: str | None, issues: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Each session of the newest export, by ID; each export holds every session."""
    base = context.logs_dir / EXPORTS / (session_id or "")
    paths = sorted(base.glob("*.jsonl"), reverse=True) if session_id and base.is_dir() else []
    for path in paths:
        records, problems = json_lines(path.read_text(encoding="utf-8", errors="replace"))
        source = path.relative_to(context.logs_dir).as_posix()
        issues.extend({"source": source, **problem} for problem in problems)
        sessions = {str(record.get("id")): record for record in records if record.get("id")}
        if session_id in sessions:
            return sessions
    return {}


def _mcp_servers(context: TrajectoryParseContext) -> dict[str, str]:
    """Hermes' sanitized name for each MCP server as it was declared."""
    path = context.logs_dir / CONFIG
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, yaml.YAMLError):
        return {}
    names = (value.get("mcp_servers") or {}) if isinstance(value, dict) else {}
    return {_sanitized(str(name)): str(name) for name in names} if isinstance(names, dict) else {}


@dataclass
class _Sessions:
    """Every session of one export and the delegated ones linked so far.

    Hermes records a session's origin on the session, never on the call that caused it:
    ``parent_session_id`` with a ``_delegate_from`` marker equal to it for a
    ``delegate_task`` child, and a parent that ended for ``compression`` for a
    continuation of the same conversation. Branches, resets and tool sessions are neither.
    """

    records: dict[str, dict[str, Any]]
    context: TrajectoryParseContext
    servers: dict[str, str]
    linked: set[str] = field(default_factory=set)

    def chain(self, session_id: str) -> list[dict[str, Any]]:
        """The conversation holding the session: it and its compression continuations,
        oldest first."""
        record = self.records[session_id]
        chain = [record]
        while (parent := self._continued(chain[0])) is not None and parent not in chain:
            chain.insert(0, parent)
        ordered = sorted(
            self.records.values(),
            key=lambda item: (_number(item.get("started_at")), str(item["id"])),
        )
        while follower := next(
            (
                candidate
                for candidate in ordered
                if candidate not in chain and self._continued(candidate) is chain[-1]
            ),
            None,
        ):
            chain.append(follower)
        return chain

    def convert(
        self,
        builder: TrajectoryBuilder,
        session_id: str,
        children: list[AtifTrajectory],
        failures: list[str | None],
    ) -> int:
        """Add one conversation's messages in order; the number of prompts among them."""
        chain = self.chain(session_id)
        ids = {str(record["id"]) for record in chain}
        self.linked |= ids
        delegated = [record for record in self.records.values() if self._delegator(record) in ids]
        messages = _shown(chain)
        results = {
            str(message.get("tool_call_id") or ""): message
            for message in messages
            if message.get("role") == "tool"
        }
        # A goal two calls share, or two children share, links neither.
        goals = Counter(
            goal
            for message in messages
            if message.get("role") == "assistant"
            for call in _calls(message)
            if call[1] == "delegate_task"
            for goal in _goals(call[2])
        )
        prompts = 0
        for message in messages:
            role = message.get("role")
            if message.get("_compressed_summary"):
                builder.add(source="system", message=_text(message.get("content")))
            elif role == "user":
                if prompts and 0 < prompts <= len(failures) and failures[prompts - 1]:
                    builder.add(source="system", message=str(failures[prompts - 1]))
                prompts += 1
                builder.add(source="user", message=_text(message.get("content")))
            elif role == "assistant":
                calls: list[AtifToolCall] = []
                observed: list[AtifObservationResult] = []
                for call_id, name, arguments in _calls(message):
                    calls.append(
                        AtifToolCall(
                            tool_call_id=call_id,
                            function_name=name,
                            arguments=arguments,
                            extra=_mcp_extra(name, self.servers),
                        )
                    )
                    if call_id not in results:
                        continue
                    refs = [
                        self._link(builder, child, children)
                        for goal in (_goals(arguments) if name == "delegate_task" else ())
                        if goals[goal] == 1
                        for child in _single(
                            [record for record in delegated if _first_prompt(record) == goal]
                        )
                        if str(child["id"]) not in self.linked
                    ]
                    observed.append(
                        AtifObservationResult(
                            source_call_id=call_id,
                            content=_text(results[call_id].get("content")),
                            subagent_trajectory_ref=refs or None,
                        )
                    )
                reasoning = message.get("reasoning_content") or message.get("reasoning")
                builder.add(
                    source="agent",
                    message=_text(message.get("content")),
                    reasoning_content=reasoning or None if isinstance(reasoning, str) else None,
                    tool_calls=calls or None,
                    observation=AtifObservation(results=observed) if observed else None,
                )
        if 0 < prompts <= len(failures) and failures[prompts - 1]:
            builder.add(source="system", message=str(failures[prompts - 1]))
        return prompts

    def unlinked(self, session_id: str) -> list[dict[str, Any]]:
        """One issue for each session delegated under the conversation, at any depth,
        that no call links."""
        found: set[str] = set()
        pending = [session_id]
        while pending:
            ids = {str(record["id"]) for record in self.chain(pending.pop())}
            for record in self.records.values():
                child = str(record["id"])
                if self._delegator(record) in ids and child not in found:
                    found.add(child)
                    pending.append(child)
        return [
            {"reason": "unlinked_subagent", "session_id": child}
            for child in sorted(found - self.linked)
        ]

    def _link(
        self, spawner: TrajectoryBuilder, record: dict[str, Any], children: list[AtifTrajectory]
    ) -> AtifSubagentTrajectoryRef:
        """Embed one delegated conversation in its spawner's trajectory."""
        child_id = str(record["id"])
        builder = TrajectoryBuilder(
            trajectory_id=f"{spawner.trajectory_id}-subagent-{child_id}",
            session_id=child_id,
            agent=AtifAgent(
                name="hermes",
                version=self.context.agent_version,
                model_name=self.context.model or None,
            ),
        )
        nested: list[AtifTrajectory] = []
        self.convert(builder, child_id, nested, [])
        children.append(builder.build(subagent_trajectories=nested or None))
        return AtifSubagentTrajectoryRef(trajectory_id=builder.trajectory_id, session_id=child_id)

    def _delegator(self, record: dict[str, Any]) -> str | None:
        parent = record.get("parent_session_id")
        if not parent or _model_config(record).get("_delegate_from") != parent:
            return None
        return str(parent)

    def _continued(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """The session this one continues after a compression, if it is one."""
        parent = self.records.get(str(record.get("parent_session_id") or ""))
        if parent is None or parent.get("end_reason") != "compression":
            return None
        if record.get("source") == "tool":
            return None
        markers = _model_config(record)
        forks = (markers.get(key) for key in ("_branched_from", "_delegate_from", "_reset_from"))
        return None if parent["id"] in forks else parent


def _shown(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A conversation's messages as Hermes shows its transcript, in stored order.

    A rewind or a newer copy removes a row (``active`` and ``compacted`` both unset). A
    compaction keeps what it summarized as archived rows and stores the summary and each
    message it carries forward again; a carried copy is shown once, where it first
    appears. A summary folded into a carried message keeps that message's ``message_uid``,
    so the message follows its summary when no other copy is shown.
    """
    shown: list[dict[str, Any]] = []
    keys: set[str] = set()
    first: dict[str, dict[str, Any]] = {}
    for record in chain:
        for message in record.get("messages") or ():
            if not isinstance(message, dict):
                continue
            uid = message.get("message_uid")
            original = first.get(uid) if isinstance(uid, str) and uid else None
            if isinstance(uid, str) and uid:
                first.setdefault(uid, message)
            if not (message.get("active", 1) or message.get("compacted")):
                continue
            key = _key(message)
            if key in keys:
                continue
            keys.add(key)
            shown.append(message)
            carried = original if message.get("_compressed_summary") else None
            if carried is not None and _key(carried) not in keys:
                keys.add(_key(carried))
                shown.append(carried)
    return shown


def _key(message: dict[str, Any]) -> str:
    """Hermes' display identity of a message, equal for each copy of it. A tool-calling
    assistant message is identified by its call IDs, since a copy's arguments may be
    pruned."""
    content = message.get("content")
    calls = message.get("tool_calls")
    ids = [call[0] for call in _calls(message)]
    if message.get("role") == "assistant" and ids and all(ids):
        content, calls = None, ids
    return json.dumps(
        [
            message.get("role"),
            content,
            message.get("timestamp"),
            message.get("tool_call_id"),
            calls,
            message.get("tool_name"),
        ],
        sort_keys=True,
        default=str,
    )


def _calls(message: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    calls = []
    for call in message.get("tool_calls") or ():
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        calls.append(
            (
                str(call.get("id") or call.get("call_id") or ""),
                str(function.get("name") or ""),
                _arguments(function.get("arguments")),
            )
        )
    return calls


def _goals(arguments: dict[str, Any]) -> list[str]:
    """The goal of each task a ``delegate_task`` call spawns, as Hermes reads them: the
    ``tasks`` list, which a model may send as a JSON string, else the single ``goal``."""
    tasks = arguments.get("tasks")
    if isinstance(tasks, str):
        try:
            tasks = json.loads(tasks)
        except json.JSONDecodeError:
            return []
    if isinstance(tasks, list) and tasks:
        return [
            task["goal"]
            for task in tasks
            if isinstance(task, dict) and isinstance(task.get("goal"), str)
        ]
    goal = arguments.get("goal")
    return [goal] if isinstance(goal, str) and goal.strip() else []


def _single(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return matches if len(matches) == 1 else []


def _first_prompt(session: dict[str, Any]) -> str | None:
    first = next(
        (
            message
            for message in session.get("messages") or ()
            if isinstance(message, dict)
            and message.get("role") == "user"
            and (message.get("active", 1) or message.get("compacted"))
        ),
        None,
    )
    return _text(first.get("content")) if first else None


def _model_config(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("model_config")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _stream_steps(
    builder: TrajectoryBuilder, stream: list[dict[str, Any]], servers: dict[str, str]
) -> None:
    """One invocation's stream-json: each tool use with its result, then its text."""
    results = {
        str(event.get("tool_call_id") or ""): event
        for event in stream
        if event.get("type") == "tool_result"
    }
    text: list[str] = []
    for event in stream:
        if event.get("type") == "text":
            text.append(str(event.get("text") or ""))
        elif event.get("type") == "tool_use":
            if "".join(text).strip():
                builder.add(source="agent", message="".join(text).strip())
            text.clear()
            call_id = str(event.get("tool_call_id") or "")
            name = str(event.get("name") or "")
            result = results.get(call_id)
            builder.add(
                source="agent",
                message="",
                tool_calls=[
                    AtifToolCall(
                        tool_call_id=call_id,
                        function_name=name,
                        arguments=_arguments(event.get("input")),
                        extra=_mcp_extra(name, servers),
                    )
                ],
                observation=(
                    AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id=call_id,
                                content=_text(result.get("output")),
                                extra={"ale": {"is_error": True}}
                                if result.get("is_error")
                                else None,
                            )
                        ]
                    )
                    if result is not None
                    else None
                ),
            )
    if "".join(text).strip():
        builder.add(source="agent", message="".join(text).strip())


def _result(stream: list[dict[str, Any]]) -> dict[str, Any]:
    return next((event for event in reversed(stream) if event.get("type") == "result"), {})


def _failure(result: dict[str, Any]) -> str | None:
    if not result:
        return None
    error = result.get("error")
    if isinstance(error, str) and error:
        return error
    code = result.get("exit_code")
    if isinstance(code, int) and code != 0:
        return f"hermes ended the turn with exit code {code}: {str(result.get('text') or '')[:600]}"
    return None


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(block.get("text") or "")
            for block in value
            if isinstance(block, dict)
            and block.get("type") in {"text", "input_text", "output_text"}
        )
    return "" if value is None else json.dumps(value, ensure_ascii=False)


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
    """``mcp__<server>__<tool>``, each part with Hermes' replacement of other characters."""
    if not name.startswith("mcp__"):
        return None
    server, separator, tool = name[5:].partition("__")
    if not separator or not server or not tool:
        return None
    return {"ale": {"mcp": {"server": servers.get(server, server), "tool": tool}}}


def _sanitized(name: str) -> str:
    """Hermes' ``sanitize_mcp_name_component``."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _count(tokens: Any, key: str) -> int | None:
    value = tokens.get(key) if isinstance(tokens, dict) else None
    return value if isinstance(value, int) else None


def _extra(
    context: TrajectoryParseContext, issues: list[dict[str, Any]], *, native: bool
) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if not native:
        data["limitations"] = [
            "Only the CLI's stream-json events are available; the session export is absent."
        ]
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"ale": data} if data else None
