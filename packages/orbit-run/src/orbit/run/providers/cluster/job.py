"""Batch jobs of a cluster, and the one Orbit runs an episode in, whatever its workload
manager.

A login node, which shares Orbit's cache with every compute node, submits a job of any
command and watches it (:class:`Job`): an image's preparation, or an episode's job. Inside an
episode's job, the job is the allocation of every Sandbox of the episode (:class:`Allocation`):
Orbit, on the job's first node, starts the worker's ``supervise`` there as a child of its
own, which holds each node's store and starts each Sandbox. Each workload manager's own
commands and variables are its module's (``slurm``, ``pbs``, ``lsf``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import socket
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from orbit.core.config import ClusterPartition, ClusterProviderConfig
from orbit.core.errors import (
    GuestUnreachableError,
    MemoryExhaustedError,
    ProviderStartError,
    StorageExhaustedError,
)
from orbit.core.sandbox import GpuDevice
from orbit.run.process import run
from orbit.run.providers.memory import GuestEvents
from orbit.run.secrets import host_credentials
from orbit.run.transport import StdioTransport

#: Below :func:`cache`: snapshots written out, which a later job boots from, the images
#: prepared for the cluster, each by its key, and Sandboxes' shared directories.
SNAPSHOTS = "snapshots"
IMAGES = "images"
SHARED = "shared"
#: How long the worker may take to load its nodes' seeds and be ready.
READY_TIMEOUT_SEC = 1800
#: How long the worker may take to remove each node's store once stopped: about a minute
#: for a 2 GB image, and a node's own step's ten (``worker.supervise``).
CLOSE_TIMEOUT_SEC = 900


def cache(config: ClusterProviderConfig) -> Path:
    """Orbit's directory on the cluster's shared file system."""
    return Path(os.path.expandvars(config.shared_fs_dir)).expanduser()


def without_credentials() -> dict[str, str]:
    """This process's environment less the Host's credentials, the environment of every
    process Orbit starts on the cluster: a workload manager hands the environment it is
    given to the job's processes, as ``sbatch`` and ``srun`` do by default."""
    held = host_credentials()
    return {name: value for name, value in os.environ.items() if name not in held}


def unset_credentials() -> list[str]:
    """``env``'s options that leave the Host's credentials out of what it runs."""
    return [option for name in sorted(host_credentials()) for option in ("-u", name)]


def exists(path: Path) -> bool:
    """Whether ``path`` exists, which another node may have just made: the directory is
    listed first, as an NFS client trusts a cached listing, which misses a file another
    machine made, until it opens the directory again."""
    with contextlib.suppress(OSError):
        os.listdir(path.parent)
    return path.exists()


def read(path: Path) -> str:
    """What ``path`` holds, or nothing; see :func:`exists`."""
    if not exists(path):
        return ""
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def write(path: Path, data: bytes) -> None:
    """Write a file the worker reads whole: under another name, then renamed."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def tail(path: Path, size: int = 2000) -> str:
    try:
        with path.open("rb") as file:
            file.seek(max(0, file.seek(0, 2) - size))
            return file.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def stage(
    config: ClusterProviderConfig,
    directory: Path,
    spec: Mapping[str, object] | None = None,
    *,
    name: str,
    helpers: Sequence[str] = (),
) -> None:
    """Give a job ``directory`` below the cache, with an empty ``stop``, the worker, the
    ``helpers`` it runs beside itself, as isolated Python imports no sibling module, and, as
    ``name``, the worker's input ``spec``, with where its nodes keep their stores."""
    cache(config).mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)
    (directory / "stop").touch()
    for module in ("worker.py", *helpers):
        shutil.copyfile(Path(__file__).with_name(module), directory / module)
    if spec is not None:
        spec = {**spec, "node_local_dir": config.node_local_dir}
        write(directory / name, json.dumps(spec).encode())


def command_line(directory: Path, argv: Sequence[str]) -> str:
    """What a job runs, in its directory: ``argv``, unless the job was stopped before it
    began, with its output and errors added to ``job.log``."""
    return (
        f"cd {shlex.quote(str(directory))} && test ! -s stop && "
        f"exec {shlex.join(argv)} >>job.log 2>&1"
    )


class JobReady(BaseModel):
    runtime: str
    node: str
    nodes: tuple[str, ...]
    """Every node of the job, ``node`` first; a Sandbox spans the first it needs."""


class StepReady(BaseModel):
    """What one node of a Sandbox started."""

    image: str
    devices: tuple[GpuDevice, ...]
    step: str
    node: str
    volumes: tuple[str, ...] = ()
    """The volume candidates the image has a directory at, which the Sandbox mounts."""
    hcas: tuple[str, ...] = ()
    """The InfiniBand verbs devices the node gave a Sandbox that spans several."""


class Job:
    """A batch job of one or more alike nodes, submitted from the login node with a
    directory of its own below the cache, from which it runs a command; each workload
    manager's own submits, watches and ends it."""

    #: The helpers the worker runs beside itself.
    helpers: ClassVar[tuple[str, ...]] = ()
    #: Whether a Sandbox may span several nodes of a job.
    spans_nodes: ClassVar[bool] = False

    @classmethod
    async def preflight(
        cls, config: ClusterProviderConfig, partitions: Sequence[ClusterPartition]
    ) -> None:
        """Check, from the login node, that the workload manager runs jobs of
        ``partitions``."""
        raise NotImplementedError

    def __init__(self, config: ClusterProviderConfig, partition: ClusterPartition) -> None:
        self.config = config
        self.partition = partition
        self.name = "orbit-" + uuid.uuid4().hex
        self.directory = cache(config) / self.name
        self.id = ""

    @property
    def workload_manager(self) -> str:
        return self.config.workload_manager

    @property
    def label(self) -> str:
        return self.id or self.name

    @property
    def job_label(self) -> str:
        """The job, as messages name it."""
        return f"{self.workload_manager} job {self.id}"

    def stage(self, spec: Mapping[str, object] | None = None, *, name: str = "recipe.json") -> None:
        """Give the job its directory (:func:`stage`), with the worker's input ``spec`` as
        ``name`` where it has one; whatever else the job reads goes there after."""
        stage(self.config, self.directory, spec, name=name, helpers=self.helpers)

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
        """Submit the job of ``nodes`` nodes, each with ``cpus``, ``memory_mb`` and ``gpus``,
        for ``minutes``, which runs ``argv`` in its directory (:func:`command_line`) on its
        first node, never again once it ends. Before it submits, it records that it does, so
        that :meth:`stop` finds a job by its unique name whose submission's reply was lost."""
        raise NotImplementedError

    async def stop(self, *, cancel: bool = False) -> None:
        """End the job, by its own cleanup, or at once where ``cancel`` asks, as for a job
        that reads no ``stop``, and remove its directory."""
        raise NotImplementedError

    async def pending_reason(self) -> str:
        """Why the workload manager keeps the job waiting in its queue."""
        raise NotImplementedError

    async def ended(self) -> str:
        """How the workload manager recorded the job's end, such as ``PREEMPTED`` or
        ``TIMEOUT``, if it did."""
        raise NotImplementedError

    async def state(self) -> str:
        """The job's state, such as ``PENDING`` or ``RUNNING``; empty once it ended."""
        raise NotImplementedError


async def remove_job_directory(directory: Path) -> None:
    """Remove a job's directory; a compute node may still be closing its NFS handle to the
    job's log, which leaves a silly-renamed file behind for a moment."""
    for _ in range(4):
        await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)
        if not directory.exists():
            return
        await asyncio.sleep(1)
    await asyncio.to_thread(shutil.rmtree, directory)


@dataclass(frozen=True)
class CurrentAllocation:
    """The batch job this process runs in, the one allocation of every Sandbox a Provider
    starts in it."""

    job_id: str
    nodes: tuple[str, ...]
    """In the order the workload manager lists them: this process runs on the first, and a
    Sandbox of ``n`` spans the first ``n``."""
    cpus: int
    memory_mb: int
    gpus: int
    """Each node's, as the job was granted them."""
    deadline: datetime | None
    """The latest time to begin saving what runs in the job, in UTC: its end less
    ``drain_minutes``."""
    partition: str | None = None
    """The partition, or queue, the job runs in."""

    @classmethod
    def from_environment(
        cls,
        allocation: type[Allocation],
        config: ClusterProviderConfig,
        *,
        cpus: int,
        memory_mb: int,
        gpus: int,
        walltime_minutes: int,
        started_at: datetime,
    ) -> CurrentAllocation:
        """The job this process runs in, as its workload manager's variables name it, which
        asked for ``cpus``, ``memory_mb`` and ``gpus`` of each node for ``walltime_minutes``
        and started at ``started_at``, holding what ``allocation`` says its partition grants
        for that, until the end it says: no workload manager is asked for the time."""
        manager = config.workload_manager
        job = os.environ.get(allocation.JOB_VARIABLE, "")
        if not job:
            raise ProviderStartError(
                f"this process runs in no {manager} job: no {allocation.JOB_VARIABLE}"
            )
        queue = os.environ.get(allocation.QUEUE_VARIABLE, "")
        partition = next((part for part in config.partitions if part.name == queue), None)
        if partition is None:
            raise ProviderStartError(
                f"{manager} job {job} runs in partition {queue!r}, which the configuration lacks"
            )
        nodes, cpus, memory_mb, gpus = allocation.held(
            partition, job, cpus=cpus, memory_mb=memory_mb, gpus=gpus
        )
        end = allocation.ends_at(started_at, walltime_minutes)
        return cls(
            job_id=job,
            nodes=nodes,
            cpus=cpus,
            memory_mb=memory_mb,
            gpus=gpus,
            deadline=end - timedelta(minutes=config.drain_minutes),
            partition=partition.name,
        )


class Allocation:
    """The job Orbit runs in, as its Sandboxes' allocation: a directory of its own below the
    cache, which every node of the job sees, and the worker's ``supervise`` there, a child of
    Orbit's own process, which holds each node's store and starts each Sandbox over the
    job's first nodes, as its workload manager holds it to its share of the job. Orbit
    reaches the worker on its own node by commands it runs there."""

    #: The variables that name, in the job's environment, the job and its partition.
    JOB_VARIABLE: ClassVar[str]
    QUEUE_VARIABLE: ClassVar[str]
    #: The helpers the worker runs beside itself.
    helpers: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: ClusterProviderConfig, current: CurrentAllocation) -> None:
        self.config = config
        self.current = current
        #: Unique beyond the job: snapshots' handles and their volume layers' names carry it.
        self.name = "orbit-" + uuid.uuid4().hex
        self.directory = cache(config) / self.name
        self.id = current.job_id
        self._worker: asyncio.subprocess.Process | None = None

    @classmethod
    def held(
        cls, partition: ClusterPartition, job: str, *, cpus: int, memory_mb: int, gpus: int
    ) -> tuple[tuple[str, ...], int, int, int]:
        """The nodes of ``job``, this one first, and what it holds of each, which asked
        ``partition`` for ``cpus``, ``memory_mb`` and ``gpus``: by default this node alone,
        and what it asked for."""
        return (socket.gethostname(),), cpus, memory_mb, gpus

    @classmethod
    def ends_at(cls, started_at: datetime, walltime_minutes: int) -> datetime:
        """When the job ends, which started at ``started_at``: by default as its wall time
        does."""
        return started_at + timedelta(minutes=walltime_minutes)

    @staticmethod
    def check(node: str) -> None:
        """Check that ``node``, the job's first, where Orbit runs, has what the workload
        manager's Sandboxes need of it."""

    @property
    def workload_manager(self) -> str:
        return self.config.workload_manager

    @property
    def job_label(self) -> str:
        """The job, as messages name it."""
        return f"{self.workload_manager} job {self.id}"

    @property
    def nodes(self) -> tuple[str, ...]:
        return self.current.nodes

    @property
    def running(self) -> bool:
        """Whether the worker runs, and with it every node's store."""
        return self._worker is not None and self._worker.returncode is None

    def stage(self, spec: Mapping[str, object]) -> None:
        stage(self.config, self.directory, spec, name="allocation.json", helpers=self.helpers)

    async def open(self, spec: Mapping[str, object]) -> JobReady:
        """Stage the directory with the worker's input ``spec`` and start the worker; what it
        reports once every node's store holds its seed."""
        self.stage(spec)
        with (self.directory / "job.log").open("ab") as log:
            # A session of its own: a signal to Orbit's process group is not the worker's,
            # whose stores Orbit removes by ``stop`` (:meth:`close`).
            self._worker = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "worker.py", "supervise",
                cwd=self.directory,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                env=without_credentials(),
                start_new_session=True,
            )  # fmt: skip
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READY_TIMEOUT_SEC
        while loop.time() < deadline:
            if error := read(self.directory / "error"):
                raise ProviderStartError(f"{self.job_label}'s worker could not start: {error}")
            if ready := read(self.directory / "ready"):
                return JobReady.model_validate_json(ready)
            self.ensure_running()
            await asyncio.sleep(0.5)
        raise ProviderStartError(f"{self.job_label}'s worker was not ready in time")

    async def start(
        self, index: str, spec: dict[str, object], *, nodes: int, timeout: int
    ) -> list[StepReady]:
        """Start a Sandbox as a step of the job over its first ``nodes`` nodes, once the
        worker sees its request; what each node started, by its number in the Sandbox."""
        box = self.directory / "sandboxes" / index
        # Made here, so that the NFS client here, which looks up what each node writes in
        # them, does not trust a cached listing that lacks them.
        for each in (*(Path("nodes", str(rank)) for rank in range(nodes)), Path("peers")):
            (box / each).mkdir(mode=0o700, parents=True, exist_ok=True)
        write(box / "request.json", json.dumps(spec).encode())
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if error := read(box / "error"):
                # The step's own log holds what Podman said.
                raise ProviderStartError(
                    f"{self.job_label} could not start Sandbox {index}: {error}\n"
                    + tail(box / "step.log")
                )
            readies = [read(box / "nodes" / str(rank) / "ready") for rank in range(nodes)]
            if all(readies):
                return [StepReady.model_validate_json(ready) for ready in readies]
            if exists(box / "done"):
                raise ProviderStartError(
                    f"{self.job_label} ended Sandbox {index} before readiness: "
                    + tail(box / "step.log")
                )
            self.ensure_running()
            await asyncio.sleep(1)
        raise ProviderStartError(f"{self.job_label} did not start Sandbox {index}")

    async def save(
        self, index: str, *, nodes: int, timeout: int = 1800
    ) -> list[tuple[str, dict[str, dict[str, object]], int]]:
        """Stop a Sandbox and commit the disk of each of its ``nodes`` to that node's store;
        return, for each by its number, the image, each volume's stack of layers, by path,
        and what the node holds over its image, in bytes."""
        box = self.directory / "sandboxes" / index
        (box / "stop").write_text("save")
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            saved = [read(box / "nodes" / str(rank) / "saved") for rank in range(nodes)]
            if all(saved):
                records = [json.loads(each) for each in saved]
                return [
                    (
                        "sha256:" + str(record["image"]),
                        dict(record.get("stacks") or {}),
                        int(record.get("written") or 0),
                    )
                    for record in records
                ]
            if error := read(box / "error"):
                raise ProviderStartError(
                    f"{self.job_label} could not save Sandbox {index}: {error}"
                )
            if exists(box / "done"):
                raise ProviderStartError(f"Sandbox {index} ended before its disk was saved")
            self.ensure_running()
            await asyncio.sleep(1)
        raise ProviderStartError(f"{self.job_label} did not save Sandbox {index} in time")

    async def end(self, index: str, *, timeout: int = 180) -> None:
        """End a Sandbox's step, idempotently, and wait until the worker has released it."""
        box = self.directory / "sandboxes" / index
        if box.is_dir() and not read(box / "stop"):
            (box / "stop").write_text("destroy")
        for _ in range(timeout):
            if not (box / "request.json").exists() or exists(box / "done") or not self.running:
                return
            await asyncio.sleep(1)
        raise ProviderStartError(f"{self.job_label} did not end Sandbox {index}'s step")

    async def control(self, *args: str, nodes: Sequence[str], timeout: int = 60) -> None:
        """Run the worker's ``args`` on each of ``nodes``, at once, each to its end before a
        failure is raised."""
        results = await asyncio.gather(
            *(run(*self.step(*args, node=node), timeout=timeout) for node in nodes),
            return_exceptions=True,
        )
        for node, result in zip(nodes, results, strict=True):
            if isinstance(result, BaseException):
                raise result
            code, _, error = result
            if code:
                raise ProviderStartError(
                    f"{self.job_label} could not {args[0]} on {node}: " + error.strip()
                )

    def step(self, *args: str, node: str) -> list[str]:
        """What runs the worker's ``args`` on ``node`` of the job, in the directory, without
        the Host's credentials: on Orbit's own node, a process of its own; on another, what
        the workload manager runs there (:meth:`remote_step`)."""
        if node == self.nodes[0]:
            return [
                "env", "-C", str(self.directory), *unset_credentials(),
                sys.executable, "-I", "worker.py", *args,
            ]  # fmt: skip
        return self.remote_step(*args, node=node)

    def remote_step(self, *args: str, node: str) -> list[str]:
        """What runs the worker's ``args`` on ``node``, another node of the job."""
        raise ProviderStartError(f"{self.workload_manager} jobs hold one node, not {node}")

    async def step_accounting_state(self, step: str) -> str:
        """How the workload manager accounts for a Sandbox's step, such as
        ``OUT_OF_MEMORY``, where it can say: a diagnostic, never what the Sandbox's liveness
        rests on."""
        return ""

    async def close(self) -> None:
        """Stop the worker, which ends what runs and removes every node's store, and remove
        the directory: what was written out of it, below the cache, stays. The job goes on,
        as Orbit itself runs in it. Idempotent."""
        worker = self._worker
        if worker is not None and worker.returncode is None:
            write(self.directory / "stop", b"stop")
            try:
                await asyncio.wait_for(worker.wait(), CLOSE_TIMEOUT_SEC)
            except TimeoutError:
                logging.getLogger("orbit.execution").warning(
                    "a job's worker did not end once stopped; it is terminated",
                    extra={"orbit_data": {"job": self.id, "directory": str(self.directory)}},
                )
                # Its SIGTERM handler still removes the stores.
                worker.terminate()
                try:
                    await asyncio.wait_for(worker.wait(), 120)
                except TimeoutError:
                    worker.kill()
                    await worker.wait()
        if not self.directory.is_dir():
            return
        if failure := read(self.directory / "cleanup-error"):
            raise ProviderStartError(
                f"{self.workload_manager} cleanup failed in {self.directory}: {failure}"
            )
        await remove_job_directory(self.directory)

    def ensure_running(self) -> None:
        """Raise unless the worker runs, and with it every node's store."""
        if not self.running:
            raise ProviderStartError(
                f"{self.job_label}'s worker ended: " + tail(self.directory / "job.log")
            )


class SandboxSteps:
    """The control commands of one node of a Sandbox in the job."""

    def __init__(self, job: Allocation, index: str, node: str) -> None:
        self.job = job
        self.index = index
        self.node = node

    def step(self, mode: str, *args: str) -> list[str]:
        return self.job.step(mode, self.index, *args, node=self.node)

    async def argv(self, remote_command: str) -> list[str]:
        return self.step("exec", "sh", "-c", remote_command)


class JobTransport(StdioTransport):
    """A guest session on a node of a Sandbox, which explains its loss by what became of
    every node: an OOM kill on one ends the Sandbox's step on all."""

    def __init__(
        self,
        argv: list[str],
        steps: SandboxSteps,
        ready: StepReady,
        watchers: Sequence[GuestEvents],
    ) -> None:
        super().__init__(argv)
        self.steps = steps
        self.ready = ready
        self.watchers = watchers

    async def explain(self, error: GuestUnreachableError) -> GuestUnreachableError:
        await asyncio.gather(*(watcher.settled(2) for watcher in self.watchers))
        job = self.steps.job
        box = job.directory / "sandboxes" / self.steps.index
        for rank in range(len(self.watchers)):
            if found := read(box / "nodes" / str(rank) / "storage"):
                past = json.loads(found)
                message = (
                    f"Orbit's storage watch stopped the Sandbox: node {rank} wrote "
                    f"{past['written_mb']} MiB over its image, past the {past['storage_mb']} MiB "
                    "its Task declares"
                )
                logging.getLogger("orbit.execution").warning(message)
                return StorageExhaustedError(message)
        for watcher in self.watchers:
            if counted := watcher.oom_killed():
                return MemoryExhaustedError(f"{job.job_label}: {counted} ({error})")
        counters = [box / "nodes" / str(rank) / "memory" for rank in range(len(self.watchers))]
        for _ in range(5):
            if all(exists(each) for each in counters):
                break
            await asyncio.sleep(1)
        state = ""
        with contextlib.suppress(ProviderStartError):
            state = await job.step_accounting_state(self.ready.step)
        if "OUT_OF_MEMORY" in state or any(
            line.startswith("oom_kill ")
            and line.split()[-1].isdigit()
            and int(line.split()[-1]) > 0
            for each in counters
            for line in read(each).splitlines()
        ):
            return MemoryExhaustedError(
                f"{job.workload_manager} recorded an OOM kill in step {job.id}.{self.ready.step}"
            )
        return error
