"""Jobs of workload managers that hold one node and delegate the job's cgroup to it rather
than starting steps, as PBS Professional and Spectrum LSF do: submitted from a login node
with a script on standard input, and the one Orbit runs in, where the worker holds each
Sandbox to its share of the job in a cgroup of its own. Each one's commands are its module's
(``pbs``, ``lsf``)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from orbit.core.config import ClusterPartition, ClusterProviderConfig, LSFQueue, PBSQueue
from orbit.core.errors import ProviderStartError
from orbit.run.images import acquire_lock
from orbit.run.process import run
from orbit.run.providers.cluster.job import (
    Allocation,
    CurrentAllocation,
    Job,
    JobReady,
    command_line,
    read,
    remove_job_directory,
    without_credentials,
    write,
)

#: Where cgroup v2 is mounted.
CGROUP = Path("/sys/fs/cgroup")
#: Below the job's cgroup: Orbit's own process, and the worker's subtree, which holds each
#: Sandbox's.
ENGINE_CGROUP = "orbit-engine"
SANDBOXES_CGROUP = "orbit-sandboxes"
#: What the worker reads of its job, beside itself.
SPEC = "delegated.json"
HELPER = "delegated_worker.py"


async def command(*args: str, stdin: bytes | None = None, absent: re.Pattern | None = None) -> str:
    """Run one of the workload manager's commands here; its output, nothing where it says
    what ``absent`` matches, or why it failed."""
    try:
        code, out, err = await run(*args, stdin=stdin, timeout=60, env=without_credentials())
    except FileNotFoundError:
        raise ProviderStartError(f"{args[0]} is not installed on the login node") from None
    if code:
        detail = err.strip() or out.strip()
        if absent and absent.search(detail):
            return ""
        raise ProviderStartError(f"{args[0]} failed ({code}): {detail}")
    return out.strip()


@dataclass(frozen=True)
class Status:
    """A job's state as its workload manager reports it, or how it ended."""

    state: str = ""
    ended: str = ""
    reason: str = ""


def granted(
    queue: ClusterPartition, *, cpus: int, memory_mb: int, gpus: int
) -> tuple[int, int, int]:
    """What a job that asks ``queue`` for ``cpus``, ``memory_mb`` and ``gpus`` holds: at
    least the queue's ``min_gpus``, and on PBS the queue's own of each resource its select
    chunk leaves out."""
    assert isinstance(queue, PBSQueue | LSFQueue)
    gpus = max(queue.min_gpus, gpus)
    if not isinstance(queue, PBSQueue):
        return cpus, memory_mb, gpus
    chunk = queue.chunk_resources
    return (
        cpus if "ncpus" in chunk else queue.max_cpus,
        memory_mb if "mem" in chunk else queue.max_memory_mb,
        gpus if "ngpus" in chunk else queue.max_gpus,
    )


class DelegatedJob(Job):
    """A single-node job, submitted with a script on standard input, whose ID the login node
    records as it learns it, and finds by the job's unique name where the reply was lost."""

    partition: PBSQueue | LSFQueue
    helpers = (HELPER,)
    #: The workload manager's commands the login node needs.
    COMMANDS: ClassVar[tuple[str, ...]]
    #: The variable that names the job in its environment, for the worker.
    JOB_VARIABLE: ClassVar[str]

    @classmethod
    async def preflight(
        cls, config: ClusterProviderConfig, partitions: Sequence[ClusterPartition]
    ) -> None:
        """Check that the login node runs the workload manager's commands, and that it knows
        each of ``partitions``."""
        for each in cls.COMMANDS:
            if not shutil.which(each):
                raise ProviderStartError(
                    f"{config.workload_manager} needs {each} on the login node"
                )
        for name in dict.fromkeys(queue.name for queue in partitions):
            await command(*cls.queue_command(name))

    @staticmethod
    def queue_command(queue: str) -> tuple[str, ...]:
        """What shows ``queue``, which fails where it does not exist."""
        raise NotImplementedError

    def submission(
        self, *, cpus: int, memory_mb: int, gpus: int, minutes: int, log: str
    ) -> list[str]:
        """What submits the job, which reads its script on standard input."""
        raise NotImplementedError

    def submitted(self, out: str) -> str | None:
        """The job's ID in what the submission printed."""
        raise NotImplementedError

    async def find(self) -> str:
        """The ID of the job by its unique name, or nothing where none is found."""
        raise NotImplementedError

    async def status(self, *, history: bool) -> Status:
        """The job's status, or how it ended, from the record of ended jobs too where
        ``history``."""
        raise NotImplementedError

    async def cancel(self) -> None:
        raise NotImplementedError

    async def submit(
        self,
        *,
        cpus: int,
        memory_mb: int,
        gpus: int,
        minutes: int,
        nodes: int = 1,
        argv: Sequence[str],
    ) -> None:
        if nodes != 1:
            raise ProviderStartError(f"{self.workload_manager} jobs hold one node")
        gpus = max(self.partition.min_gpus, gpus)
        held = granted(self.partition, cpus=cpus, memory_mb=memory_mb, gpus=gpus)
        # What the worker of an image's preparation holds.
        spec = {
            "workload_manager": self.workload_manager,
            "job_variable": self.JOB_VARIABLE,
            "podman": self.config.podman,
            "cpus": held[0],
            "memory_mb": held[1],
            "gpus": held[2],
        }
        script = f"#!/bin/sh\n{command_line(self.directory, argv)}\n".encode()
        argv_ = self.submission(
            cpus=cpus,
            memory_mb=memory_mb,
            gpus=gpus,
            minutes=minutes,
            log=str(self.directory / "batch.log"),
        )
        with (self.directory / "submission").open("a") as lock:
            await acquire_lock(lock)
            if read(self.directory / "stop") or read(self.directory / "submitting"):
                raise ProviderStartError(f"job {self.name} was stopped or already submitted")
            write(self.directory / SPEC, json.dumps(spec).encode())
            write(self.directory / "submitting", b"1")
            # Finish recording the ID before cancellation can release the lock to cleanup.
            pending = asyncio.create_task(self._submit(argv_, script))
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise

    async def _submit(self, argv: list[str], script: bytes) -> None:
        out = await command(*argv, stdin=script)
        job = self.submitted(out)
        if not job:
            raise ProviderStartError(f"{argv[0]} returned an invalid job ID: {out!r}")
        self.id = job
        write(self.directory / "job-id", self.id.encode())

    async def _recover_id(self) -> str:
        if job := read(self.directory / "job-id"):
            return job
        if not read(self.directory / "submitting"):
            return ""
        if found := await self.find():
            write(self.directory / "job-id", found.encode())
        return found

    async def _status(self, *, history: bool = False) -> Status:
        return await self.status(history=history) if self.id else Status()

    async def state(self) -> str:
        return (await self._status()).state

    async def ended(self) -> str:
        return (await self._status(history=True)).ended

    async def pending_reason(self) -> str:
        return (await self._status()).reason

    async def stop(self, *, cancel: bool = False) -> None:
        try:
            submission = (self.directory / "submission").open("a")
        except FileNotFoundError:
            return
        with submission as lock:
            await acquire_lock(lock)
            write(self.directory / "stop", b"stop")
            self.id = self.id or await self._recover_id()
        if self.id and cancel:
            await self.cancel()
        started = asyncio.get_running_loop().time()
        while self.id and (status := await self.state()):
            age = asyncio.get_running_loop().time() - started
            if age > 900:
                raise ProviderStartError(f"{self.workload_manager} did not release job {self.id}")
            if not cancel and (status == "PENDING" or (age > 600 and status != "COMPLETING")):
                await self.cancel()
                cancel = True
            await asyncio.sleep(2)
        if failure := read(self.directory / "cleanup-error"):
            raise ProviderStartError(f"job {self.id} cleanup failed in {self.directory}: {failure}")
        await remove_job_directory(self.directory)


def own_cgroup() -> Path:
    """The cgroup v2 cgroup this process is in."""
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return CGROUP / line[3:].lstrip("/")
    raise ProviderStartError("Orbit runs in no cgroup v2 hierarchy, which each Sandbox needs")


def delegate(group: Path, pid: int) -> Path:
    """Split the job's cgroup ``group``, which the workload manager delegates to the job's account,
    in two: a leaf for Orbit's own process ``pid``, and a subtree for the worker, which
    holds each Sandbox there to its CPUs, memory and swap, given the memory, CPU and, where
    the workload manager offers it, cpuset controllers; return the worker's subtree. Nothing the
    worker ends there reaches Orbit, and the job's cgroup itself, which enables controllers
    for both, holds no process, as cgroup v2 requires of one that does. ``group`` may be
    Orbit's leaf already."""
    if group.name == ENGINE_CGROUP:
        group = group.parent
    try:
        if group == CGROUP:
            raise RuntimeError("Orbit runs in no cgroup of its job's own")
        available = set((group / "cgroup.controllers").read_text().split())
        if absent := {"memory", "cpu"} - available:
            raise RuntimeError("unavailable controllers: " + ", ".join(sorted(absent)))
        engine = group / ENGINE_CGROUP
        engine.mkdir(exist_ok=True)
        (engine / "cgroup.procs").write_text(str(pid))
        if others := (group / "cgroup.procs").read_text().split():
            raise RuntimeError("it holds processes other than Orbit's: " + " ".join(others))
        enabled = set((group / "cgroup.subtree_control").read_text().split())
        if needed := ({"memory", "cpu"} | ({"cpuset"} & available)) - enabled:
            (group / "cgroup.subtree_control").write_text(
                " ".join("+" + name for name in sorted(needed))
            )
        sandboxes = group / SANDBOXES_CGROUP
        sandboxes.mkdir(exist_ok=True)
        return sandboxes
    except (OSError, RuntimeError) as exc:
        raise ProviderStartError(
            f"{group}: each Sandbox needs its own of the job's cgroup v2 memory and CPU "
            f"controllers, which the workload manager must delegate to the job's account: {exc}"
        ) from exc


class DelegatedAllocation(Allocation):
    """The single-node job Orbit runs in, as its Sandboxes' allocation: the worker holds
    each Sandbox to its share of the job in a cgroup of its own below the worker's subtree of
    the job's (:func:`delegate`)."""

    helpers = (HELPER,)

    def __init__(self, config: ClusterProviderConfig, current: CurrentAllocation) -> None:
        super().__init__(config, current)
        #: The worker's subtree of the job's cgroup, once Orbit made it.
        self.group: Path | None = None

    @classmethod
    def held(
        cls, partition: ClusterPartition, job: str, *, cpus: int, memory_mb: int, gpus: int
    ) -> tuple[tuple[str, ...], int, int, int]:
        nodes, *_ = super().held(partition, job, cpus=cpus, memory_mb=memory_mb, gpus=gpus)
        return (nodes, *granted(partition, cpus=cpus, memory_mb=memory_mb, gpus=gpus))

    async def open(self, spec: Mapping[str, object]) -> JobReady:
        # Before the worker starts: as Orbit's child, it starts in Orbit's leaf.
        self.group = delegate(own_cgroup(), os.getpid())
        return await super().open(spec)

    def stage(self, spec: Mapping[str, object]) -> None:
        super().stage(spec)
        assert self.group is not None
        write(
            self.directory / SPEC,
            json.dumps(
                {
                    "workload_manager": self.workload_manager,
                    "job_variable": self.JOB_VARIABLE,
                    "podman": self.config.podman,
                    "cpus": self.current.cpus,
                    "memory_mb": self.current.memory_mb,
                    "gpus": self.current.gpus,
                    "cgroup": str(self.group),
                }
            ).encode(),
        )

    async def close(self) -> None:
        await super().close()
        if self.group is not None:
            # Empty once the worker ended: it removes each Sandbox's cgroup and its own.
            try:
                with contextlib.suppress(FileNotFoundError):
                    self.group.rmdir()
            except OSError as exc:
                raise ProviderStartError(f"the worker left {self.group} in use: {exc}") from exc
            self.group = None
