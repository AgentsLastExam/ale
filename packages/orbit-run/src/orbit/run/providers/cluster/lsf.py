"""Spectrum LSF's jobs: ``bsub`` submits them, ``bjobs`` watches them and finds one by its
name, and ``bkill`` ends it (:mod:`delegated` holds what LSF shares with PBS)."""

from __future__ import annotations

import getpass
import json
import re

from orbit.core.config import LSFQueue
from orbit.core.errors import ProviderStartError
from orbit.run.providers.cluster.delegated import (
    DelegatedAllocation,
    DelegatedJob,
    Status,
    command,
)

#: What bjobs and bkill say of a job they no longer know.
MISSING = re.compile(r"job <[^>]+> is (?:not found|already finished)", re.I)
NO_JOBS = re.compile(r"no (?:unfinished |matching |unfinished and finished )?job(?:s)? found", re.I)


def status(job: str, listing: str) -> Status:
    """Job ``job``'s status in what ``bjobs -json`` listed of it."""
    try:
        (record,) = json.loads(listing)["RECORDS"]
        if MISSING.search(str(record.get("ERROR", ""))):
            return Status()
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
                return Status(ended=outcome)
        return Status(ended="COMPLETED" if state == "DONE" else "FAILED")
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
    return Status(active, reason=str(record.get("PEND_REASON") or state))


class LSFJob(DelegatedJob):
    partition: LSFQueue
    COMMANDS = ("bsub", "bjobs", "bqueues", "bkill")
    JOB_VARIABLE = "LSB_JOBID"

    @staticmethod
    def queue_command(queue: str) -> tuple[str, ...]:
        return ("bqueues", "-w", queue)

    def submission(
        self, *, cpus: int, memory_mb: int, gpus: int, minutes: int, log: str
    ) -> list[str]:
        # LSF otherwise sends the entire Host environment.
        argv = [
            "bsub",
            "-env",
            "none",
            "-rn",
            "-q",
            self.partition.name,
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
        return argv

    def submitted(self, out: str) -> str | None:
        match = re.search(r"^Job <([0-9]+)>", out, re.M)
        return match[1] if match else None

    async def find(self) -> str:
        out = await command(
            "bjobs",
            "-u",
            getpass.getuser(),
            "-J",
            self.name,
            "-noheader",
            "-o",
            "jobid",
            absent=NO_JOBS,
        )
        if out and not re.fullmatch(r"[0-9]+", out):
            raise ProviderStartError(f"cannot recover a unique job ID for {self.name}: {out!r}")
        return out

    async def status(self, *, history: bool) -> Status:
        listing = await command(
            "bjobs",
            "-a",
            "-o",
            "jobid stat exit_reason pend_reason",
            "-json",
            self.id,
            absent=MISSING,
        )
        return status(self.id, listing) if listing else Status()

    async def cancel(self) -> None:
        await command("bkill", self.id, absent=MISSING)


class LSFAllocation(DelegatedAllocation):
    JOB_VARIABLE = "LSB_JOBID"
    QUEUE_VARIABLE = "LSB_QUEUE"
