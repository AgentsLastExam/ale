"""Scaffold, lint, validate, run — the loop a task author actually lives in.

Each step is tested elsewhere; what this covers is that they compose. A scaffold that
lints clean but cannot be validated, or a task that validates but behaves differently
when run by path, wastes the author's time in exactly the place we promised not to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import NopHarness, OracleHarness
from ale.run.lint import lint_repository
from ale.run.providers.docker import DockerProvider
from ale.run.scaffold import scaffold_task
from ale.run.sources import resolve
from ale.run.tasksets.manifest import ManifestTaskset

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"


def new_repo(root: Path) -> Path:
    (root / "tasks").mkdir(parents=True)
    (root / "domain.yaml").write_text("name: demo\nrequires_core: '>=0.1,<0.2'\n")
    return root


def scaffold_on_our_image(root: Path, name: str = "first") -> Path:
    """Scaffold, then point it at a real base image the way an author would."""
    task = scaffold_task(root / "tasks" / name)
    manifest = task / "task.yaml"
    manifest.write_text(manifest.read_text().replace("image: sandbox-base-cli", f"image: {IMAGE}"))
    return task


async def run(task_root: Path, run_dir: Path, harness: object):  # type: ignore[no-untyped-def]
    task = next(iter(ManifestTaskset(task_root).load()))
    return await run_episode(
        task,
        StandardEnvironment(harness),  # type: ignore[arg-type]
        DockerProvider(),
        run_dir=run_dir,
    )


@pytest.mark.asyncio
async def test_a_scaffolded_task_lints_and_validates_immediately(tmp_path: Path) -> None:
    """The point of a self-solving scaffold: the author starts from green."""
    root = new_repo(tmp_path / "repo")
    task = scaffold_on_our_image(root)

    assert lint_repository(root) == []

    result = await run(task, tmp_path / "runs", OracleHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    episode = result.run_dir
    assert episode is not None
    for name in (
        "trajectory.json",
        "trace.transport.jsonl",
        "trace.execution.jsonl",
        "result.json",
    ):
        assert (episode / name).is_file()
    assert json.loads((episode / "result.json").read_text())["rewards"] == {"reward": 1.0}


@pytest.mark.asyncio
async def test_an_edit_that_breaks_the_task_is_caught_by_validation(tmp_path: Path) -> None:
    """Changing one thing from a known-good state is how the loop is meant to be used."""
    root = new_repo(tmp_path / "repo")
    task = scaffold_on_our_image(root)

    # The instruction now asks for something the oracle does not produce.
    instruction = task / "instruction.md"
    instruction.write_text(instruction.read_text().replace("${greeting}", "${greeting} there"))
    verify = task / "verify" / "run.sh"
    verify.write_text(verify.read_text().replace('= "hello"', '= "hellothere"'))

    result = await run(task, tmp_path / "runs", OracleHarness())
    assert result.verdict.rewards == {"reward": 0.0}, (
        "validation passed a task its oracle cannot solve"
    )


@pytest.mark.asyncio
async def test_running_by_path_matches_running_by_name(tmp_path: Path) -> None:
    """A local path and a registry name differ only in what provenance records."""
    root = new_repo(tmp_path / "repo")
    task = scaffold_on_our_image(root)

    resolved = resolve(str(task))
    assert resolved.source.kind == "local"
    assert not resolved.source.is_reproducible

    # Same loader, same spec, whichever way it was reached.
    from_path = next(iter(ManifestTaskset(resolved.task_dir).load()))
    from_dir = next(iter(ManifestTaskset(task).load()))
    assert from_path.spec.spec_hash == from_dir.spec.spec_hash


@pytest.mark.asyncio
async def test_a_task_without_an_oracle_cannot_bypass_validation(tmp_path: Path) -> None:
    root = new_repo(tmp_path / "repo")
    task = scaffold_on_our_image(root)
    (task / "oracle" / "run.sh").unlink()

    assert any("no oracle" in f.message for f in lint_repository(root))


@pytest.mark.asyncio
async def test_an_idle_agent_scores_a_real_zero(tmp_path: Path) -> None:
    """What an author sees when the agent does nothing: a score, not an error."""
    root = new_repo(tmp_path / "repo")
    task = scaffold_on_our_image(root)

    result = await run(task, tmp_path / "runs", NopHarness())
    assert result.verdict.status is Status.COMPLETED
    assert result.verdict.rewards == {"reward": 0.0}
