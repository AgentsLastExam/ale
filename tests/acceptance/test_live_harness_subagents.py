"""A real agent delegates to one subagent, and ALE attributes the child's work to it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from .trajectory import LIVE, MODELS, run_task

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_llm, LIVE]

TASK = Path(__file__).resolve().parents[1] / "fixtures/tasks/subagent-smoke"
MARKER = "SUBAGENT_MARKER_7F3A"
#: Harness -> (model, settings).
CASES = {
    "claude-code": (MODELS["anthropic"], ()),
    "codex-cli": (MODELS["openai"], ()),
    "openclaw-cli": (MODELS["openai"], ('agent.settings.thinking="off"',)),
}


def _refs(trajectory: dict[str, Any]) -> dict[str, str]:
    """Each referenced subagent trajectory ID and the call that spawned it."""
    return {
        ref["trajectory_id"]: result["source_call_id"]
        for step in trajectory["steps"]
        for result in (step.get("observation") or {}).get("results", [])
        for ref in result.get("subagent_trajectory_ref") or ()
    }


def _calls(trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    return [call for step in trajectory["steps"] for call in step.get("tool_calls") or ()]


@pytest.mark.parametrize("harness", sorted(CASES))
def test_real_subagent_work_lands_in_its_own_trajectory(tmp_path: Path, harness: str) -> None:
    model, settings = CASES[harness]
    episode = run_task(
        TASK,
        tmp_path,
        run_id=f"live-{harness}-subagent",
        agent=harness,
        model=model,
        settings=settings,
    )
    result = json.loads((episode / "result.json").read_text())
    trajectory = json.loads((episode / "trajectory.json").read_text())
    issues = ((trajectory.get("extra") or {}).get("ale") or {}).get("parse_issues", [])
    subagents = trajectory.get("subagent_trajectories") or []
    refs = _refs(trajectory)

    assert result["status"] == "completed", result
    assert result["rewards"] == {"overall": 1.0}, result
    # Every native child session was linked to its spawn and converted.
    assert not [
        issue
        for issue in issues
        if issue["reason"] in {"unlinked_subagent", "unconverted_native_files"}
    ], issues
    assert subagents
    # Each is referenced from the call that spawned it; ATIF validation already
    # guarantees that call is in the referencing step.
    assert {subagent["trajectory_id"] for subagent in subagents} <= refs.keys()
    # The child wrote the marker, and its trajectory holds that call.
    assert any(
        MARKER in json.dumps(call["arguments"], ensure_ascii=False)
        for subagent in subagents
        for call in _calls(subagent)
    ), subagents
