"""PBS Professional's jobs: ``qsub`` submits them, ``qstat`` watches them, ``qselect`` finds
one by its name and ``qdel`` ends it (:mod:`delegated` holds what PBS shares with LSF)."""

from __future__ import annotations

import getpass
import re

from orbit.core.config import PBSQueue
from orbit.core.errors import ProviderStartError
from orbit.run.providers.cluster.delegated import (
    DelegatedAllocation,
    DelegatedJob,
    Status,
    command,
)

ID = re.compile(r"[0-9]+(?:\.[a-zA-Z0-9][\w.-]*)?")
#: What qstat and qdel say of a job they no longer know.
MISSING = re.compile(r"unknown job id|job has (?:already )?finished", re.I)


def status(job: str, listing: str) -> Status:
    """Job ``job``'s status in what ``qstat -f`` listed of it."""
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
        return Status(ended="CANCELLED" if fields.get("substate") == "91" else outcome)
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
    return Status(active, reason=fields.get("comment", state))


class PBSJob(DelegatedJob):
    partition: PBSQueue
    COMMANDS = ("qsub", "qselect", "qstat", "qdel")
    JOB_VARIABLE = "PBS_JOBID"

    @staticmethod
    def queue_command(queue: str) -> tuple[str, ...]:
        return ("qstat", "-Q", queue)

    def submission(
        self, *, cpus: int, memory_mb: int, gpus: int, minutes: int, log: str
    ) -> list[str]:
        resources = {"ncpus": str(cpus), "mem": f"{memory_mb}mb", "ngpus": str(gpus)}
        select = ":".join(
            (
                "select=1",
                *(
                    f"{key}={resources[key]}"
                    for key in self.partition.chunk_resources
                    if key != "ngpus" or gpus
                ),
            )
        )
        argv = [
            "qsub",
            "-S",
            "/bin/sh",
            "-q",
            self.partition.name,
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
        return argv

    def submitted(self, out: str) -> str | None:
        return out if ID.fullmatch(out) else None

    async def find(self) -> str:
        out = await command("qselect", "-u", getpass.getuser(), "-N", self.name)
        if out and not ID.fullmatch(out):
            raise ProviderStartError(f"cannot recover a unique job ID for {self.name}: {out!r}")
        return out

    async def status(self, *, history: bool) -> Status:
        listing = await command(
            "qstat", *(("-x",) if history else ()), "-f", self.id, absent=MISSING
        )
        return status(self.id, listing) if listing else Status()

    async def cancel(self) -> None:
        await command("qdel", self.id, absent=MISSING)


class PBSAllocation(DelegatedAllocation):
    JOB_VARIABLE = "PBS_JOBID"
    QUEUE_VARIABLE = "PBS_QUEUE"
