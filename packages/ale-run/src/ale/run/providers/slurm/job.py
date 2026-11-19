"""Submit and drive one batch job through its login host."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import uuid
from pathlib import Path

from pydantic import BaseModel

from ale.core.config import SlurmPartition
from ale.core.errors import GuestUnreachableError, MemoryExhaustedError, ProviderStartError
from ale.core.sandbox import GpuDevice
from ale.run.process import run
from ale.run.providers.memory import GuestEvents
from ale.run.transport import StdioTransport

_CACHE = ".cache/ale/slurm"


async def config_digest(image: str) -> str:
    """The digest of a Docker image's config, by which Podman names the image it loads from
    ``docker image save``: Docker's classic store names it so too, and the containerd store
    by its manifest's digest, recording the config's beside it."""
    code, out, err = await run("docker", "image", "inspect", image, timeout=60)
    if code:
        raise ProviderStartError(f"could not inspect {image}: {err.strip()}")
    (record,) = json.loads(out)
    annotations = (record.get("Descriptor") or {}).get("annotations") or {}
    digest = annotations.get("config.digest") or record.get("Id")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ProviderStartError(f"Docker reports no config digest for {image}")
    return digest


class JobReady(BaseModel):
    image: str
    runtime: str
    node: str
    devices: tuple[GpuDevice, ...]


class Job:
    def __init__(self, partition: SlurmPartition) -> None:
        self.partition = partition
        self.name = "ale-" + uuid.uuid4().hex
        self.directory = f"{_CACHE}/{self.name}"
        self.id = ""
        self.ssh = [
            "ssh",
            "-T",
            "-oBatchMode=yes",
            "-oForwardAgent=no",
            "-oClearAllForwardings=yes",
            "-oConnectTimeout=15",
            "-oServerAliveInterval=15",
            "--",
            partition.host,
        ]

    async def remote(self, command: str, *, stdin: bytes | None = None, timeout: int = 60) -> str:
        code, out, err = await run(*self.ssh, command, stdin=stdin, timeout=timeout)
        if code:
            raise ProviderStartError(
                f"Slurm {self.partition.host}/{self.id or self.name}: {err.strip() or out.strip()}"
            )
        return out.strip()

    async def stage(self, image: str, spec: dict[str, object]) -> None:
        await self.remote(
            f"umask 077 && mkdir -p {_CACHE} && mkdir {self.directory} "
            f"&& touch {self.directory}/stop && cat > {self.directory}/worker.py",
            stdin=Path(__file__).with_name("worker.py").read_bytes(),
        )
        await self.remote(
            f"umask 077 && cat > {self.directory}/request.json", stdin=json.dumps(spec).encode()
        )
        archive = f"{_CACHE}/{image.removeprefix('sha256:')}.tar.gz"
        if await self.remote(f"test -s {archive} && echo cached || true"):
            return
        # Only publish after both Docker and SSH finished successfully. A concurrent upload
        # of the same immutable image can replace the archive without changing its contents.
        temporary = self.directory + "/image.part"
        send = shlex.join([*self.ssh, f"umask 077 && cat > {temporary}"])
        pipeline = f"docker image save {shlex.quote(image)} | gzip -1 | {send}"
        code, _, err = await run("bash", "-o", "pipefail", "-c", pipeline, timeout=3600)
        if code:
            raise ProviderStartError(
                f"could not stage {image} on {self.partition.host}: {err.strip()}"
            )
        await self.remote(f"mv {temporary} {archive}")

    async def submit(self, *, cpus: int, memory_mb: int, gpus: int, minutes: int) -> None:
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
            "--output=job.log",
        ]
        if self.partition.qos:
            args.append("--qos=" + self.partition.qos)
        if gpus:
            args.append(f"--gres=gpu:{gpus}")
        args.append("--wrap=exec python3 -I worker.py supervise")
        # Keep the reply on the shared filesystem even if the submission channel drops.
        submit = f"test ! -s stop && {shlex.join(args)} > job.id && cat job.id"
        out = await self.remote(
            f"cd {self.directory} && flock submission sh -c {shlex.quote(submit)}"
        )
        self.id = out.partition(";")[0]
        if not self.id.isdecimal():
            raise ProviderStartError(f"sbatch returned an invalid job id: {out!r}")

    async def ready(self, queue_timeout: int) -> JobReady:
        deadline = asyncio.get_running_loop().time() + queue_timeout
        started = False
        while asyncio.get_running_loop().time() < deadline:
            reply = await self.remote(
                f"cd {self.directory} && if test -f error; then cat error >&2; exit 1; "
                f"elif test -f ready; then cat ready; "
                f"else squeue -h --me --name={self.name} -o %T; fi"
            )
            if reply.startswith("{"):
                return JobReady.model_validate_json(reply)
            if reply in {"RUNNING", "CONFIGURING"} and not started:
                started = True
                deadline = asyncio.get_running_loop().time() + 1800
            if reply not in {"PENDING", "RUNNING", "CONFIGURING"}:
                log = await self.remote(
                    f"cd {self.directory} && tail -c 2000 job.log 2>/dev/null || true"
                )
                raise ProviderStartError(
                    f"Slurm job {self.id} ended before readiness ({reply or 'gone'}): {log}"
                )
            await asyncio.sleep(2)
        raise ProviderStartError(
            f"Slurm job {self.id} timed out during {'startup' if started else 'queueing'}"
        )

    def step(self, *args: str) -> list[str]:
        remote = shlex.join(
            [
                "srun",
                "--unbuffered",
                "--overlap",
                "--nodes=1",
                "--ntasks=1",
                "--jobid=" + self.id,
                "python3",
                "-I",
                "worker.py",
                *args,
            ]
        )
        return [*self.ssh, f"cd {self.directory} && exec {remote}"]

    async def argv(self, remote_command: str) -> list[str]:
        return self.step("exec", "sh", "-c", remote_command)

    async def network(self, mode: str) -> None:
        code, _, error = await run(*self.step(mode), timeout=60)
        if code:
            raise ProviderStartError(f"job {self.id} could not apply {mode} egress: {error}")

    async def stop(self) -> None:
        # Look up by the unique name even if cancellation interrupted sbatch's reply.
        await self.remote(
            f"if cd {self.directory} 2>/dev/null; then "
            "flock submission sh -c 'echo stop > stop'; fi"
        )
        for attempt in range(120):
            state = await self.remote(f"squeue -h --me --name={self.name} -o %T")
            if not state:
                failure = await self.remote(
                    f"cat {self.directory}/cleanup-error 2>/dev/null || true"
                )
                if failure:
                    raise ProviderStartError(f"Slurm cleanup failed in {self.directory}: {failure}")
                # A compute node may still be closing its NFS handle to the batch log.
                await self.remote(
                    f"for i in 1 2 3 4; do rm -rf -- {self.directory} && exit 0; sleep 1; done; "
                    f"rm -rf -- {self.directory}"
                )
                return
            if state == "PENDING" or (attempt == 60 and state != "COMPLETING"):
                await self.remote(f"scancel --me --name={self.name}")
            await asyncio.sleep(1)
        raise ProviderStartError(f"Slurm did not release job {self.partition.host}/{self.name}")


class JobTransport(StdioTransport):
    def __init__(self, argv: list[str], job: Job, watcher: GuestEvents) -> None:
        super().__init__(argv)
        self.job = job
        self.watcher = watcher

    async def explain(self, error: GuestUnreachableError) -> GuestUnreachableError:
        await self.watcher.settled(2)
        if counted := self.watcher.oom_killed():
            return MemoryExhaustedError(f"Slurm job {self.job.id}: {counted} ({error})")
        with contextlib.suppress(ProviderStartError):
            evidence = await self.job.remote(
                f"cd {self.job.directory} && for i in 1 2 3 4 5; do "
                f"if test -f memory; then cat memory; break; fi; sleep 1; done; "
                f"sacct -nj {self.job.id} --format=State%32 -P"
            )
            if "OUT_OF_MEMORY" in evidence or any(
                line.startswith("oom_kill ")
                and line.split()[-1].isdigit()
                and int(line.split()[-1]) > 0
                for line in evidence.splitlines()
            ):
                return MemoryExhaustedError(f"Slurm recorded an OOM kill in job {self.job.id}")
        return error
