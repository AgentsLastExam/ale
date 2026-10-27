"""Pure conversion of Codex CLI evidence to ATIF."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from ale.core.harness import TrajectoryParseContext
from ale.core.trajectory import (
    AtifAgent,
    AtifContentPart,
    AtifFinalMetrics,
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
    NativeResult,
    invocations,
    mirrored_files,
    native_events,
    requested_session_id,
    stdout_events,
    unconverted,
)

SESSIONS = ".codex-ale/sessions"
#: Where `CODEX_ROLLOUT_TRACE_ROOT` points: one bundle per `codex exec` process.
TRACES = ".codex-ale/rollout-traces"
_CALL_TYPES = frozenset(
    ("function_call", "custom_tool_call", "web_search_call", "tool_search_call")
)
_OUTPUT_TYPES = frozenset(("function_call_output", "custom_tool_call_output", "tool_search_output"))
#: The only payload paths a trace event may name; any other would read outside its bundle.
_TRACE_PAYLOAD = re.compile(r"payloads/[0-9]+\.json")
#: Every other mirrored rollout, with the issues reading it raised.
_Rollouts = dict[Path, tuple[list[dict[str, Any]], list[dict[str, Any]]]]
#: How a code-mode script calls ``spawn_agent``.
_SPAWN_TOOL = "multi_agent_v1__spawn_agent"
_DATA_URL = re.compile(r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=]+)")
#: A thread's script calls as steps' calls and results, keyed by the model call each followed.
_ScriptSteps = dict[str, list[tuple[AtifToolCall, AtifObservation | None]]]


@dataclass
class _TracedCall:
    """One call a code-mode script made, as a rollout trace bundle records it."""

    thread: str
    #: The model call it followed, which places it in time.
    after: str
    #: The ``exec`` call whose script made it.
    caller: str
    started: dict[str, Any]
    runtime: dict[str, Any] = field(default_factory=dict)
    ended: dict[str, Any] = field(default_factory=dict)


def is_session_file(path: PurePosixPath, session_id: str) -> bool:
    # Rollouts are named `rollout-<timestamp>-<thread id>.jsonl`.
    return path.is_relative_to(SESSIONS) and path.name.endswith(f"-{session_id}.jsonl")


def read_result(stdout: str) -> NativeResult:
    return NativeResult(_thread_id(stdout), _final_message(stdout), _native_error(stdout))


def parse_trajectory(context: TrajectoryParseContext) -> AtifTrajectory:
    recorded = invocations(context)
    turns, issues = stdout_events(recorded)
    stream = [event for turn in turns for event in turn]
    expected = requested_session_id(context, recorded) or _thread_id_from_events(stream)
    files = mirrored_files(context, SESSIONS)
    matches = [
        path
        for path in files
        if expected
        and is_session_file(PurePosixPath(path.relative_to(context.logs_dir).as_posix()), expected)
    ]
    rollout: list[dict[str, Any]] = []
    consumed: set[Path] = set()
    if len(matches) == 1:
        rollout, native_issues = native_events(context, matches[0])
        issues.extend(native_issues)
        observed = _session_id_from_events(rollout)
        if observed and observed != expected:
            issues.append(
                {"reason": "session_id_mismatch", "expected": expected, "actual": observed}
            )
            rollout = []
        else:
            consumed.add(matches[0])
    else:
        issues.append({"reason": "native_session_unavailable", "session_id": expected})
    # Without the root rollout no spawn can be found, so every child stays raw evidence.
    threads = {
        path: native_events(context, path) for path in files if rollout and path not in consumed
    }
    traces = mirrored_files(context, TRACES)
    scripted = _script_calls(context, expected, traces, issues, consumed) if rollout else {}
    subagents, spawned = _subagents(context, expected, rollout, threads, scripted, issues, consumed)
    issues.extend(unconverted(context, (set(files) | set(traces)) - consumed))
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id, session_id=expected, agent=_agent(context)
    )
    # The rollout is the complete ordered conversation. Its user messages include context
    # Codex injects, so a turn starts only at the exact prompt ALE sent. Turns the rollout
    # does not contain fall back to that invocation's stdout events.
    prompts = [item.prompt.strip() for item in recorded]
    native_turns = 0
    transcript_mcp = [
        event.get("item") or {}
        for event in stream
        if event.get("type") == "item.completed"
        and (event.get("item") or {}).get("type") == "mcp_tool_call"
    ]
    outputs = _outputs(rollout)
    scripts = _scripts(scripted, expected, rollout, issues)
    used_mcp: set[int] = set()
    for event in rollout:
        payload = event.get("payload") or {}
        if event.get("type") != "response_item":
            continue
        kind = payload.get("type")
        if kind == "message" and payload.get("role") == "user":
            if native_turns < len(prompts) and _text(payload) == prompts[native_turns]:
                if native_turns:
                    _errors(builder, turns[native_turns - 1])
                builder.add(source="user", message=recorded[native_turns].prompt)
                native_turns += 1
            continue
        if kind not in _CALL_TYPES and kind != "reasoning" and payload.get("role") != "assistant":
            continue
        if not native_turns:
            # The conversation began without the prompt ALE sent; keep it and say so.
            issues.append({"reason": "prompt_not_in_native_session", "invocation": 1})
            builder.add(source="user", message=recorded[0].prompt)
            native_turns = 1
        _agent_item(builder, payload, outputs, spawned, scripts, transcript_mcp, used_mcp, context)
    _unplaced(scripts, expected, issues)
    if native_turns:
        _errors(builder, turns[native_turns - 1])
    if rollout and native_turns < len(recorded):
        issues.append({"reason": "prompt_not_in_native_session", "invocation": native_turns + 1})
    for item, turn in zip(recorded[native_turns:], turns[native_turns:], strict=True):
        builder.add(source="user", message=item.prompt)
        _stream_steps(builder, turn, context)
    if not any(step.source == "agent" for step in builder.steps) and context.final_message:
        builder.add(source="agent", message=context.final_message)
    usage: dict[str, int] = {}
    for event in stream:
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = {key: value for key, value in event["usage"].items() if isinstance(value, int)}
    cached = usage.get("cached_input_tokens") or usage.get("input_tokens_details_cached_tokens")
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
    trajectory = builder.build(final_metrics=final_metrics, subagent_trajectories=subagents or None)
    extra = _extra(context, issues)
    return trajectory.model_copy(update={"extra": extra}) if extra else trajectory


def _subagents(
    context: TrajectoryParseContext,
    thread_id: str | None,
    rollout: list[dict[str, Any]],
    threads: _Rollouts,
    scripted: dict[str, _ScriptSteps],
    issues: list[dict[str, Any]],
    consumed: set[Path],
) -> tuple[list[AtifTrajectory], dict[str, list[AtifSubagentTrajectoryRef]]]:
    """The threads ``thread_id`` spawned, converted, with references keyed by spawning call.

    A child names its parent in its first record, and the call that spawned it names the
    child in its own result: ``spawn_agent``'s, or what a code-mode script printed of it.
    A child that no spawn result names stays raw evidence with a parse issue.
    """
    children = {
        path: events
        for path, (events, _) in threads.items()
        if path not in consumed
        and (meta := _session_meta(events)).get("parent_thread_id") == thread_id
        and meta.get("id") != thread_id
    }
    embedded: list[AtifTrajectory] = []
    refs: dict[str, list[AtifSubagentTrajectoryRef]] = {}
    for call_id, result in _spawn_results(rollout):
        for path, events in list(children.items()):
            child_id = str(_session_meta(events).get("id") or "")
            if not child_id or child_id not in result:
                continue
            if any(item.session_id == child_id for item in embedded):
                # A second rollout claiming the same thread stays raw evidence.
                continue
            del children[path]
            consumed.add(path)
            child = _thread_trajectory(
                replace(
                    context,
                    trajectory_id=f"{context.trajectory_id}-subagent-{child_id}",
                    session_id=child_id,
                ),
                events,
                threads,
                scripted,
                issues,
                consumed,
            )
            if child is None:
                # Spawned, but no turn of its own was recorded.
                consumed.discard(path)
                continue
            issues.extend(threads[path][1])
            embedded.append(child)
            refs.setdefault(call_id, []).append(
                AtifSubagentTrajectoryRef(trajectory_id=child.trajectory_id, session_id=child_id)
            )
    issues.extend(
        {"reason": "unlinked_subagent", "source": path.relative_to(context.logs_dir).as_posix()}
        for path in sorted(children)
    )
    return embedded, refs


def _thread_trajectory(
    context: TrajectoryParseContext,
    rollout: list[dict[str, Any]],
    threads: _Rollouts,
    scripted: dict[str, _ScriptSteps],
    issues: list[dict[str, Any]],
    consumed: set[Path],
) -> AtifTrajectory | None:
    """A spawned thread's rollout, which no ALE invocation or stdout describes.

    Each input its parent sent is recorded as a ``user_message`` event; the context
    Codex injects into the conversation is not, so it stays out as it does for the root.
    """
    subagents, spawned = _subagents(
        context, context.session_id, rollout, threads, scripted, issues, consumed
    )
    builder = TrajectoryBuilder(
        trajectory_id=context.trajectory_id,
        session_id=context.session_id,
        agent=_agent(context),
        extra=_extra(context, []),
    )
    outputs = _outputs(rollout)
    scripts = _scripts(scripted, context.session_id, rollout, issues)
    for event in rollout:
        payload = event.get("payload") or {}
        if event.get("type") == "event_msg" and payload.get("type") == "user_message":
            builder.add(source="user", message=str(payload.get("message") or ""))
        elif event.get("type") == "response_item":
            # Only stdout carries the real server name of a model's MCP call, and only the
            # root's.
            _agent_item(builder, payload, outputs, spawned, scripts, [], set(), context)
    _unplaced(scripts, context.session_id, issues)
    if not builder.steps:
        return None
    return builder.build(subagent_trajectories=subagents or None)


def _spawn_results(rollout: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Each call that can spawn a thread, in order, with its result as text."""
    outputs = _outputs(rollout)
    found = []
    for event in rollout:
        payload = event.get("payload") or {}
        if event.get("type") != "response_item":
            continue
        if payload.get("type") == "function_call":
            spawns = (payload.get("namespace"), payload.get("name")) == (
                "multi_agent_v1",
                "spawn_agent",
            )
        else:
            spawns = payload.get("type") == "custom_tool_call" and _SPAWN_TOOL in str(
                payload.get("input") or ""
            )
        call_id = str(payload.get("call_id") or "")
        if spawns and call_id in outputs:
            # A thread ID needs no JSON escaping, so it appears verbatim in the dump.
            result = json.dumps(outputs[call_id].get("output"), ensure_ascii=False, default=str)
            found.append((call_id, result))
    return found


def _agent_item(
    builder: TrajectoryBuilder,
    payload: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    spawned: dict[str, list[AtifSubagentTrajectoryRef]],
    scripts: _ScriptSteps,
    transcript_mcp: list[dict[str, Any]],
    used_mcp: set[int],
    context: TrajectoryParseContext,
) -> None:
    """An agent message, reasoning summary, or call from a rollout, as one step.

    A call is followed by the calls code-mode scripts made before the model's next call.
    """
    kind = payload.get("type")
    if kind == "message":
        if payload.get("role") == "assistant" and (text := _text(payload)):
            builder.add(source="agent", message=text)
    elif kind == "reasoning":
        if summary := _summary(payload):
            builder.add(source="agent", message="", reasoning_content=summary)
    elif kind in _CALL_TYPES:
        call, observation = _native_call(payload, outputs, transcript_mcp, used_mcp, context)
        if observation is not None and (refs := spawned.get(call.tool_call_id)):
            (result,) = observation.results
            observation = AtifObservation(
                results=[result.model_copy(update={"subagent_trajectory_ref": refs})]
            )
        builder.add(source="agent", message="", tool_calls=[call], observation=observation)
        for scripted_call, result in scripts.pop(call.tool_call_id, ()):
            # A script, not a model inference, chose this call: ATIF's zero-LLM dispatch step.
            builder.add(
                source="agent",
                message="",
                tool_calls=[scripted_call],
                observation=result,
                llm_call_count=0,
            )


def _errors(builder: TrajectoryBuilder, events: list[dict[str, Any]]) -> None:
    """Failures only stdout reports, at the end of their turn."""
    for event in events:
        if event.get("type") in {"error", "turn.failed"}:
            detail = error_text(event.get("error") or event.get("message")) or str(event)
            builder.add(source="system", message=detail)


def _stream_steps(
    builder: TrajectoryBuilder, events: list[dict[str, Any]], context: TrajectoryParseContext
) -> None:
    """One turn from ``codex exec --json`` events alone."""
    for event in events:
        kind = event.get("type")
        if kind in {"error", "turn.failed"}:
            detail = error_text(event.get("error") or event.get("message")) or str(event)
            builder.add(source="system", message=detail)
            continue
        if kind != "item.completed":
            continue
        item = event.get("item") or {}
        item_type = item.get("type")
        item_id = str(item.get("id") or "")
        if item_type == "agent_message":
            builder.add(source="agent", message=str(item.get("text") or ""))
        elif item_type == "reasoning":
            builder.add(source="agent", message="", reasoning_content=str(item.get("text") or ""))
        elif item_type == "command_execution":
            builder.add(
                source="agent",
                message="",
                tool_calls=[
                    AtifToolCall(
                        tool_call_id=item_id,
                        function_name="shell",
                        arguments={"command": str(item.get("command") or "")},
                    )
                ],
                observation=_observation(
                    item_id,
                    str(item.get("aggregated_output") or ""),
                    error=int(item.get("exit_code") or 0) != 0,
                ),
            )
        elif item_type == "mcp_tool_call":
            server = str(item.get("server") or "")
            tool = str(item.get("tool") or "")
            result, error = item.get("result"), item.get("error")
            builder.add(
                source="agent",
                message="",
                tool_calls=[
                    AtifToolCall(
                        tool_call_id=item_id,
                        function_name=f"mcp__{server}__{tool}",
                        arguments=_dict(item.get("arguments")),
                        extra={"ale": {"mcp": {"server": server, "tool": tool}}},
                    )
                ],
                observation=(
                    _observation(
                        item_id,
                        _result_content(result or error or "", context),
                        error=error is not None,
                    )
                    if result is not None or error is not None
                    else None
                ),
            )
        elif item_type == "file_change":
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
                observation=_observation(item_id, str(item.get("status") or "completed")),
            )
        elif item_type == "web_search":
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
                observation=_observation(
                    item_id,
                    json.dumps(
                        {"status": item.get("status") or "completed", "action": item.get("action")},
                        ensure_ascii=False,
                        default=str,
                    ),
                ),
            )


def _native_call(
    payload: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    transcript_mcp: list[dict[str, Any]],
    used_mcp: set[int],
    context: TrajectoryParseContext,
) -> tuple[AtifToolCall, AtifObservation | None]:
    kind = payload.get("type")
    if kind == "web_search_call":
        call_id = str(payload.get("id") or "")
        return (
            AtifToolCall(
                tool_call_id=call_id,
                function_name="web.run",
                arguments=_dict(payload.get("action")),
            ),
            _observation(
                call_id,
                json.dumps(
                    {
                        "status": payload.get("status") or "completed",
                        "action": payload.get("action"),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            ),
        )
    call_id = str(payload.get("call_id") or payload.get("id") or "")
    output = outputs.get(call_id)
    if kind == "tool_search_call":
        value = (
            {"status": output.get("status"), "tools": output.get("tools") or []} if output else None
        )
        return (
            AtifToolCall(
                tool_call_id=call_id,
                function_name="tool_search_tool",
                arguments=_dict(payload.get("arguments")),
            ),
            _observation(call_id, _session_result_content(value, context)) if output else None,
        )
    if kind == "custom_tool_call":
        return (
            AtifToolCall(
                tool_call_id=call_id,
                function_name=str(payload.get("name") or "custom_tool"),
                arguments={"input": payload.get("input")},
            ),
            _observation(call_id, _session_result_content(output.get("output"), context))
            if output
            else None,
        )
    name = str(payload.get("name") or "")
    namespace = str(payload.get("namespace") or "")
    arguments = _dict(payload.get("arguments"))
    value = output.get("output") if output else None
    error = None
    extra = None
    if namespace.startswith("mcp__"):
        # The namespace carries a sanitized server name; stdout carries the real one and
        # the structured result, so the two are matched on server, tool and arguments.
        server = namespace.removeprefix("mcp__")
        match = next(
            (
                (index, item)
                for index, item in enumerate(transcript_mcp)
                if index not in used_mcp
                and _sanitized(str(item.get("server") or "")) == server
                and item.get("tool") == name
                and _dict(item.get("arguments")) == arguments
            ),
            None,
        )
        if match is not None:
            index, item = match
            used_mcp.add(index)
            server = str(item.get("server") or server)
            error = item.get("error")
            value = item.get("result") if item.get("result") is not None else error
        function_name = f"mcp__{server}__{name}"
        extra = {"ale": {"mcp": {"server": server, "tool": name}}}
    else:
        function_name = _function_name(namespace, name)
    return (
        AtifToolCall(
            tool_call_id=call_id, function_name=function_name, arguments=arguments, extra=extra
        ),
        _observation(call_id, _session_result_content(value, context), error=error is not None)
        if value is not None
        else None,
    )


def _function_name(namespace: str, name: str) -> str:
    """The name of a tool outside MCP, as the model and scripts both call it."""
    if namespace == "multi_agent_v1":
        return f"multi_agent.{name}"
    return f"{namespace}.{name}" if namespace else name


def _script_calls(
    context: TrajectoryParseContext,
    thread_id: str | None,
    traces: list[Path],
    issues: list[dict[str, Any]],
    consumed: set[Path],
) -> dict[str, _ScriptSteps]:
    """The calls code-mode scripts made, for every thread the trace of ``thread_id`` covers.

    The rollout keeps none of them but an MCP call's or a patch's end. Each ``codex exec``
    process, a resume included, writes its own bundle, ``trace-<trace id>-<thread id>``,
    shared by the threads it spawns. A traced thread is present even with no script call.
    """
    found: dict[str, _ScriptSteps] = {}
    for log in traces:
        bundle = log.parent
        if (
            not thread_id
            or log.name != "trace.jsonl"
            or bundle.parent != context.logs_dir / TRACES
            or not bundle.name.endswith(f"-{thread_id}")
        ):
            continue
        consumed.update(path for path in traces if bundle in path.parents)
        events, trace_issues = native_events(context, log)
        issues.extend(trace_issues)
        cells: dict[tuple[str, str], str] = {}
        latest: dict[str, str] = {}
        calls: dict[str, _TracedCall] = {}
        for event in events:
            payload = event.get("payload") or {}
            thread = str(event.get("thread_id") or "")
            if thread:
                found.setdefault(thread, {})
            kind = payload.get("type")
            if kind == "code_cell_started":
                call_id = str(payload.get("model_visible_call_id") or "")
                cells[thread, str(payload.get("runtime_cell_id"))] = latest[thread] = call_id
            elif kind == "tool_call_started":
                requester = _dict(payload.get("requester"))
                if requester.get("type") == "model":
                    latest[thread] = str(payload.get("model_visible_call_id") or "")
                elif requester.get("type") == "code_cell":
                    cell = (thread, str(requester.get("runtime_cell_id")))
                    calls[str(payload.get("tool_call_id"))] = _TracedCall(
                        thread, latest.get(thread, ""), cells.get(cell, ""), payload
                    )
            elif (call := calls.get(str(payload.get("tool_call_id")))) is not None:
                if kind in {"tool_call_runtime_started", "tool_call_runtime_ended"}:
                    call.runtime = payload
                elif kind == "tool_call_ended":
                    call.ended = payload
        for call in calls.values():
            found.setdefault(call.thread, {}).setdefault(call.after, []).append(
                _script_call(context, bundle, call, issues)
            )
    return found


def _scripts(
    scripted: dict[str, _ScriptSteps],
    thread_id: str | None,
    rollout: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> _ScriptSteps:
    """``thread_id``'s script calls; a thread that ran scripts with no trace says so."""
    if thread_id in scripted:
        return scripted[thread_id]
    if any(
        event.get("type") == "response_item"
        and (payload := event.get("payload") or {}).get("type") == "custom_tool_call"
        and payload.get("name") == "exec"
        for event in rollout
    ):
        issues.append({"reason": "rollout_trace_unavailable", "session_id": thread_id})
    return {}


def _unplaced(scripts: _ScriptSteps, thread_id: str | None, issues: list[dict[str, Any]]) -> None:
    """Script calls that followed no model call the rollout kept stay raw evidence."""
    if count := sum(len(calls) for calls in scripts.values()):
        issues.append({"reason": "unplaced_script_calls", "session_id": thread_id, "count": count})


def _script_call(
    context: TrajectoryParseContext,
    bundle: Path,
    call: _TracedCall,
    issues: list[dict[str, Any]],
) -> tuple[AtifToolCall, AtifObservation | None]:
    """A script's call and what the script got back, named as the model would call it."""
    call_id = str(call.started.get("tool_call_id") or "")
    request = _trace_payload(context, bundle, call.started.get("invocation_payload"), issues)
    name = str(request.get("tool_name") or "")
    namespace = str(request.get("tool_namespace") or "")
    body = _dict(request.get("payload"))
    if body.get("type") == "custom":
        arguments = {"input": body.get("input")}
    else:
        arguments = _dict(body.get("arguments") or {})
    extra: dict[str, Any] = {"caller": call.caller} if call.caller else {}
    if namespace.startswith("mcp__"):
        # The namespace sanitizes the server name; Codex's record of the run keeps it.
        runtime = _trace_payload(context, bundle, call.runtime.get("runtime_payload"), issues)
        server = str(
            _dict(runtime.get("invocation")).get("server") or namespace.removeprefix("mcp__")
        )
        function_name = f"mcp__{server}__{name}"
        extra["mcp"] = {"server": server, "tool": name}
    else:
        function_name = _function_name(namespace, name)
    tool_call = AtifToolCall(
        tool_call_id=call_id,
        function_name=function_name,
        arguments=arguments,
        extra={"ale": extra} if extra else None,
    )
    result = _trace_payload(context, bundle, call.ended.get("result_payload"), issues)
    if result.get("type") == "code_mode_response":
        value = result.get("value")
        content = (
            _result_content(value, context)
            if isinstance(value, dict | str)
            else json.dumps(value, ensure_ascii=False, default=str)
        )
        error = call.ended.get("status") != "completed" or (
            isinstance(value, dict) and value.get("isError") is True
        )
        return tool_call, _observation(call_id, content, error=error)
    if result.get("type") == "error":
        return tool_call, _observation(call_id, str(result.get("error") or ""), error=True)
    return tool_call, None


def _trace_payload(
    context: TrajectoryParseContext, bundle: Path, ref: Any, issues: list[dict[str, Any]]
) -> dict[str, Any]:
    """The payload a trace event names, read only from its own bundle's payload files."""
    if not isinstance(ref, dict) or not ref.get("path"):
        return {}
    path = str(ref["path"])
    source = f"{bundle.relative_to(context.logs_dir).as_posix()}/{path}"
    if not _TRACE_PAYLOAD.fullmatch(path) or not (bundle / path).is_file():
        issues.append({"reason": "trace_payload_unavailable", "source": source})
        return {}
    try:
        value = json.loads((bundle / path).read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        issues.append({"reason": "invalid_trace_payload", "source": source, "detail": str(exc)})
        return {}
    return value if isinstance(value, dict) else {}


def _observation(
    call_id: str, content: str | list[AtifContentPart], *, error: bool = False
) -> AtifObservation:
    return AtifObservation(
        results=[
            AtifObservationResult(
                source_call_id=call_id,
                content=content,
                extra={"ale": {"is_error": True}} if error else None,
            )
        ]
    )


def _sanitized(server: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", server)


def _text(payload: dict[str, Any]) -> str:
    return "".join(
        str(part.get("text") or "")
        for part in payload.get("content") or ()
        if isinstance(part, dict) and part.get("type") in {"input_text", "output_text", "text"}
    ).strip()


def _summary(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(part.get("text") or "")
        for part in payload.get("summary") or ()
        if isinstance(part, dict) and part.get("text")
    )


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
    value = _session_meta(events).get("id")
    return value if isinstance(value, str) else None


def _session_meta(events: list[dict[str, Any]]) -> dict[str, Any]:
    """What a rollout's first ``session_meta`` record says about its thread."""
    for event in events:
        if event.get("type") == "session_meta":
            payload = event.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


def _outputs(rollout: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(payload.get("call_id") or ""): payload
        for event in rollout
        if event.get("type") == "response_item"
        and (payload := event.get("payload") or {}).get("type") in _OUTPUT_TYPES
    }


def _agent(context: TrajectoryParseContext) -> AtifAgent:
    return AtifAgent(
        name="codex-cli", version=context.agent_version, model_name=context.model or None
    )


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


def _session_result_content(
    value: Any, context: TrajectoryParseContext
) -> str | list[AtifContentPart]:
    if not isinstance(value, list):
        return _result_content(value, context)
    parts: list[AtifContentPart] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in {"input_text", "output_text", "text"}:
            parts.append(AtifContentPart(type="text", text=str(block.get("text") or "")))
        elif kind == "input_image":
            if match := _DATA_URL.fullmatch(str(block.get("image_url") or "")):
                media_type = match.group(1)
                blob = context.blobs.put(base64.b64decode(match.group(2)), media_type=media_type)
                parts.append(
                    AtifContentPart(
                        type="image",
                        source=AtifImageSource(media_type=media_type, path=blob.path),
                    )
                )
            else:
                # An image ALE cannot store stays visible as the record it was.
                text = json.dumps(block, ensure_ascii=False, default=str)
                parts.append(AtifContentPart(type="text", text=text))
    return parts or json.dumps(value, ensure_ascii=False, default=str)


def _extra(context: TrajectoryParseContext, issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    data: dict[str, Any] = {}
    if issues:
        data["parse_issues"] = issues
    if context.incomplete:
        data["incomplete"] = True
        data["incomplete_reason"] = context.incomplete_reason or "interrupted"
    return {"ale": data} if data else None
