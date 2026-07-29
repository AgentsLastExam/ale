from __future__ import annotations

import json

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

CASES = (
    ("grok-build", "grok-4.5", "0.2.112", "session_chat_history.jsonl"),
    ("codex-cli", "gpt-5-mini", "0.146.0", "transcript.jsonl"),
    ("openclaw-cli", "gpt-5-mini", "2026.7.1", "transcript.jsonl"),
)


@pytest.mark.parametrize(
    ("harness", "model", "version", "native_log"),
    CASES,
    ids=[case[0] for case in CASES],
)
def test_real_harness_uses_task_skill_and_task_mcp(
    tmp_path, harness: str, model: str, version: str, native_log: str
) -> None:
    task = task_path(
        "ALE_LIVE_RESOURCE_TASK",
        WORKSPACE_ROOT / "ale-tasks-base/tasks/demo/resource_injection",
    )
    episode = run_task(
        task,
        tmp_path,
        run_id=f"live-{harness}-resources",
        agent=harness,
        model=model,
    )
    _, steps, native, lock = audit_episode(
        episode,
        model=model,
        harness=harness,
        version=version,
        native_log=native_log,
    )

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
    assert native

    receipt = json.loads((episode / "artifacts/output/mcp-call.json").read_text())
    answer = (episode / "artifacts/output/result.txt").read_text()
    assert receipt["nonce"] == call["arguments"]["nonce"]
    assert answer == f"SKILL-R7::{receipt['nonce']}::{receipt['fragment']}"
    assert {(item["kind"], item["name"]) for item in lock["agent"]["resources"]} == {
        ("skill", "resource-proof"),
        ("mcp", "task-proof"),
    }
    llm_audit_episode(
        episode,
        requirement=(
            f"The {harness} agent used the injected resource-proof Skill and the "
            "task-proof derive_fragment MCP tool to produce the verifier-approved output."
        ),
    )
