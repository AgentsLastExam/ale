from __future__ import annotations

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

pytestmark = [
    pytest.mark.needs_docker,
    pytest.mark.needs_gui,
    pytest.mark.needs_llm,
    LIVE,
]


def test_real_claude_uses_cua_desktop_to_solve_screen_task(tmp_path) -> None:
    task = task_path(
        "ALE_LIVE_CUA_TASK",
        WORKSPACE_ROOT / "ale-tasks-152/tasks/demo/screen_code",
    )
    assert task.is_dir()
    episode = run_task(
        task,
        tmp_path,
        run_id="live-cua-desktop",
        settings=('agent.mcp_servers=[{ builtin = "cua-desktop" }]',),
    )
    _, steps, native, lock = audit_episode(episode)

    screenshots = [
        value
        for value in trajectory_mcp_calls(steps).values()
        if value["mcp"] == {"server": "cua-desktop", "tool": "screenshot"}
    ]
    assert screenshots
    screenshot = screenshots[0]
    assert screenshot["result"] is not None
    image = next(item for item in screenshot["result"]["content"] if item["type"] == "image")
    assert image["source"]["media_type"] == "image/png"
    assert (episode / image["source"]["path"]).is_file()

    pairs = native_mcp_pairs(native)
    assert "result" in pairs[screenshot["call"]["tool_call_id"]]
    normalized = screenshot["call"]["extra"]["ale"]["normalized_action"]
    assert normalized["type"] == "screenshot"
    init = next(event for event in native if event.get("subtype") == "init")
    assert {"name": "cua-desktop", "status": "connected"} in init["mcp_servers"]
    assert (episode / "artifacts/output/code.txt").read_text().strip()
    resource = next(item for item in lock["agent"]["resources"] if item["name"] == "cua-desktop")
    assert resource["kind"] == "mcp" and resource["source_layers"] == ["cli"]
    llm_audit_episode(
        episode,
        requirement=(
            "The agent used cua-desktop screenshots and desktop actions to read the "
            "screen-only task and produce the verified code."
        ),
    )
