from __future__ import annotations

import json

import pytest

from .trajectory import (
    LIVE,
    WORKSPACE_ROOT,
    audit_episode,
    llm_audit_episode,
    native_mcp_pairs,
    run_task,
    task_path,
    trajectory_mcp_calls,
)

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_llm, LIVE]


def test_real_claude_uses_task_skill_and_task_mcp(tmp_path) -> None:
    task = task_path(
        "ALE_LIVE_RESOURCE_TASK",
        WORKSPACE_ROOT / "ale-tasks-base/tasks/demo/resource_injection",
    )
    assert task.is_dir()
    episode = run_task(task, tmp_path, run_id="live-task-resources")
    _, steps, native, lock = audit_episode(episode)

    calls = [
        value
        for value in trajectory_mcp_calls(steps).values()
        if value["mcp"] == {"server": "task-proof", "tool": "derive_fragment"}
    ]
    assert len(calls) == 1
    call = calls[0]["call"]
    result = calls[0]["result"]
    assert result is not None
    assert call["arguments"]["nonce"]
    fragment = json.loads(result["content"])["structuredContent"]["fragment"]

    pairs = native_mcp_pairs(native)
    assert pairs[call["tool_call_id"]]["use"]["input"] == call["arguments"]
    assert "result" in pairs[call["tool_call_id"]]
    init = next(event for event in native if event.get("subtype") == "init")
    assert "resource-proof" in init["skills"]
    assert {"name": "task-proof", "status": "connected"} in init["mcp_servers"]
    assert any(
        part.get("type") == "tool_use"
        and part.get("name") == "Skill"
        and part.get("input") == {"skill": "resource-proof"}
        for event in native
        for part in ((event.get("message") or {}).get("content") or [])
        if isinstance(part, dict)
    )

    receipt = json.loads((episode / "artifacts/output/mcp-call.json").read_text())
    answer = (episode / "artifacts/output/result.txt").read_text()
    assert receipt == {"nonce": call["arguments"]["nonce"], "fragment": fragment}
    assert answer == f"SKILL-R7::{receipt['nonce']}::{fragment}"
    assert {(item["kind"], item["name"]) for item in lock["agent"]["resources"]} == {
        ("skill", "resource-proof"),
        ("mcp", "task-proof"),
    }
    llm_audit_episode(
        episode,
        requirement=(
            "The agent used the resource-proof Skill and task-proof derive_fragment "
            "MCP tool to produce the verified output."
        ),
    )
