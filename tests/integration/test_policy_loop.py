"""The framework-owned observe-act loop.

What matters here is that the *framework* witnesses every observation and every action.
An autonomous agent reports what it did; a policy agent cannot, because each step passes
through this loop — which is what makes the two families produce comparable trajectories.

The GUI image is large, so these run against it only where a real screen is required.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.trace import DesktopAction, read_records
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import ScriptedPolicyHarness
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import ManifestTaskset

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_gui]

GUI_IMAGE = "ghcr.io/agentslastexam/sandbox-base-gui:latest"


def gui_repo(write_repo: Callable[..., Path], root: Path) -> Path:
    """The standard fixture task, moved onto the desktop image."""
    task_root = write_repo(root)
    manifest = task_root / "task.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "image: docker.io/library/python:3.12-slim", f"image: {GUI_IMAGE}"
        )
    )
    return task_root


async def run_policy(task_root: Path, run_dir: Path, harness: ScriptedPolicyHarness, **kwargs):  # type: ignore[no-untyped-def]
    task = next(iter(ManifestTaskset(task_root).load()))
    return await run_episode(
        task,
        StandardEnvironment(harness, **kwargs),
        DockerProvider(),
        run_dir=run_dir,
    )


@pytest.mark.asyncio
async def test_every_step_is_witnessed_by_the_framework(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """Observations and actions are recorded because they passed through the loop."""
    task_root = gui_repo(write_repo, tmp_path / "repo")
    harness = ScriptedPolicyHarness(
        [
            [DesktopAction(type="click", coordinate=(500, 500))],
            [DesktopAction(type="type", text="hello"), DesktopAction(type="key", keys=("Return",))],
        ]
    )

    result = await run_policy(task_root, tmp_path / "runs", harness)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    records = list(read_records(result.run_dir / "trace.semantic.jsonl"))

    observations = [r for r in records if r["kind"] == "observation"]
    actions = [r for r in records if r["kind"] == "action"]

    # Three observations: two that produced actions, one that ended the episode.
    assert len(observations) == 3
    assert len(actions) == 3
    assert [a["step"] for a in actions] == [0, 1, 1]

    # Screenshots are files referenced by path, never inlined into the trace.
    assert all(o["screenshot_ref"].startswith("blobs/") for o in observations)
    for observation in observations:
        assert (result.run_dir / observation["screenshot_ref"]).is_file()

    # The harness saw the instruction exactly once, on the first step.
    assert harness.observations[0].instruction is not None
    assert all(o.instruction is None for o in harness.observations[1:])


@pytest.mark.asyncio
async def test_a_stalled_agent_ends_the_episode(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """Acting without the screen ever changing is no progress, not patience."""
    task_root = gui_repo(write_repo, tmp_path / "repo")
    # A harmless action that cannot change anything, repeated forever.
    harness = ScriptedPolicyHarness([[DesktopAction(type="wait", duration_ms=10)]] * 50)

    result = await run_policy(task_root, tmp_path / "runs", harness, stall_limit=3)

    assert result.verdict.status is Status.AGENT_ERROR
    assert "no progress" in (result.verdict.failure.message if result.verdict.failure else "")
    # It stopped early rather than burning the phase deadline.
    assert len(harness.observations) < 10


@pytest.mark.asyncio
async def test_the_step_ceiling_ends_the_episode(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """A run-level ceiling binds an agent that was never written to respect one."""
    task_root = gui_repo(write_repo, tmp_path / "repo")
    harness = ScriptedPolicyHarness([[DesktopAction(type="wait", duration_ms=10)]] * 50)

    # A stall limit above the step ceiling isolates the ceiling as the cause.
    result = await run_policy(task_root, tmp_path / "runs", harness, max_steps=2, stall_limit=99)

    assert result.verdict.status is Status.AGENT_ERROR
    assert "within 2 steps" in (result.verdict.failure.message if result.verdict.failure else "")
    assert len(harness.observations) == 2
