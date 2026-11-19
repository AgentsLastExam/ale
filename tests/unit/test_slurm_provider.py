"""Slurm selection must find one feasible allocation on one compute node."""

import subprocess

import pytest

from ale.core.capability import SandboxNeeds
from ale.core.config import RunConfig
from ale.core.sandbox import SandboxRole
from ale.core.taskspec import Resources
from ale.run.providers import ProviderRegistry
from ale.run.providers.slurm import worker


def provider():
    partitions = [
        dict(host="cpu", name="cpu", max_cpus=64, max_memory_mb=32768),
        dict(
            host="gpu",
            name="small",
            max_cpus=4,
            max_memory_mb=16384,
            max_gpus=2,
            gpu_model=dict(name="NVIDIA H100 80GB HBM3", memory_mb=81559),
        ),
        dict(
            host="gpu",
            name="large",
            max_cpus=16,
            max_memory_mb=32768,
            max_gpus=1,
            gpu_model=dict(name="NVIDIA B200", memory_mb=183359),
        ),
    ]
    config = RunConfig.model_validate(
        {"sandbox": {"providers": ["slurm"], "slurm": {"partitions": partitions}}}
    )
    return ProviderRegistry(config).get("slurm")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resources", "served"),
    [
        (Resources(cpus=64), True),
        (Resources(gpus=2, gpu_types=("H100",)), True),
        (Resources(gpus=1, gpu_memory_mb=100000), True),
        (Resources(gpus=2, gpu_memory_mb=100000), False),
        (Resources(cpus=8, gpus=1, gpu_types=("H100",)), False),
        (Resources(gpus=3), False),
        (Resources(storage_mb=1000), False),
        (Resources(container_features=("nested_containers", "raw_sockets")), False),
        (Resources(container_features=("nested_containers", "mount", "fuse")), True),
    ],
)
async def test_selection(resources, served):
    needs = SandboxNeeds(
        kind="container", os="linux", arch="x86_64", network_mode="block", resources=resources
    )
    why = await provider().unserved(needs, user="user", role=SandboxRole.SOLVER)
    assert (not why) is served


def test_a_job_removes_only_the_stores_of_jobs_that_ended(tmp_path, monkeypatch):
    running = {"7"}
    for name, job in (("ours", "5"), ("running", "7"), ("ended", "9"), ("unmarked", None)):
        store = tmp_path / f"{worker.STORE_PREFIX}{name}"
        store.mkdir()
        if job is not None:
            (store / worker.OWNER).write_text(job)
    (tmp_path / "unrelated").mkdir()

    def squeue(argv, **_):
        listed = "RUNNING" if argv[3] in running else ""
        return subprocess.CompletedProcess(argv, 0 if listed else 1, stdout=listed, stderr="")

    monkeypatch.setenv("SLURM_JOB_ID", "5")
    monkeypatch.setattr(worker.subprocess, "run", squeue)
    assert worker.stale_stores(tmp_path) == [tmp_path / f"{worker.STORE_PREFIX}ended"]
