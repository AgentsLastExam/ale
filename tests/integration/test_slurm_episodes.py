"""Slurm episodes, run on a login node of an explicitly configured cluster, which shares
Orbit's cache with its compute nodes; no Docker is needed there."""

from __future__ import annotations

from pathlib import Path

import pytest

from orbit.run.providers.slurm import SlurmProvider
from orbit.run.providers.slurm.job import Job
from tests.integration.test_validation_rounds import _check as check_rounds
from tests.integration.test_validation_rounds import _episode as rounds_episode
from tests.integration.test_validation_rounds import _task as rounds_task
from tests.integration.test_volume_artifacts import _check as check_volume
from tests.integration.test_volume_artifacts import _episode as volume_episode
from tests.support import slurm_config

pytestmark = [pytest.mark.integration, pytest.mark.needs_slurm]


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "jobs"), [("shared", 1), ("snapshot", 1), ("separate", 4)])
async def test_an_agent_improves_over_validation_rounds_in_one_slurm_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, jobs: int
) -> None:
    """Every turn's solver and every verifier booted from a round's snapshot run in the
    first solver's allocation, from its node's own store: only a separate verifier, from
    an image of its own, gets another."""
    provider = SlurmProvider(slurm_config())
    submitted: list[str] = []
    submit = Job.submit

    async def counted(job: Job, **resources: int | str) -> None:
        # Allocations, not the jobs that prepare images.
        if resources.get("command", "supervise") == "supervise":
            submitted.append(job.name)
        await submit(job, **resources)  # type: ignore[arg-type]

    monkeypatch.setattr(Job, "submit", counted)
    root = rounds_task(tmp_path / f"rounds-slurm-{mode}", mode)
    result = await rounds_episode(tmp_path, root, provider)
    check_rounds(result)
    assert len(submitted) == jobs
    assert provider.snapshots == {}
    kept = await provider.list_retained()
    assert not [record for record in kept if record["episode"] == result.episode_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "validation"), [("separate", None), ("snapshot", None), ("shared", "separate")]
)
async def test_an_agent_works_in_a_volume_in_one_slurm_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, validation: str | None
) -> None:
    """Every turn's solver and every verifier, a separate one of the verifier's image
    included, runs in the first solver's allocation, viewing its volume in the node's
    store."""
    provider = SlurmProvider(slurm_config())
    submitted: list[str] = []
    submit = Job.submit

    async def counted(job: Job, **resources: int | str) -> None:
        # Allocations, not the jobs that prepare images.
        if resources.get("command", "supervise") == "supervise":
            submitted.append(job.name)
        await submit(job, **resources)  # type: ignore[arg-type]

    monkeypatch.setattr(Job, "submit", counted)
    result = await volume_episode(tmp_path, mode, validation, provider)
    check_volume(result, mode, validation)
    assert len(submitted) == 1
    assert provider.snapshots == {}
