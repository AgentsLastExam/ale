from __future__ import annotations

import pytest

from .trajectory import (
    LIVE,
    WORKSPACE_ROOT,
    audit_episode,
    llm_audit_episode,
    run_task,
    task_path,
    trajectory_mcp_calls,
)

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_llm, LIVE]


def test_real_claude_unified_logging_and_llm_trajectory_audit(tmp_path) -> None:
    task = task_path(
        "ALE_LIVE_RESOURCE_TASK",
        WORKSPACE_ROOT / "ale-tasks-base/tasks/demo/resource_injection",
    )
    assert task.is_dir()
    episode = run_task(task, tmp_path, run_id="live-unified-logging")
    transport, steps, _native, lock = audit_episode(episode)
    calls = trajectory_mcp_calls(steps)
    assert any(
        value["mcp"] == {"server": "task-proof", "tool": "derive_fragment"}
        and value["result"] is not None
        for value in calls.values()
    )
    assert any(record["kind"] == "trajectory_link" for record in transport)
    assert lock["agent"]["resources"]
    llm_audit_episode(
        episode,
        requirement=(
            "The agent genuinely loaded the task Skill, called the task-provided MCP "
            "tool, used its result in the submitted artifact, and passed verification."
        ),
    )
