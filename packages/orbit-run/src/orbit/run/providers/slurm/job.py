"""Submit and drive one batch job, and the Sandboxes in it, from the machine Orbit runs on: a
login node of the cluster, which shares Orbit's cache with every compute node."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from orbit.core.config import SlurmPartition, SlurmProviderConfig
from orbit.core.errors import (
    AllocationEndedError,
    GuestUnreachableError,
    MemoryExhaustedError,
    ProviderStartError,
)
from orbit.core.sandbox import GpuDevice
from orbit.run.process import run
from orbit.run.providers.memory import GuestEvents
from orbit.run.transport import StdioTransport

#: How Slurm records a job it ended itself: at its wall time, for a job of higher
#: priority, or as its node failed.
ENDED = {"TIMEOUT", "PREEMPTED", "NODE_FAIL", "DEADLINE"}
#: Below :func:`cache`: snapshots another allocation boots from, the images prepared for
#: the cluster, each by its key, and Sandboxes' shared directories.
SNAPSHOTS = "snapshots"
IMAGES = "images"
SHARED = "shared"
#: How often a job still waiting in the queue says so, with Slurm's reason.
QUEUE_NOTICE_SEC = 600


def cache(config: SlurmProviderConfig) -> Path:
    """Orbit's directory on the cluster's shared file system."""
    return Path(config.shared_fs_dir).expanduser()


async def slurm(*argv: str, timeout: float = 60) -> str:
    """Run one of Slurm's commands here; its output, or why it failed."""
    try:
        code, out, err = await run(*argv, timeout=timeout)
    except FileNotFoundError:
        raise ProviderStartError(f"Slurm's {argv[0]} is not installed here") from None
    if code:
        raise ProviderStartError(f"Slurm {argv[0]}: {err.strip() or out.strip()}")
    return out.strip()


def exists(path: Path) -> bool:
    """Whether ``path`` exists, which a compute node may have just made: the directory is
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
    """Write a file the batch step reads whole: under another name, then renamed."""
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


def seconds(text: str) -> int | None:
    """A time as Slurm prints it, ``[days-]hours:minutes:seconds`` or shorter, in seconds;
    None for none."""
    text = text.strip()
    if text in ("", "UNLIMITED", "NOT_SET", "INVALID"):
        return None
    days, _, clock = text.rpartition("-")
    parts = [int(part) for part in clock.split(":")]
    hours, minutes, secs = [0] * (3 - len(parts)) + parts
    return ((int(days or 0) * 24 + hours) * 60 + minutes) * 60 + secs


class JobReady(BaseModel):
    runtime: str
    node: str
    nodes: tuple[str, ...]
    """Every node of the allocation, ``node`` first; a Sandbox spans the first it needs."""


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
    """One batch job of one or more alike nodes, whose batch step runs each Sandbox started
    in it as a step of its own over the first nodes it spans."""

    def __init__(self, config: SlurmProviderConfig, partition: SlurmPartition) -> None:
        self.config = config
        self.partition = partition
        self.name = "orbit-" + uuid.uuid4().hex
        self.directory = cache(config) / self.name
        self.id = ""

    @property
    def label(self) -> str:
        return self.id or self.name

    def stage(self, spec: dict[str, object], *, name: str = "allocation.json") -> None:
        """Give the job its directory, the worker and the worker's input, as ``name``, with
        where its nodes keep their stores."""
        cache(self.config).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.mkdir(mode=0o700)
        (self.directory / "stop").touch()
        shutil.copyfile(Path(__file__).with_name("worker.py"), self.directory / "worker.py")
        spec = {**spec, "node_local_dir": self.config.node_local_dir}
        write(self.directory / name, json.dumps(spec).encode())

    async def submit(
        self,
        *,
        cpus: int,
        memory_mb: int,
        gpus: int,
        minutes: int,
        nodes: int = 1,
        command: str = "supervise",
    ) -> None:
        """Submit the job of ``nodes`` nodes, each with ``cpus``, ``memory_mb`` and ``gpus``,
        whose batch step runs the worker's ``command``."""
        args = [
            "sbatch",
            "--parsable",
            "--no-requeue",
            f"--nodes={nodes}",
            "--ntasks-per-node=1",
            f"--job-name={self.name}",
            f"--partition={self.partition.name}",
            f"--cpus-per-task={cpus}",
            f"--mem={memory_mb}M",
            f"--time={minutes}",
            f"--chdir={self.directory}",
            "--output=job.log",
        ]
        if self.partition.qos:
            args.append("--qos=" + self.partition.qos)
        if self.config.account:
            args.append("--account=" + self.config.account)
        if self.partition.constraint:
            args.append("--constraint=" + self.partition.constraint)
        if gpus:
            args.append(f"--gres=gpu:{gpus}")
        args.append(f"--wrap=exec python3 -I worker.py {command}")
        # Not once the job is stopped, which a cancelled submission may race.
        submit = f"test ! -s stop && {shlex.join(args)}"
        out = await slurm(
            "flock",
            str(self.directory / "submission"),
            "sh",
            "-c",
            f"cd {self.directory} && {submit}",
        )
        self.id = out.partition(";")[0]
        if not self.id.isdecimal():
            raise ProviderStartError(f"sbatch returned an invalid job id: {out!r}")

    async def ready(self, queue_timeout: int | None) -> JobReady:
        """Wait for the batch step to be ready: ``queue_timeout`` seconds in the queue, or
        as long as Slurm keeps the job pending where that is ``None``, then 1800 s to start."""
        loop = asyncio.get_running_loop()
        deadline = None if queue_timeout is None else loop.time() + queue_timeout
        started = False
        noticed = loop.time()
        while deadline is None or loop.time() < deadline:
            if error := read(self.directory / "error"):
                raise ProviderStartError(f"Slurm job {self.id} could not start: {error}")
            if ready := read(self.directory / "ready"):
                return JobReady.model_validate_json(ready)
            state = await self.state()
            if state in {"RUNNING", "CONFIGURING"} and not started:
                started = True
                deadline = loop.time() + 1800
            if state == "PENDING" and loop.time() - noticed >= QUEUE_NOTICE_SEC:
                noticed = loop.time()
                logging.getLogger("orbit.execution").info(
                    "a Slurm job still waits in the queue",
                    extra={"orbit_data": {"job": self.id, "reason": await self.pending_reason()}},
                )
            if state not in {"PENDING", "RUNNING", "CONFIGURING"}:
                raise ProviderStartError(
                    f"Slurm job {self.id} ended before readiness ({state or 'gone'}): "
                    + tail(self.directory / "job.log")
                )
            await asyncio.sleep(2)
        raise ProviderStartError(
            f"Slurm job {self.id} timed out during {'startup' if started else 'queueing'}"
        )

    async def start(
        self, index: str, spec: dict[str, object], *, nodes: int, timeout: int
    ) -> list[StepReady]:
        """Start a Sandbox as a step of this job over its first ``nodes`` nodes, once the
        batch step sees its request; what each node started, by its number in the Sandbox."""
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
                    f"Slurm job {self.id} could not start Sandbox {index}: {error}\n"
                    + tail(box / "step.log")
                )
            readies = [read(box / "nodes" / str(rank) / "ready") for rank in range(nodes)]
            if all(readies):
                return [StepReady.model_validate_json(ready) for ready in readies]
            if exists(box / "done"):
                raise ProviderStartError(
                    f"Slurm job {self.id} ended Sandbox {index}'s step before readiness: "
                    + tail(box / "step.log")
                )
            await self._alive()
            await asyncio.sleep(1)
        raise ProviderStartError(f"Slurm job {self.id} did not start Sandbox {index}")

    async def save(
        self, index: str, *, nodes: int, timeout: int = 1800
    ) -> list[tuple[str, dict[str, dict[str, object]]]]:
        """Stop a Sandbox and commit the disk of each of its ``nodes`` to that node's store;
        return, for each by its number, the image and each volume's stack of layers, by
        path."""
        box = self.directory / "sandboxes" / index
        (box / "stop").write_text("save")
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            saved = [read(box / "nodes" / str(rank) / "saved") for rank in range(nodes)]
            if all(saved):
                records = [json.loads(each) for each in saved]
                return [
                    ("sha256:" + str(record["image"]), dict(record.get("stacks") or {}))
                    for record in records
                ]
            if error := read(box / "error"):
                raise ProviderStartError(
                    f"Slurm job {self.id} could not save Sandbox {index}: {error}"
                )
            if exists(box / "done"):
                raise ProviderStartError(f"Sandbox {index} ended before its disk was saved")
            await self._alive()
            await asyncio.sleep(1)
        raise ProviderStartError(f"Slurm job {self.id} did not save Sandbox {index} in time")

    async def end(self, index: str, *, timeout: int = 180) -> None:
        """End a Sandbox's step, idempotently, and wait until Slurm has released it."""
        box = self.directory / "sandboxes" / index
        if box.is_dir() and not read(box / "stop"):
            (box / "stop").write_text("destroy")
        for _ in range(timeout):
            if not (box / "request.json").exists() or exists(box / "done"):
                return
            if not await self.state():
                return
            await asyncio.sleep(1)
        raise ProviderStartError(f"Slurm job {self.id} did not end Sandbox {index}'s step")

    async def control(self, *args: str, nodes: Sequence[str], timeout: int = 60) -> None:
        """Run the worker's ``args`` on each of ``nodes``, at once."""
        code, _, error = await run(*self.step(*args, nodes=nodes), timeout=timeout)
        if code:
            raise ProviderStartError(f"job {self.id} could not {args[0]}: {error.strip()}")

    def step(self, *args: str, nodes: Sequence[str]) -> list[str]:
        """A step that runs the worker's ``args`` once on each of ``nodes``."""
        return [
            "srun",
            "--unbuffered",
            "--overlap",
            f"--nodes={len(nodes)}",
            f"--ntasks={len(nodes)}",
            "--ntasks-per-node=1",
            "--nodelist=" + ",".join(nodes),
            "--jobid=" + self.id,
            f"--chdir={self.directory}",
            "python3",
            "-I",
            "worker.py",
            *args,
        ]

    async def stop(self, *, cancel: bool = False) -> None:
        """End the job, by its batch step's own cleanup, or at once where ``cancel`` asks, as
        for a job whose batch step reads no ``stop``, and remove its directory."""
        if self.directory.is_dir():
            # By the unique name, even if cancellation interrupted sbatch's reply.
            await slurm(
                "flock", str(self.directory / "submission"), "sh", "-c",
                f"echo stop > {shlex.quote(str(self.directory / 'stop'))}",
            )  # fmt: skip
        if cancel:
            await slurm("scancel", "--me", f"--name={self.name}")
        loop = asyncio.get_running_loop()
        started = loop.time()
        while loop.time() - started < 900:
            state = await self.state()
            if not state:
                if failure := read(self.directory / "cleanup-error"):
                    raise ProviderStartError(f"Slurm cleanup failed in {self.directory}: {failure}")
                await _remove(self.directory)
                return
            # Each node's store is removed as the job ends, which takes about a minute for a
            # 2 GB image: only one that cleanup does not end in ten minutes is cancelled.
            if state == "PENDING" or (loop.time() - started > 600 and state != "COMPLETING"):
                await slurm("scancel", "--me", f"--name={self.name}")
            await asyncio.sleep(2)
        raise ProviderStartError(f"Slurm did not release job {self.name}")

    async def pending_reason(self) -> str:
        """Why Slurm keeps the job pending, such as ``Resources`` or ``Priority``."""
        return await slurm("squeue", "-h", "-j", self.id, "-o", "%r")

    async def time_left(self) -> int | None:
        """How many seconds Slurm still holds the running job for; None without a limit."""
        return seconds(await slurm("squeue", "-h", "-j", self.id, "-o", "%L"))

    async def ended(self) -> str:
        """How Slurm recorded the job's end, such as ``PREEMPTED`` or ``TIMEOUT``, if it
        did."""
        recorded = await slurm("sacct", "-nXj", self.id, "--format=State%32", "-P")
        return recorded.split()[0] if recorded.split() else ""

    async def state(self) -> str:
        """The job's state, as squeue names it; empty once it ended."""
        return await slurm("squeue", "-h", "--me", f"--name={self.name}", "-o", "%T")

    async def _alive(self) -> None:
        if not await self.state():
            raise ProviderStartError(
                f"Slurm job {self.id} ended: " + tail(self.directory / "job.log")
            )


async def _remove(directory: Path) -> None:
    """Remove a job's directory; a compute node may still be closing its NFS handle to the
    batch log, which leaves a silly-renamed file behind for a moment."""
    for _ in range(4):
        await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)
        if not directory.exists():
            return
        await asyncio.sleep(1)
    await asyncio.to_thread(shutil.rmtree, directory)


class SandboxSteps:
    """The control steps of one node of a Sandbox in a job."""

    def __init__(self, job: Job, index: str, node: str) -> None:
        self.job = job
        self.index = index
        self.node = node

    def step(self, mode: str, *args: str) -> list[str]:
        return self.job.step(mode, self.index, *args, nodes=(self.node,))

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
        # First: an OOM kill the Sandbox outlived is no reason it went now.
        with contextlib.suppress(ProviderStartError):
            if (ended := await job.ended()) in ENDED:
                return AllocationEndedError(
                    f"Slurm ended job {job.id} ({ended}): what the Sandbox held since its "
                    "last save written out is lost"
                )
        for watcher in self.watchers:
            if counted := watcher.oom_killed():
                return MemoryExhaustedError(f"Slurm job {job.id}: {counted} ({error})")
        box = job.directory / "sandboxes" / self.steps.index
        counters = [box / "nodes" / str(rank) / "memory" for rank in range(len(self.watchers))]
        with contextlib.suppress(ProviderStartError):
            for _ in range(5):
                if all(exists(each) for each in counters):
                    break
                await asyncio.sleep(1)
            state = await slurm(
                "sacct", "-nj", f"{job.id}.{self.ready.step}", "--format=State%32", "-P"
            )
            if "OUT_OF_MEMORY" in state or any(
                line.startswith("oom_kill ")
                and line.split()[-1].isdigit()
                and int(line.split()[-1]) > 0
                for each in counters
                for line in read(each).splitlines()
            ):
                return MemoryExhaustedError(
                    f"Slurm recorded an OOM kill in step {job.id}.{self.ready.step}"
                )
        return error
