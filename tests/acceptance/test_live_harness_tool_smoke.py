from __future__ import annotations

import json
from typing import Any

import pytest

from .trajectory import (
    LIVE,
    WORKSPACE_ROOT,
    jsonl,
    llm_audit_evidence,
    run_task,
    task_path,
)

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_llm, LIVE]

CASES = (
    (
        "grok-build",
        "grok-4.5",
        "0.2.112",
        "session_chat_history.jsonl",
        ('gateway.dialect="openai-chat-completions"',),
    ),
    (
        "codex-cli",
        "gpt-5.4-mini",
        "0.146.0",
        "transcript.jsonl",
        ('agent.settings.reasoning_effort="high"',),
    ),
    (
        "openclaw-cli",
        "gpt-5.4-mini",
        "2026.7.1",
        "transcript.jsonl",
        ('agent.settings.thinking="off"',),
    ),
)


def _compact(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _tool_evidence(trajectory: dict[str, Any]) -> list[dict[str, object]]:
    evidence = []
    for step in trajectory["steps"]:
        results = {
            result.get("source_call_id"): result
            for result in (step.get("observation") or {}).get("results", [])
        }
        for call in step.get("tool_calls") or ():
            result = results.get(call["tool_call_id"])
            evidence.append(
                {
                    "tool": call["function_name"],
                    "arguments": _compact(call["arguments"], 300),
                    "result": _compact((result or {}).get("content"), 500),
                    "is_error": bool(
                        ((result or {}).get("extra") or {}).get("ale", {}).get("is_error")
                    ),
                }
            )
    return evidence


@pytest.mark.parametrize(
    ("harness", "model", "version", "native_log", "settings"),
    CASES,
    ids=[case[0] for case in CASES],
)
def test_real_harness_exercises_its_visible_tool_surface(
    tmp_path,
    harness: str,
    model: str,
    version: str,
    native_log: str,
    settings: tuple[str, ...],
) -> None:
    task = task_path(
        "ALE_LIVE_TOOL_SMOKE_TASK",
        WORKSPACE_ROOT / "ale-tasks-152/tasks/demo/tool_smoke",
    )
    episode = run_task(
        task,
        tmp_path,
        run_id=f"live-{harness}-tool-smoke",
        agent=harness,
        model=model,
        settings=settings,
    )
    result = json.loads((episode / "result.json").read_text())
    lock = json.loads((episode / "lock.json").read_text())
    trajectory = json.loads((episode / "trajectory.json").read_text())
    transport = jsonl(episode / "trace.transport.jsonl")
    execution = jsonl(episode / "trace.execution.jsonl")
    native = jsonl(episode / "logs" / harness / native_log)
    report = json.loads((episode / "artifacts/output/tool_report.json").read_text())

    tested = len(report["tools_tested"])
    passed = len(report["tools_passed"])
    failed = len(report["tools_failed"])
    untested = len(report["tools_untested"])
    assert tested > 0
    assert all(
        isinstance(report[name], int)
        for name in ("total", "tested", "passed", "failed", "untested")
    )
    assert passed + failed == tested
    assert result["status"] == "completed"
    denominator = max(report["total"], tested + untested)
    assert result["rewards"]["reward"] == round(passed / denominator, 3)
    assert lock["agent"]["harness"] == harness
    assert lock["agent"]["model"] == model
    assert lock["agent"]["version"] == version
    assert native

    calls = [call for step in trajectory["steps"] for call in step.get("tool_calls") or ()]
    assert len(calls) >= tested
    assert [record for record in transport if record["kind"] == "call"]
    verify = [
        record
        for record in execution
        if record["kind"] == "command_finished" and record["phase"] == "verify"
    ]
    assert verify and verify[-1]["outcome"] == "succeeded"

    llm_audit_evidence(
        {
            "trajectory_tool_calls": _tool_evidence(trajectory),
            "transport": {
                "calls": len([record for record in transport if record["kind"] == "call"]),
                "models": sorted(
                    {
                        record["model"]
                        for record in transport
                        if record["kind"] == "call" and record.get("model")
                    }
                ),
                "dispositions": sorted(
                    {record["disposition"] for record in transport if record["kind"] == "call"}
                ),
            },
            "native_log_present": bool(native),
            "tool_report": report,
            "result": result,
            "lock_agent": lock["agent"],
        },
        requirement=(
            f"The {harness} agent actually exercised the visible tools it reported as "
            "tested. Each passed tool has observed trajectory evidence of a real call "
            "and result; failed or safely untestable tools are reported honestly. A "
            "parallel orchestration wrapper may be evidenced by the intended child calls "
            "and their distinct verified results when the native log flattens the wrapper. "
            "Codex native web-search actions are evidence of its visible web.run callable."
        ),
    )
