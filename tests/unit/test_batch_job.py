"""Scheduler wire contracts for the single-node PBS provider."""

import asyncio
import json
import shlex

import pytest

from orbit.core.config import BatchProviderConfig, BatchQueue
from orbit.core.errors import ProviderStartError
from orbit.run.providers.batch import job as batch

pytestmark = pytest.mark.unit


def job(tmp_path, *, chunk=None, min_gpus=0):
    queue = BatchQueue(
        name="gpu",
        max_cpus=16,
        max_memory_mb=65536,
        gpu_model={"name": "GB200", "memory_mb": 180000},
        max_gpus=4,
        min_gpus=min_gpus,
        **({} if chunk is None else {"chunk_resources": chunk}),
    )
    config = BatchProviderConfig(
        scheduler="pbs",
        shared_fs_dir=str(tmp_path / "shared data"),
        account="account",
        partitions=(queue,),
    )
    return batch.BatchJob(config, queue)


@pytest.mark.parametrize(
    ("chunk", "resource_request", "physical"),
    [
        (("ngpus",), "select=1:ngpus=1", (16, 65536, 1)),
        ((), "select=1", (16, 65536, 4)),
        (None, "select=1:ncpus=2:mem=1024mb:ngpus=1", (2, 1024, 1)),
    ],
)
async def test_submit_records_the_allocation_and_forwards_script_safely(
    tmp_path, monkeypatch, chunk, resource_request, physical
):
    submitted = job(tmp_path, chunk=chunk)
    submitted.stage({"some": "input"})
    calls = []

    async def run(*args, stdin, timeout):
        calls.append(args)
        assert shlex.quote(str(submitted.directory)).encode() in stdin
        assert b"exec python3 -I worker.py supervise" in stdin
        return 0, "123.server\n", ""

    monkeypatch.setattr(batch, "run", run)
    await submitted.submit(cpus=2, memory_mb=1024, minutes=62, gpus=1)
    (args,) = calls
    assert resource_request in args
    assert args[0] == "qsub" and "walltime=01:02:00" in args
    assert "-A" in args and submitted.id == "123.server"
    assert (submitted.directory / "job-id").read_text() == submitted.id
    spec = json.loads((submitted.directory / "batch.json").read_text())
    assert (spec["cpus"], spec["memory_mb"], spec["gpus"]) == physical
    assert json.loads((submitted.directory / "allocation.json").read_text())["scheduler"] == "pbs"
    assert all(
        (submitted.directory / name).is_file()
        for name in ("worker.py", "batch_worker.py", "bridge.py")
    )
    # A retry cannot double-submit after an ambiguous client outcome.
    with pytest.raises(ProviderStartError, match="already submitted"):
        await submitted.submit(cpus=2, memory_mb=1024, minutes=62, gpus=1)


async def test_status_deadline_and_termination_records_are_scheduler_specific(
    tmp_path, monkeypatch
):
    submitted = job(tmp_path)
    submitted.id = "123.server"
    listing = [
        "Job Id: 123.server\n    job_state = R\n"
        "    Resource_List.walltime = 03:00:00\n    resources_used.walltime = 00:30:00",
        "Job Id: 123.server\n    job_state = F\n    Exit_status = -29",
    ]

    async def run(*args, **kwargs):
        assert args[-1] == submitted.id
        return 0, listing[0], ""

    monkeypatch.setattr(batch, "run", run)
    assert await submitted.state() == "RUNNING"
    assert await submitted.time_left() == 9000
    listing.pop(0)
    assert not await submitted.state()
    assert await submitted.ended() == "TIMEOUT"


async def test_uncertain_submission_is_recovered_by_name_before_cancelling(tmp_path, monkeypatch):
    submitted = job(tmp_path)
    submitted.stage({})
    full_id = "123.server"
    active = True
    cancelled = []

    async def run(command, *args, **kwargs):
        nonlocal active
        if command == "qsub":
            return 1, "", "connection lost"
        if command == "qselect":
            assert submitted.name in args
            return 0, full_id, ""
        if command == "qdel":
            cancelled.append(args)
            active = False
            return 0, "", ""
        assert args[-1] == full_id
        if not active:
            return 153, "", "Unknown Job Id"
        return 0, "Job Id: 123.server\n    job_state = Q", ""

    async def sleep(delay):
        return None

    monkeypatch.setattr(batch, "run", run)
    monkeypatch.setattr(batch.asyncio, "sleep", sleep)
    with pytest.raises(ProviderStartError, match="connection lost"):
        await submitted.submit(cpus=2, memory_mb=512, gpus=1, minutes=20)
    await submitted.stop()
    assert cancelled == [(full_id,)]
    assert not submitted.directory.exists()


async def test_cancelled_client_still_records_accepted_job(tmp_path, monkeypatch):
    submitted = job(tmp_path)
    submitted.stage({})
    entered = asyncio.Event()
    receipt = asyncio.Event()

    async def run(*args, **kwargs):
        entered.set()
        await receipt.wait()
        return 0, "537.server", ""

    monkeypatch.setattr(batch, "run", run)
    task = asyncio.create_task(submitted.submit(cpus=1, memory_mb=512, gpus=0, minutes=10))
    await entered.wait()
    task.cancel()
    receipt.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (submitted.directory / "job-id").read_text() == "537.server"


async def test_scheduler_outage_preserves_job_directory_and_diagnostics(tmp_path, monkeypatch):
    submitted = job(tmp_path)
    submitted.stage({})
    submitted.id = "123.server"

    async def run(*args, **kwargs):
        return 1, "", "cannot connect to server"

    monkeypatch.setattr(batch, "run", run)
    with pytest.raises(ProviderStartError, match="cannot connect"):
        await submitted.stop()
    assert submitted.directory.exists()


async def test_queue_gpu_minimum_reserves_hardware_for_cpu_only_sandbox(tmp_path, monkeypatch):
    submitted = job(tmp_path, chunk=("ngpus",), min_gpus=1)
    submitted.stage({"gpus": 0})

    async def run(*args, **kwargs):
        assert "select=1:ngpus=1" in args
        return 0, "23.server", ""

    monkeypatch.setattr(batch, "run", run)
    await submitted.submit(cpus=1, memory_mb=1024, gpus=0, minutes=30)
    assert json.loads((submitted.directory / "batch.json").read_text())["gpus"] == 1
    assert json.loads((submitted.directory / "allocation.json").read_text())["gpus"] == 0
