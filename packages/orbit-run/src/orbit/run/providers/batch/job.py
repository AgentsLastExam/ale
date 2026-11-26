"""PBS Professional and LSF batch allocations driven from a cluster login node."""

from __future__ import annotations

import asyncio
import getpass
import json
import re
import shlex
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orbit.core.config import BatchProviderConfig, BatchQueue
from orbit.core.errors import ProviderStartError
from orbit.run.images import acquire_lock
from orbit.run.process import run
from orbit.run.providers.slurm.job import Job, read, remove_job_directory, seconds, write

_PBS_ID = re.compile(r"[0-9]+(?:\.[a-zA-Z0-9][\w.-]*)?")
_MISSING = re.compile(
    r"unknown job id|job has (?:already )?finished|job <[^>]+> is (?:not found|already finished)",
    re.I,
)
_NO_JOBS = re.compile(
    r"no (?:unfinished |matching |unfinished and finished )?job(?:s)? found", re.I
)


async def _command(*args: str, stdin: bytes | None = None, absent: re.Pattern | None = None) -> str:
    try:
        code, out, err = await run(*args, stdin=stdin, timeout=60)
    except FileNotFoundError:
        raise ProviderStartError(f"{args[0]} is not installed on the login node") from None
    if code:
        detail = err.strip() or out.strip()
        if absent and absent.search(detail):
            return ""
        raise ProviderStartError(f"{args[0]} failed ({code}): {detail}")
    return out.strip()


async def preflight(config: BatchProviderConfig, partitions: Sequence[BatchQueue]) -> None:
    pbs = config.scheduler == "pbs"
    commands = (
        ("qsub", "qselect", "qstat", "qdel") if pbs else ("bsub", "bjobs", "bqueues", "bkill")
    )
    for command in commands:
        if not shutil.which(command):
            raise ProviderStartError(f"{config.scheduler} needs {command} on the login node")
    for name in dict.fromkeys(queue.name for queue in partitions):
        await _command(*(("qstat", "-Q", name) if pbs else ("bqueues", "-w", name)))


@dataclass(frozen=True)
class _Status:
    state: str = ""
    ended: str = ""
    reason: str = ""
    left: int | None = None


def _pbs_status(job: str, listing: str) -> _Status:
    header, _, attributes = listing.partition("\n")
    full_id = header.removeprefix("Job Id:").strip()
    if not header.startswith("Job Id:") or not (
        full_id == job or ("." not in job and full_id.startswith(job + "."))
    ):
        raise ProviderStartError(f"qstat returned no record for job {job}")
    fields = dict(re.findall(r"^    ([\w.]+) = (.*)$", attributes, re.M))
    state = fields.get("job_state", "")
    if state in {"F", "X"}:
        outcome = {
            "-29": "TIMEOUT",
            "-27": "OUT_OF_MEMORY",
            "-26": "OUT_OF_MEMORY",
            "0": "COMPLETED",
        }.get(fields.get("Exit_status"), "FAILED")
        return _Status(ended="CANCELLED" if fields.get("substate") == "91" else outcome)
    active = {
        "B": "RUNNING",
        "Q": "PENDING",
        "H": "PENDING",
        "W": "PENDING",
        "T": "PENDING",
        "M": "PENDING",
        "R": "RUNNING",
        "S": "RUNNING",
        "U": "RUNNING",
        "E": "COMPLETING",
    }.get(state)
    if not active:
        raise ProviderStartError(f"qstat returned an unknown state {state!r} for job {job}")
    left = None
    if active == "RUNNING" and (limit := fields.get("Resource_List.walltime")):
        try:
            elapsed = seconds(fields.get("resources_used.walltime", ""))
            if started := fields.get("stime"):
                stamp = (
                    int(started)
                    if started.isdecimal()
                    else datetime.strptime(started, "%a %b %d %H:%M:%S %Y").timestamp()
                )
                elapsed = max(elapsed or 0, int(time.time() - stamp))
            if elapsed is not None:
                duration = seconds(limit)
                if duration is not None:
                    left = max(0, duration - elapsed)
        except (ValueError, OverflowError) as exc:
            raise ProviderStartError(f"qstat returned an invalid wall time for job {job}") from exc
    return _Status(active, reason=fields.get("comment", state), left=left)


def _lsf_status(job: str, listing: str) -> _Status:
    try:
        (record,) = json.loads(listing)["RECORDS"]
        if _MISSING.search(str(record.get("ERROR", ""))):
            return _Status()
        if str(record["JOBID"]) != job:
            raise ValueError
        state = record["STAT"]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProviderStartError(f"bjobs returned no valid record for job {job}") from exc
    if state in {"DONE", "EXIT"}:
        reason = str(record.get("EXIT_REASON", ""))
        for word, outcome in (
            ("TERM_RUNLIMIT", "TIMEOUT"),
            ("TERM_PREEMPT", "PREEMPTED"),
            ("TERM_MEMLIMIT", "OUT_OF_MEMORY"),
            ("TERM_OWNER", "CANCELLED"),
            ("TERM_ADMIN", "CANCELLED"),
        ):
            if word in reason:
                return _Status(ended=outcome)
        return _Status(ended="COMPLETED" if state == "DONE" else "FAILED")
    active = {
        "PEND": "PENDING",
        "PSUSP": "PENDING",
        "WAIT": "PENDING",
        "PROV": "CONFIGURING",
        "RUN": "RUNNING",
        "USUSP": "RUNNING",
        "SSUSP": "RUNNING",
    }.get(state)
    if not active:
        raise ProviderStartError(f"bjobs returned state {state!r} for job {job}")
    left = None
    if match := re.fullmatch(
        r"(\d+):(\d{1,2})(?:\s+[ELX])?", str(record.get("TIME_LEFT", "")).strip()
    ):
        left = 3600 * int(match[1]) + 60 * int(match[2])
    return _Status(active, reason=str(record.get("PEND_REASON") or state), left=left)


class BatchJob(Job):
    """A single-node batch allocation with a private scheduler job ID and control directory."""

    def __init__(self, config: BatchProviderConfig, partition: BatchQueue) -> None:
        super().__init__(config, partition)
        self.config = config
        self.partition = partition

    def stage(self, spec: dict[str, object], *, name: str = "allocation.json") -> None:
        super().stage({**spec, "scheduler": self.config.scheduler}, name=name)
        here = Path(__file__).parent
        shutil.copyfile(here / "bridge.py", self.directory / "bridge.py")
        shutil.copyfile(here / "worker.py", self.directory / "batch_worker.py")

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
        if nodes != 1:
            raise ProviderStartError(f"{self.config.scheduler} needs a single-node allocation")
        pbs = self.config.scheduler == "pbs"
        queue = self.partition
        gpus = max(queue.min_gpus, gpus)
        spec = {
            "scheduler": self.config.scheduler,
            "podman": self.config.podman,
            "gpus": queue.max_gpus if pbs and "ngpus" not in queue.chunk_resources else gpus,
            "cpus": queue.max_cpus if pbs and "ncpus" not in queue.chunk_resources else cpus,
            "memory_mb": queue.max_memory_mb
            if pbs and "mem" not in queue.chunk_resources
            else memory_mb,
        }
        script = (
            "#!/bin/sh\n"
            + f"cd {shlex.quote(str(self.directory))} || exit 1\n"
            + "test ! -s stop || exit 0\n"
            + f"exec python3 -I worker.py {shlex.quote(command)} >>job.log 2>&1\n"
        ).encode()
        log = str(self.directory / "batch.log")
        if pbs:
            resources = {"ncpus": str(cpus), "mem": f"{memory_mb}mb", "ngpus": str(gpus)}
            select = ":".join(
                (
                    "select=1",
                    *(
                        f"{key}={resources[key]}"
                        for key in queue.chunk_resources
                        if key != "ngpus" or gpus
                    ),
                )
            )
            argv = [
                "qsub",
                "-S",
                "/bin/sh",
                "-q",
                queue.name,
                "-N",
                self.name,
                "-r",
                "n",
                "-j",
                "oe",
                "-o",
                log,
                "-l",
                select,
                "-l",
                f"walltime={minutes // 60:02d}:{minutes % 60:02d}:00",
            ]
            if self.config.account:
                argv.extend(("-A", self.config.account))
        else:
            # LSF otherwise sends the entire Host environment, including credentials.
            argv = [
                "bsub",
                "-env",
                "none",
                "-rn",
                "-q",
                queue.name,
                "-J",
                self.name,
                "-o",
                log,
                "-n",
                str(cpus),
                "-W",
                f"{minutes // 60}:{minutes % 60:02d}",
                "-M",
                f"{memory_mb}M",
                "-R",
                f"span[hosts=1] rusage[mem={memory_mb}MB/host]",
            ]
            if gpus:
                argv.extend(("-gpu", f"num={gpus}:mode=shared:j_exclusive=yes"))
            if self.config.account:
                argv.extend(("-P", self.config.account))
        with (self.directory / "submission").open("a") as lock:
            await acquire_lock(lock)
            if read(self.directory / "stop") or read(self.directory / "submitting"):
                raise ProviderStartError(f"job {self.name} was stopped or already submitted")
            write(self.directory / "batch.json", json.dumps(spec).encode())
            write(self.directory / "submitting", b"1")
            # qsub and bsub both accept a job script on stdin. Finish recording the ID
            # before cancellation can release the lock to cleanup.
            pending = asyncio.create_task(self._submit(argv, script))
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise

    async def _submit(self, argv: list[str], script: bytes) -> None:
        out = await _command(*argv, stdin=script)
        match = (
            _PBS_ID.fullmatch(out)
            if self.config.scheduler == "pbs"
            else re.search(r"^Job <([0-9]+)>", out, re.M)
        )
        if not match:
            raise ProviderStartError(f"{argv[0]} returned an invalid job ID: {out!r}")
        self.id = match[0] if self.config.scheduler == "pbs" else match[1]
        write(self.directory / "job-id", self.id.encode())

    async def _recover_id(self) -> str:
        if job := read(self.directory / "job-id"):
            return job
        if not read(self.directory / "submitting"):
            return ""
        if self.config.scheduler == "pbs":
            out = await _command("qselect", "-u", getpass.getuser(), "-N", self.name)
            valid = _PBS_ID
        else:
            out = await _command(
                "bjobs",
                "-u",
                getpass.getuser(),
                "-J",
                self.name,
                "-noheader",
                "-o",
                "jobid",
                absent=_NO_JOBS,
            )
            valid = re.compile(r"[0-9]+")
        if out and not valid.fullmatch(out):
            raise ProviderStartError(f"cannot recover a unique job ID for {self.name}: {out!r}")
        if out:
            write(self.directory / "job-id", out.encode())
        return out

    async def _status(self, *, history: bool = False) -> _Status:
        if not self.id:
            return _Status()
        if self.config.scheduler == "pbs":
            listing = await _command(
                "qstat", *(("-x",) if history else ()), "-f", self.id, absent=_MISSING
            )
            return _pbs_status(self.id, listing) if listing else _Status()
        listing = await _command(
            "bjobs",
            "-a",
            "-o",
            "jobid stat time_left exit_reason pend_reason",
            "-json",
            self.id,
            absent=_MISSING,
        )
        return _lsf_status(self.id, listing) if listing else _Status()

    async def state(self) -> str:
        return (await self._status()).state

    async def ended(self) -> str:
        return (await self._status(history=True)).ended

    async def step_accounting_state(self, step: str) -> str:
        return await self.ended()

    async def pending_reason(self) -> str:
        return (await self._status()).reason

    async def time_left(self) -> int:
        status = await self._status()
        if status.left is None:
            raise ProviderStartError(
                f"{self.config.scheduler} did not report a remaining wall time for job {self.id}"
            )
        return status.left

    def step(self, *args: str, nodes: Sequence[str]) -> list[str]:
        if len(nodes) != 1:
            raise ProviderStartError(f"{self.config.scheduler} control requires one allocated node")
        return [
            sys.executable,
            "-I",
            str(self.directory / "bridge.py"),
            "connect",
            str(self.directory),
            *args,
        ]

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
            await self._cancel()
        started = asyncio.get_running_loop().time()
        while self.id and (status := await self.state()):
            age = asyncio.get_running_loop().time() - started
            if age > 900:
                raise ProviderStartError(f"{self.config.scheduler} did not release job {self.id}")
            if not cancel and (status == "PENDING" or (age > 600 and status != "COMPLETING")):
                await self._cancel()
                cancel = True
            await asyncio.sleep(2)
        if failure := read(self.directory / "cleanup-error"):
            raise ProviderStartError(f"job {self.id} cleanup failed in {self.directory}: {failure}")
        await remove_job_directory(self.directory)

    async def _cancel(self) -> None:
        await _command(
            "qdel" if self.config.scheduler == "pbs" else "bkill", self.id, absent=_MISSING
        )
