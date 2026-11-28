"""PBS's and LSF's commands for their jobs, and the job Orbit runs in on each."""

import asyncio
import json
import os
import shlex
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.core.config import LSFProviderConfig, LSFQueue, PBSProviderConfig, PBSQueue
from orbit.core.errors import ProviderStartError
from orbit.run.providers.cluster import delegated
from orbit.run.providers.cluster.job import Allocation, CurrentAllocation, JobReady
from orbit.run.providers.cluster.lsf import LSFJob
from orbit.run.providers.cluster.pbs import PBSAllocation, PBSJob

pytestmark = pytest.mark.unit


def job(tmp_path, manager, *, chunk=None, min_gpus=0):
    pbs = manager == "pbs"
    queue = (PBSQueue if pbs else LSFQueue)(
        name="gpu",
        max_cpus=16,
        max_memory_mb=65536,
        gpu_model={"name": "GB200", "memory_mb": 180000},
        max_gpus=4,
        min_gpus=min_gpus,
        **({} if chunk is None else {"chunk_resources": chunk}),
    )
    config = (PBSProviderConfig if pbs else LSFProviderConfig)(
        shared_fs_dir=str(tmp_path / "shared data"), account="account", partitions=(queue,)
    )
    return (PBSJob if pbs else LSFJob)(config, queue)


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
    """The job runs its command in its directory, submitted without the Host's
    credentials."""
    submitted = job(tmp_path, scheduler, chunk=chunk)
    submitted.stage({"some": "input"})
    calls = []
    monkeypatch.setenv("ORBIT_TEST_API_KEY", "secret-value")
    argv = ["/engine/bin/python3", "-I", "worker.py", "prepare"]

    async def run(*args, stdin, timeout, env):
        calls.append(args)
        assert stdin.decode() == (
            f"#!/bin/sh\ncd {shlex.quote(str(submitted.directory))} && test ! -s stop && "
            "exec /engine/bin/python3 -I worker.py prepare >>job.log 2>&1\n"
        )
        assert "ORBIT_TEST_API_KEY" not in env and env["PATH"] == os.environ["PATH"]
        return (
            0,
            "123.server\n" if scheduler == "pbs" else "Job <123> is submitted to queue <gpu>.\n",
            "",
        )

    monkeypatch.setattr(delegated, "run", run)
    await submitted.submit(cpus=2, memory_mb=1024, minutes=62, gpus=1, argv=argv)
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
    spec = json.loads((submitted.directory / "delegated.json").read_text())
    assert spec["job_variable"] == ("PBS_JOBID" if scheduler == "pbs" else "LSB_JOBID")
    assert (spec["cpus"], spec["memory_mb"], spec["gpus"]) == physical
    assert json.loads((submitted.directory / "recipe.json").read_text())["some"] == "input"
    assert sorted(path.name for path in submitted.directory.iterdir()) == [
        "delegated.json", "delegated_worker.py", "job-id", "recipe.json", "stop",
        "submission", "submitting", "worker.py",
    ]  # fmt: skip
    # A retry cannot double-submit after an ambiguous client outcome.
    with pytest.raises(ProviderStartError, match="already submitted"):
        await submitted.submit(cpus=2, memory_mb=1024, minutes=62, gpus=1, argv=argv)


@pytest.mark.parametrize("scheduler", ["pbs", "lsf"])
async def test_status_and_termination_records_are_scheduler_specific(
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

    monkeypatch.setattr(delegated, "run", run)
    assert await submitted.state() == "RUNNING"
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

    monkeypatch.setattr(delegated, "run", run)
    monkeypatch.setattr(delegated.asyncio, "sleep", sleep)
    with pytest.raises(ProviderStartError, match="connection lost"):
        await submitted.submit(cpus=2, memory_mb=512, gpus=1, minutes=20, argv=["true"])
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

    monkeypatch.setattr(delegated, "run", run)
    task = asyncio.create_task(
        submitted.submit(cpus=1, memory_mb=512, gpus=0, minutes=10, argv=["true"])
    )
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

    monkeypatch.setattr(delegated, "run", run)
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

    monkeypatch.setattr(delegated, "run", run)
    await submitted.submit(cpus=1, memory_mb=1024, gpus=0, minutes=30, argv=["true"])
    assert json.loads((submitted.directory / "delegated.json").read_text())["gpus"] == 1
    assert json.loads((submitted.directory / "recipe.json").read_text())["gpus"] == 0


def _cgroup(root: Path, *, controllers: str = "cpuset cpu io memory pids") -> Path:
    """A job's cgroup as the scheduler made it, below a cgroup v2 mount at ``root``."""
    group = root / "pbs_jobs.service" / "5.server"
    group.mkdir(parents=True)
    (group / "cgroup.controllers").write_text(controllers + "\n")
    (group / "cgroup.subtree_control").write_text("")
    # The kernel lists no process here once Orbit's moved to its own.
    (group / "cgroup.procs").write_text("")
    return group


def test_orbit_and_the_worker_each_get_a_cgroup_of_their_own_in_the_jobs(tmp_path, monkeypatch):
    """Orbit's process in a leaf of its own, the worker's subtree beside it, and the job's
    controllers enabled for both, so that nothing the worker ends is Orbit's."""
    monkeypatch.setattr(delegated, "CGROUP", tmp_path)
    group = _cgroup(tmp_path)
    subtree = delegated.delegate(group, 4242)
    assert subtree == group / "orbit-sandboxes" and subtree.is_dir()
    assert (group / "orbit-engine" / "cgroup.procs").read_text() == "4242"
    assert (group / "cgroup.subtree_control").read_text() == "+cpu +cpuset +memory"
    # Again from Orbit's own leaf, as a later job's allocation in the same process would.
    assert delegated.delegate(group / "orbit-engine", 4242) == subtree


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda group: (group / "cgroup.controllers").write_text("cpu pids"), "memory"),
        (lambda group: (group / "cgroup.procs").write_text("17\n"), "other than Orbit's: 17"),
    ],
)
def test_a_job_whose_cgroup_is_not_orbits_alone_to_split_is_refused(
    tmp_path, monkeypatch, change, why
):
    monkeypatch.setattr(delegated, "CGROUP", tmp_path)
    group = _cgroup(tmp_path)
    change(group)
    with pytest.raises(ProviderStartError, match=why):
        delegated.delegate(group, 4242)
    with pytest.raises(ProviderStartError, match="no cgroup of its job"):
        delegated.delegate(tmp_path, 4242)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes where the account may not")
def test_a_job_whose_cgroup_the_scheduler_did_not_delegate_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(delegated, "CGROUP", tmp_path)
    group = _cgroup(tmp_path)
    group.chmod(0o555)
    try:
        with pytest.raises(ProviderStartError, match="delegate"):
            delegated.delegate(group, 4242)
    finally:
        group.chmod(0o755)


def _current(**update) -> CurrentAllocation:
    fields = dict(
        job_id="5.server",
        nodes=("cn1",),
        cpus=16,
        memory_mb=65536,
        gpus=4,
        deadline=datetime(2026, 11, 27, 13, 0, tzinfo=UTC),
        partition="gpu",
    )
    return CurrentAllocation(**(fields | update))


async def test_the_job_orbit_runs_in_gives_its_worker_the_grant_and_the_cgroup(
    tmp_path, monkeypatch
):
    """Before the worker starts, Orbit splits the job's cgroup; the worker gets its subtree
    and what the job was granted, runs on Orbit's node alone, and leaves the subtree as it
    ends."""
    allocation = PBSAllocation(job(tmp_path, "pbs").config, _current())
    subtree = tmp_path / "group" / "orbit-sandboxes"
    subtree.mkdir(parents=True)
    split = []

    def delegate(group, pid):
        split.append((group, pid))
        return subtree

    async def open_(self, spec):
        self.stage(spec)
        self._worker = None
        return JobReady(runtime="runsc", node="cn1", nodes=("cn1",))

    async def close(self):
        return None

    monkeypatch.setattr(delegated, "own_cgroup", lambda: tmp_path / "group")
    monkeypatch.setattr(delegated, "delegate", delegate)
    monkeypatch.setattr(Allocation, "open", open_)
    monkeypatch.setattr(Allocation, "close", close)
    await allocation.open({"seeds": []})
    assert split == [(tmp_path / "group", os.getpid())]
    assert json.loads((allocation.directory / "delegated.json").read_text()) == {
        "workload_manager": "pbs",
        "job_variable": "PBS_JOBID",
        "podman": None,
        "cpus": 16,
        "memory_mb": 65536,
        "gpus": 4,
        "cgroup": str(subtree),
    }
    assert (allocation.directory / "delegated_worker.py").is_file()
    assert allocation.step("exec", "1", "true", node="cn1")[-3:] == ["exec", "1", "true"]
    with pytest.raises(ProviderStartError, match="hold one node"):
        allocation.step("exec", "1", "true", node="cn2")
    assert await allocation.step_accounting_state("1") == ""
    await allocation.close()
    assert not subtree.exists() and allocation.group is None
