"""A real agent delegates to one subagent, and ALE attributes the child's work to it."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from .trajectory import LIVE, MODEL, run_task

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_llm, LIVE]

TASK = Path(__file__).resolve().parents[1] / "fixtures/tasks/subagent-smoke"
MARKER = "SUBAGENT_MARKER_7F3A"
#: Harness -> (ALE_LIVE_<provider>_* upstream, model variable, default model, settings).
CASES = {
    "claude-code": ("ANTHROPIC", "ALE_LIVE_MODEL", MODEL, ()),
    "codex-cli": ("OPENAI", "ALE_LIVE_OPENAI_MODEL", "gpt-5.4-mini", ()),
    "openclaw-cli": (
        "OPENAI",
        "ALE_LIVE_OPENAI_MODEL",
        "gpt-5.4-mini",
        ('agent.settings.thinking="off"',),
    ),
}


def _upstream(provider: str) -> tuple[str, ...]:
    """The API-key endpoint the checkout names for a provider; the preset's when it names none.

    API-key mode is explicit, because ``auto`` prefers a stored subscription login, whose
    calls this test neither meters nor needs.
    """
    flags = ['agent.authentication="api-key"']
    if key_env := os.environ.get(f"ALE_LIVE_{provider}_API_KEY_ENV"):
        flags.append(f'gateway.api_key_env="{key_env}"')
    if base_url := os.environ.get(f"ALE_LIVE_{provider}_BASE_URL"):
        flags.append(f'gateway.base_url="{base_url}"')
    return tuple(flags)


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
    provider, model_env, default_model, settings = CASES[harness]
    episode = run_task(
        TASK,
        tmp_path,
        run_id=f"live-{harness}-subagent",
        agent=harness,
        model=os.environ.get(model_env, default_model),
        settings=(*settings, *_upstream(provider)),
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
