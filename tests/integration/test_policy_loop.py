"""The stepwise path against a real desktop.

The unit tests cover the environment's logic with a fake sandbox; what these add is that
it works against an actual X server — a real screenshot, real input dispatch through
xdotool, real blobs on disk. The guards are verified in both places on purpose: they are
the part an agent could otherwise walk past.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.trace import DesktopAction
from ale.core.trajectory import AtifTrajectory
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import ScriptedPolicyHarness
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import load_tasks
from tests.support import provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_gui]

GUI_IMAGE = "ghcr.io/agentslastexam/sandbox-base-gui:latest"
CLI_IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"


class PolicyEnvironment(StandardEnvironment):
    async def _verify(self, task, ctx, sandbox):  # type: ignore[no-untyped-def]
        return {"reward": 0.0}


def gui_repo(write_repo: Callable[..., Path], root: Path) -> Path:
    """The standard fixture task, moved onto the desktop image."""
    task_root = write_repo(root)
    dockerfile = task_root / "image" / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text().replace(f"FROM {CLI_IMAGE}", f"FROM {GUI_IMAGE}"))
    return task_root


async def run_policy(task_root: Path, run_dir: Path, harness: ScriptedPolicyHarness, **kwargs):  # type: ignore[no-untyped-def]
    task = load_tasks(task_root)[0]
    return await run_episode(
        task,
        PolicyEnvironment(harness, **kwargs),
        provider_registry(DockerProvider()),
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
            [DesktopAction(type="click", coordinate=(500, 500)), DesktopAction(type="screenshot")],
            [DesktopAction(type="type", text="hello"), DesktopAction(type="key", keys=("Return",))],
        ]
    )

    result = await run_policy(task_root, tmp_path / "runs", harness)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    agent_steps = [step for step in trajectory.steps if step.source == "agent"]
    calls = [call for step in agent_steps for call in step.tool_calls or ()]
    results = [
        item
        for step in agent_steps
        for item in (step.observation.results if step.observation else ())
    ]

    # One observation: the one step that asked to see. The other step acted without
    # looking, which is now a thing an agent can choose and the trace can show.
    assert len(agent_steps) == 3
    assert agent_steps[-1].message.startswith("scripted harness ran 2 step")
    assert len(calls) == len(results) == 4

    # Screenshots are files referenced by path, never inlined into the trace.
    screenshot = next(
        item
        for item in results
        if isinstance(item.content, list) and item.content[0].type == "image"
    )
    assert screenshot.content[0].source is not None
    assert (result.run_dir / screenshot.content[0].source.path).is_file()

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

    # Truncation is a normal ending, not an error: the episode is scored on what the
    # agent achieved before it stopped making progress.
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
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

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert len(harness.observations) == 2
