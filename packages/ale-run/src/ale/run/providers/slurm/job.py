"""Submit and drive one batch job, and the Sandboxes in it, from the machine ALE runs on: a
login node of the cluster, whose home every compute node shares."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import uuid
from pathlib import Path

from pydantic import BaseModel

from ale.core.config import SlurmPartition
from ale.core.errors import (
    AllocationEndedError,
    GuestUnreachableError,
    MemoryExhaustedError,
    ProviderStartError,
)
from ale.core.sandbox import GpuDevice
from ale.run.process import run
from ale.run.providers.memory import GuestEvents
from ale.run.transport import StdioTransport

#: Below the account's home, which the login node and every compute node share.
CACHE = ".cache/ale/slurm"
#: How Slurm records a job it ended itself: at its wall time, for a job of higher
#: priority, or as its node failed.
ENDED = {"TIMEOUT", "PREEMPTED", "NODE_FAIL", "DEADLINE"}
#: Below :data:`CACHE`: snapshots another allocation boots from, and the images prepared for
#: the cluster, each by its key.
SNAPSHOTS = "snapshots"
IMAGES = "images"


def cache() -> Path:
    return Path.home() / CACHE


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


class StepReady(BaseModel):
    image: str
    devices: tuple[GpuDevice, ...]
    step: str
    volumes: tuple[str, ...] = ()
    """The volume candidates the image has a directory at, which the Sandbox mounts."""


class Job:
    """One single-node batch job, whose batch step runs each Sandbox started in it as a step
    of its own."""

    def __init__(self, partition: SlurmPartition) -> None:
        self.partition = partition
        self.name = "ale-" + uuid.uuid4().hex
        self.directory = cache() / self.name
        self.id = ""

    @property
    def label(self) -> str:
        return self.id or self.name

    def stage(self, spec: dict[str, object], *, name: str = "allocation.json") -> None:
        """Give the job its directory, the worker and the worker's input, as ``name``."""
        cache().mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.mkdir(mode=0o700)
        (self.directory / "stop").touch()
        shutil.copyfile(Path(__file__).with_name("worker.py"), self.directory / "worker.py")
        write(self.directory / name, json.dumps(spec).encode())

    async def submit(
        self, *, cpus: int, memory_mb: int, gpus: int, minutes: int, command: str = "supervise"
    ) -> None:
        """Submit the job, whose batch step runs the worker's ``command``."""
        args = [
            "sbatch",
            "--parsable",
            "--no-requeue",
            "--nodes=1",
            "--ntasks=1",
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

    async def ready(self, queue_timeout: int) -> JobReady:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + queue_timeout
        started = False
        while loop.time() < deadline:
            if error := read(self.directory / "error"):
                raise ProviderStartError(f"Slurm job {self.id} could not start: {error}")
            if ready := read(self.directory / "ready"):
                return JobReady.model_validate_json(ready)
            state = await self.state()
            if state in {"RUNNING", "CONFIGURING"} and not started:
                started = True
                deadline = loop.time() + 1800
            if state not in {"PENDING", "RUNNING", "CONFIGURING"}:
                raise ProviderStartError(
                    f"Slurm job {self.id} ended before readiness ({state or 'gone'}): "
                    + tail(self.directory / "job.log")
                )
            await asyncio.sleep(2)
        raise ProviderStartError(
            f"Slurm job {self.id} timed out during {'startup' if started else 'queueing'}"
        )

    async def start(self, index: str, spec: dict[str, object], *, timeout: int) -> StepReady:
        """Start a Sandbox as a step of this job, once the batch step sees its request."""
        box = self.directory / "sandboxes" / index
        box.mkdir(mode=0o700, parents=True, exist_ok=True)
        write(box / "request.json", json.dumps(spec).encode())
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if error := read(box / "error"):
                # The step's own log holds what Podman said.
                raise ProviderStartError(
                    f"Slurm job {self.id} could not start Sandbox {index}: {error}\n"
                    + tail(box / "step.log")
                )
            if ready := read(box / "ready"):
                return StepReady.model_validate_json(ready)
            if exists(box / "done"):
                raise ProviderStartError(
                    f"Slurm job {self.id} ended Sandbox {index}'s step before readiness: "
                    + tail(box / "step.log")
                )
            await self._alive()
            await asyncio.sleep(1)
        raise ProviderStartError(f"Slurm job {self.id} did not start Sandbox {index}")

    async def save(
        self, index: str, *, timeout: int = 1800
    ) -> tuple[str, dict[str, dict[str, object]]]:
        """Stop a Sandbox and commit its disk to this job's store; return the image and each
        volume's stack of layers, by path."""
        box = self.directory / "sandboxes" / index
        (box / "stop").write_text("save")
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if saved := read(box / "saved"):
                record = json.loads(saved)
                return "sha256:" + str(record["image"]), dict(record.get("stacks") or {})
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

    async def control(self, *args: str, timeout: int = 60) -> None:
        code, _, error = await run(*self.step(*args), timeout=timeout)
        if code:
            raise ProviderStartError(f"job {self.id} could not {args[0]}: {error.strip()}")

    def step(self, *args: str) -> list[str]:
        return [
            "srun",
            "--unbuffered",
            "--overlap",
            "--nodes=1",
            "--ntasks=1",
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
            # The batch step removes the node's store, which takes about a minute for a
            # 2 GB image: only one that cleanup does not end in ten minutes is cancelled.
            if state == "PENDING" or (loop.time() - started > 600 and state != "COMPLETING"):
                await slurm("scancel", "--me", f"--name={self.name}")
            await asyncio.sleep(2)
        raise ProviderStartError(f"Slurm did not release job {self.name}")

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
    """The control steps of one Sandbox in a job."""

    def __init__(self, job: Job, index: str) -> None:
        self.job = job
        self.index = index

    def step(self, mode: str, *args: str) -> list[str]:
        return self.job.step(mode, self.index, *args)

    async def argv(self, remote_command: str) -> list[str]:
        return self.step("exec", "sh", "-c", remote_command)

    async def network(self, mode: str) -> None:
        await self.job.control(mode, self.index)


class JobTransport(StdioTransport):
    def __init__(
        self, argv: list[str], steps: SandboxSteps, ready: StepReady, watcher: GuestEvents
    ) -> None:
        super().__init__(argv)
        self.steps = steps
        self.ready = ready
        self.watcher = watcher

    async def explain(self, error: GuestUnreachableError) -> GuestUnreachableError:
        await self.watcher.settled(2)
        job = self.steps.job
        # First: an OOM kill the Sandbox outlived is no reason it went now.
        with contextlib.suppress(ProviderStartError):
            if (ended := await job.ended()) in ENDED:
                return AllocationEndedError(
                    f"Slurm ended job {job.id} ({ended}): what the Sandbox held since its "
                    "last save written out is lost"
                )
        if counted := self.watcher.oom_killed():
            return MemoryExhaustedError(f"Slurm job {self.steps.job.id}: {counted} ({error})")
        counters = job.directory / "sandboxes" / self.steps.index / "memory"
        with contextlib.suppress(ProviderStartError):
            for _ in range(5):
                if exists(counters):
                    break
                await asyncio.sleep(1)
            state = await slurm(
                "sacct", "-nj", f"{job.id}.{self.ready.step}", "--format=State%32", "-P"
            )
            if "OUT_OF_MEMORY" in state or any(
                line.startswith("oom_kill ")
                and line.split()[-1].isdigit()
                and int(line.split()[-1]) > 0
                for line in read(counters).splitlines()
            ):
                return MemoryExhaustedError(
                    f"Slurm recorded an OOM kill in step {job.id}.{self.ready.step}"
                )
        return error
