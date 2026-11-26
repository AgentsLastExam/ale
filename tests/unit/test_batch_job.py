"""Scheduler wire contracts for the PBS and LSF single-node providers."""

import asyncio
import json
import shlex

import pytest

from orbit.core.config import BatchProviderConfig, BatchQueue
from orbit.core.errors import ProviderStartError
from orbit.run.providers.batch import job as batch

pytestmark = pytest.mark.unit


def job(tmp_path, scheduler, *, chunk=None, min_gpus=0):
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
        scheduler=scheduler,
        shared_fs_dir=str(tmp_path / "shared data"),
        account="account",
        partitions=(queue,),
    )
    return batch.BatchJob(config, queue)


@pytest.mark.parametrize(
    ("scheduler", "chunk", "resource_request", "physical"),
    [
        ("pbs", ("ngpus",), "select=1:ngpus=1", (16, 65536, 1)),
        ("pbs", (), "select=1", (16, 65536, 4)),
        ("pbs", None, "select=1:ncpus=2:mem=1024mb:ngpus=1", (2, 1024, 1)),
        ("lsf", None, "span[hosts=1] rusage[mem=1024MB/host]", (2, 1024, 1)),
    ],
)
async def test_submit_records_the_allocation_and_forwards_script_safely(
    tmp_path, monkeypatch, scheduler, chunk, resource_request, physical
):
    submitted = job(tmp_path, scheduler, chunk=chunk)
    submitted.stage({"some": "input"})
    calls = []

    async def run(*args, stdin, timeout):
        calls.append(args)
        assert shlex.quote(str(submitted.directory)).encode() in stdin
        assert b"exec python3 -I worker.py supervise" in stdin
        return (
            0,
            "123.server\n" if scheduler == "pbs" else "Job <123> is submitted to queue <gpu>.\n",
            "",
        )

    monkeypatch.setattr(batch, "run", run)
    await submitted.submit(cpus=2, memory_mb=1024, minutes=62, gpus=1)
    (args,) = calls
    assert resource_request in args
    if scheduler == "pbs":
        assert args[0] == "qsub" and "walltime=01:02:00" in args
        assert "-A" in args and submitted.id == "123.server"
    else:
        assert args[0] == "bsub" and "1:02" in args
        assert args[args.index("-env") + 1] == "none"
        assert "num=1:mode=shared:j_exclusive=yes" in args
        assert "-P" in args and submitted.id == "123"
    assert (submitted.directory / "job-id").read_text() == submitted.id
    spec = json.loads((submitted.directory / "batch.json").read_text())
    assert (spec["cpus"], spec["memory_mb"], spec["gpus"]) == physical
    assert (
        json.loads((submitted.directory / "allocation.json").read_text())["scheduler"] == scheduler
    )
    assert all(
        (submitted.directory / name).is_file()
        for name in ("worker.py", "batch_worker.py", "bridge.py")
    )
    # A retry cannot double-submit after an ambiguous client outcome.
    with pytest.raises(ProviderStartError, match="already submitted"):
        await submitted.submit(cpus=2, memory_mb=1024, minutes=62, gpus=1)


@pytest.mark.parametrize("scheduler", ["pbs", "lsf"])
async def test_status_deadline_and_termination_records_are_scheduler_specific(
    tmp_path, monkeypatch, scheduler
):
    submitted = job(tmp_path, scheduler)
    submitted.id = "123.server" if scheduler == "pbs" else "123"
    listing = (
        [
            "Job Id: 123.server\n    job_state = R\n"
            "    Resource_List.walltime = 03:00:00\n    resources_used.walltime = 00:30:00",
            "Job Id: 123.server\n    job_state = F\n    Exit_status = -29",
        ]
        if scheduler == "pbs"
        else [
            json.dumps({"RECORDS": [{"JOBID": "123", "STAT": "RUN", "TIME_LEFT": "2:30 L"}]}),
            json.dumps(
                {
                    "RECORDS": [
                        {
                            "JOBID": "123",
                            "STAT": "EXIT",
                            "EXIT_REASON": "TERM_RUNLIMIT: job reached its limit",
                        }
                    ]
                }
            ),
        ]
    )

    async def run(*args, **kwargs):
        assert args[-1] == submitted.id
        return 0, listing[0], ""

    monkeypatch.setattr(batch, "run", run)
    assert await submitted.state() == "RUNNING"
    assert await submitted.time_left() == 9000
    listing.pop(0)
    assert not await submitted.state()
    assert await submitted.ended() == "TIMEOUT"


@pytest.mark.parametrize("scheduler", ["pbs", "lsf"])
async def test_uncertain_submission_is_recovered_by_name_before_cancelling(
    tmp_path, monkeypatch, scheduler
):
    submitted = job(tmp_path, scheduler)
    submitted.stage({})
    full_id = "123.server" if scheduler == "pbs" else "123"
    active = True
    cancelled = []

    async def run(command, *args, **kwargs):
        nonlocal active
        if command in ("qsub", "bsub"):
            return 1, "", "connection lost"
        if command == "qselect" or (command == "bjobs" and "-J" in args):
            assert submitted.name in args
            return 0, full_id, ""
        if command in ("qdel", "bkill"):
            cancelled.append(args)
            active = False
            return 0, "", ""
        assert args[-1] == full_id
        if not active:
            return 153, "", "Unknown Job Id" if scheduler == "pbs" else "Job <123> is not found"
        if scheduler == "pbs":
            return 0, "Job Id: 123.server\n    job_state = Q", ""
        return 0, json.dumps({"RECORDS": [{"JOBID": "123", "STAT": "PEND"}]}), ""

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
    submitted = job(tmp_path, "pbs")
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
    submitted = job(tmp_path, "pbs")
    submitted.stage({})
    submitted.id = "123.server"

    async def run(*args, **kwargs):
        return 1, "", "cannot connect to server"

    monkeypatch.setattr(batch, "run", run)
    with pytest.raises(ProviderStartError, match="cannot connect"):
        await submitted.stop()
    assert submitted.directory.exists()


@pytest.mark.parametrize(
    ("scheduler", "expected"),
    [("pbs", "select=1:ngpus=1"), ("lsf", "num=1:mode=shared:j_exclusive=yes")],
)
async def test_queue_gpu_minimum_reserves_hardware_for_cpu_only_sandbox(
    tmp_path, monkeypatch, scheduler, expected
):
    submitted = job(
        tmp_path, scheduler, chunk=("ngpus",) if scheduler == "pbs" else None, min_gpus=1
    )
    submitted.stage({"gpus": 0})

    async def run(*args, **kwargs):
        assert expected in args
        return 0, "23.server" if scheduler == "pbs" else "Job <23> is submitted", ""

    monkeypatch.setattr(batch, "run", run)
    await submitted.submit(cpus=1, memory_mb=1024, gpus=0, minutes=30)
    assert json.loads((submitted.directory / "batch.json").read_text())["gpus"] == 1
    assert json.loads((submitted.directory / "allocation.json").read_text())["gpus"] == 0
