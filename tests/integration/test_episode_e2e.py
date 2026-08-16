"""One task folder, one container, one verdict.

This is the vertical slice: a real task repository on disk, a real container, the task's
own setup and verify stages, and a scored result. It runs the oracle harness, so it needs
no model and no gateway — everything else is the production path.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.trace import read_jsonl
from ale.core.trajectory import AtifTrajectory
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import NopHarness, OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets import load_tasks
from tests.support import provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


async def run_one(task_root: Path, run_dir: Path, harness: object):  # type: ignore[no-untyped-def]
    task = load_tasks(task_root)[0]
    return await run_episode(
        task,
        StandardEnvironment(harness),  # type: ignore[arg-type]
        provider_registry(DockerProvider()),
        run_dir=run_dir,
    )


@pytest.mark.asyncio
async def test_oracle_solves_the_task(tmp_path: Path, write_repo: Callable[..., Path]) -> None:
    """The admission gate: a task whose own solution scores full marks."""
    task_root = write_repo(tmp_path / "repo")
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
async def test_idle_agent_scores_zero_but_completes(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """Doing nothing is a legitimate zero, not an error."""
    task_root = write_repo(tmp_path / "repo")
    result = await run_one(task_root, tmp_path / "runs", NopHarness())

    assert result.verdict.status is Status.COMPLETED
    assert result.verdict.rewards == {"reward": 0.0}


@pytest.mark.asyncio
async def test_broken_verifier_is_a_task_error_not_a_zero(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """A verifier that writes nothing is a defect in the task, and must say so."""
    task_root = write_repo(
        tmp_path / "repo",
        verify_body="#!/usr/bin/env bash\nexit 0\n",  # exits cleanly, writes no rewards
    )
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())

    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.rewards is None


@pytest.mark.asyncio
async def test_agent_never_sees_verification_material(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """The reason answers stay on the host until scoring."""
    task_root = write_repo(tmp_path / "repo")
    (task_root / "verify" / "answer.txt").write_text("hello world")

    probe = task_root / "setup" / "run.sh"
    probe.parent.mkdir()
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /home/user/input /home/user/output\n"
        "printf 'world' > /home/user/input/word.txt\n"
        "for p in /opt/ale/verify /home/user/reference; do\n"
        '  if [ -e "$p" ]; then echo "LEAK: $p" >&2; exit 17; fi\n'
        "done\n"
    )
    result = await run_one(task_root, tmp_path / "runs", NopHarness())

    # Setup runs before the agent; if scoring material were staged, it would exit 17.
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure


@pytest.mark.asyncio
async def test_traces_and_artifacts_are_written(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(tmp_path / "repo")
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())

    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    assert trajectory.steps[0].source == "user"
    execution = read_jsonl(result.run_dir / "trace.execution.jsonl").records
    assert {"phase_started", "command_started", "command_finished", "phase_finished"} <= {
        record["kind"] for record in execution
    }
    assert not (result.run_dir / "trace.semantic.jsonl").exists()
    assert not (result.run_dir / "events.jsonl").exists()
    assert (result.run_dir / "result.json").is_file()
    assert (result.run_dir / "artifacts" / "output" / "result.txt").is_file()
