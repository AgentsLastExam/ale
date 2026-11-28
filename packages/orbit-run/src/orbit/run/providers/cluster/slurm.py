"""Slurm's jobs: ``sbatch`` submits them, ``squeue`` and ``sacct`` watch them and ``scancel``
ends them, by their unique names; in the job Orbit runs in, each Sandbox is a step of the
job, which Slurm holds to its share, and Orbit reaches the job's other nodes by steps that
overlap it."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import socket
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime

from orbit.core.config import (
    ClusterPartition,
    ClusterProviderConfig,
    SlurmPartition,
    SlurmProviderConfig,
)
from orbit.core.errors import ProviderStartError
from orbit.run.process import run
from orbit.run.providers.cluster.job import (
    Allocation,
    Job,
    command_line,
    read,
    remove_job_directory,
    unset_credentials,
    without_credentials,
)


async def slurm(*argv: str, timeout: float = 60) -> str:
    """Run one of Slurm's commands here; its output, or why it failed."""
    try:
        code, out, err = await run(*argv, timeout=timeout, env=without_credentials())
    except FileNotFoundError:
        raise ProviderStartError(f"Slurm's {argv[0]} is not installed here") from None
    if code:
        raise ProviderStartError(f"Slurm {argv[0]}: {err.strip() or out.strip()}")
    return out.strip()


class SlurmJob(Job):
    """A Slurm job of one or more alike nodes."""

    config: SlurmProviderConfig
    partition: SlurmPartition
    #: A Sandbox spans up to its partition's ``max_nodes``.
    spans_nodes = True

    @classmethod
    async def preflight(
        cls, config: ClusterProviderConfig, partitions: Sequence[ClusterPartition]
    ) -> None:
        """Check that the login node runs Slurm's commands and Slurm knows ``partitions``."""
        for command in ("sbatch", "squeue", "scancel", "sacct", "scontrol"):
            if shutil.which(command) is None:
                raise ProviderStartError(
                    f"Slurm's {command} is not installed here: Orbit submits each episode's "
                    "job from a login node of the cluster, which shares its cache with the "
                    "compute nodes"
                )
        for part in dict.fromkeys(part.name for part in partitions):
            await slurm("scontrol", "show", "partition", part, "-o")

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
        first node, never again once it ends."""
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
            # The job runs in its directory (command_line), its output in its log. Slurm's
            # working directory and output file for it, which it fills no further, are the
            # node's /tmp, beside which a site's epilog may leave files of root's, as
            # OpenRSI's GPU statistics beside the output file, which in the job's directory
            # would outlive it.
            "--chdir=/tmp",
            "--output=/tmp/orbit-%u.out",
            # What Slurm itself says of the job, such as why it ended, beside what it runs.
            "--error=" + str(self.directory / "job.log").replace("%", "%%"),
            "--open-mode=append",
        ]
        if self.partition.qos:
            args.append("--qos=" + self.partition.qos)
        if self.config.account:
            args.append("--account=" + self.config.account)
        if self.partition.constraint:
            args.append("--constraint=" + self.partition.constraint)
        if gpus:
            args.append(f"--gres=gpu:{gpus}")
        args.append("--wrap=" + command_line(self.directory, argv))
        # Not once the job is stopped, which a cancelled submission may race.
        submit = f"test ! -s stop && {shlex.join(args)}"
        out = await slurm(
            "flock",
            str(self.directory / "submission"),
            "sh",
            "-c",
            f"cd {shlex.quote(str(self.directory))} && {submit}",
        )
        self.id = out.partition(";")[0]
        if not self.id.isdecimal():
            raise ProviderStartError(f"sbatch returned an invalid job id: {out!r}")

    async def stop(self, *, cancel: bool = False) -> None:
        """End the job, by its own cleanup, or at once where ``cancel`` asks, as for a job
        that reads no ``stop``, and remove its directory."""
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
                await remove_job_directory(self.directory)
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

    async def ended(self) -> str:
        """How Slurm recorded the job's end, such as ``PREEMPTED`` or ``TIMEOUT``, if it
        did."""
        recorded = await slurm("sacct", "-nXj", self.id, "--format=State%32", "-P")
        return recorded.split()[0] if recorded.split() else ""

    async def state(self) -> str:
        """The job's state, as squeue names it; empty once it ended."""
        return await slurm("squeue", "-h", "--me", f"--name={self.name}", "-o", "%T")


class SlurmAllocation(Allocation):
    """The Slurm job Orbit runs in: the worker starts each Sandbox as a step of it over its
    first nodes (``srun --exact``), which Slurm holds to the Sandbox's resources on each."""

    JOB_VARIABLE = "SLURM_JOB_ID"
    QUEUE_VARIABLE = "SLURM_JOB_PARTITION"

    @classmethod
    def held(
        cls, partition: ClusterPartition, job: str, *, cpus: int, memory_mb: int, gpus: int
    ) -> tuple[tuple[str, ...], int, int, int]:
        nodes = _hostnames(os.environ.get("SLURM_JOB_NODELIST", ""))
        here = os.environ.get("SLURMD_NODENAME", "")
        if not nodes or here != nodes[0]:
            raise ProviderStartError(
                f"Orbit runs on {here or socket.gethostname()}, not on the first node of "
                f"Slurm job {job} ({', '.join(nodes)}), as a job's own script does"
            )
        return nodes, cpus, memory_mb, gpus

    @classmethod
    def ends_at(cls, started_at: datetime, walltime_minutes: int) -> datetime:
        """The end Slurm states as the job starts, where it does."""
        stamp = os.environ.get("SLURM_JOB_END_TIME", "")
        if stamp.isdecimal() and int(stamp) > 0:
            return datetime.fromtimestamp(int(stamp), UTC)
        return super().ends_at(started_at, walltime_minutes)

    @staticmethod
    def check(node: str) -> None:
        """Check that ``node``, the job's first, runs the steps that start its Sandboxes and
        reach its other nodes."""
        for command in ("srun", "scontrol"):
            if shutil.which(command) is None:
                raise ProviderStartError(f"Slurm's {command} is not installed on {node}")

    def remote_step(self, *args: str, node: str) -> list[str]:
        """An unbuffered step that overlaps the job's others, as a session's binary stream
        needs."""
        return [
            "env",
            *unset_credentials(),
            "srun",
            "--unbuffered",
            "--overlap",
            "--nodes=1",
            "--ntasks=1",
            "--nodelist=" + node,
            "--jobid=" + self.id,
            f"--chdir={self.directory}",
            "python3",
            "-I",
            "worker.py",
            *args,
        ]

    async def step_accounting_state(self, step: str) -> str:
        return await slurm("sacct", "-nj", f"{self.id}.{step}", "--format=State%32", "-P")


def _hostnames(nodelist: str) -> tuple[str, ...]:
    """Slurm's names of the nodes ``nodelist`` names, in its order."""
    try:
        listed = subprocess.run(
            ["scontrol", "show", "hostnames", nodelist],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            env=without_credentials(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProviderStartError(
            f"Slurm's scontrol could not list the job's nodes: {error}"
        ) from error
    return tuple(listed.stdout.split())
