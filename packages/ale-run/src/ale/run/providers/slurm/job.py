"""Submit and drive one batch job, and the Sandboxes in it, through its login host."""

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

#: Below the submitting account's home, which the login host and every compute node share.
CACHE = ".cache/ale/slurm"
#: Snapshots another allocation boots from, below :data:`CACHE`.
SNAPSHOTS = "snapshots"


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
    runtime: str
    node: str


class StepReady(BaseModel):
    image: str
    devices: tuple[GpuDevice, ...]
    step: str


class Login:
    """The submission host of one partition, reached over SSH."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.ssh = [
            "ssh",
            "-T",
            "-oBatchMode=yes",
            "-oForwardAgent=no",
            "-oClearAllForwardings=yes",
            "-oConnectTimeout=15",
            "-oServerAliveInterval=15",
            "--",
            host,
        ]

    @property
    def label(self) -> str:
        return self.host

    async def remote(self, command: str, *, stdin: bytes | None = None, timeout: int = 60) -> str:
        code, out, err = await run(*self.ssh, command, stdin=stdin, timeout=timeout)
        if code:
            raise ProviderStartError(f"Slurm {self.label}: {err.strip() or out.strip()}")
        return out.strip()


class Job(Login):
    """One single-node batch job, whose batch step runs each Sandbox started in it as a step
    of its own."""

    def __init__(self, partition: SlurmPartition) -> None:
        super().__init__(partition.host)
        self.partition = partition
        self.name = "ale-" + uuid.uuid4().hex
        self.directory = f"{CACHE}/{self.name}"
        self.id = ""

    @property
    def label(self) -> str:
        return f"{self.host}/{self.id or self.name}"

    async def stage_image(self, image: str) -> tuple[str, str]:
        """Send a Task image to the shared cache once; return its archive's name there and
        the ID Podman gives it once loaded."""
        loaded = await config_digest(image)
        archive = f"{image.removeprefix('sha256:')}.tar.gz"
        if await self.remote(f"test -s {CACHE}/{archive} && echo cached || true"):
            return archive, loaded
        await self.remote(f"umask 077 && mkdir -p {CACHE}")
        # Only publish after both Docker and SSH finished successfully. A concurrent upload
        # of the same immutable image can replace the archive without changing its contents.
        temporary = f"{CACHE}/{self.name}.image.part"
        send = shlex.join([*self.ssh, f"umask 077 && cat > {temporary}"])
        pipeline = f"docker image save {shlex.quote(image)} | gzip -1 | {send}"
        code, _, err = await run("bash", "-o", "pipefail", "-c", pipeline, timeout=3600)
        if code:
            await self.remote(f"rm -f {temporary}")
            raise ProviderStartError(f"could not stage {image} on {self.host}: {err.strip()}")
        await self.remote(f"mv {temporary} {CACHE}/{archive}")
        return archive, loaded

    async def stage(self, spec: dict[str, object]) -> None:
        await self.remote(
            f"umask 077 && mkdir -p {CACHE} && mkdir {self.directory} "
            f"&& touch {self.directory}/stop && cat > {self.directory}/worker.py",
            stdin=Path(__file__).with_name("worker.py").read_bytes(),
        )
        await self.remote(
            f"umask 077 && cat > {self.directory}/allocation.json",
            stdin=json.dumps(spec).encode(),
        )

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
                raise ProviderStartError(
                    f"Slurm job {self.id} ended before readiness ({reply or 'gone'}): "
                    + await self._log("job.log")
                )
            await asyncio.sleep(2)
        raise ProviderStartError(
            f"Slurm job {self.id} timed out during {'startup' if started else 'queueing'}"
        )

    async def start(self, index: str, spec: dict[str, object], *, timeout: int) -> StepReady:
        """Start a Sandbox as a step of this job, once the batch step sees its request."""
        box = f"{self.directory}/sandboxes/{index}"
        await self.remote(
            f"umask 077 && mkdir -p {box} && cat > {box}/request.tmp "
            f"&& mv {box}/request.tmp {box}/request.json",
            stdin=json.dumps(spec).encode(),
        )
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            reply = await self.remote(
                # The step's own log holds what Podman said.
                f"cd {box} && if test -f error; then cat error >&2; tail -c 2000 step.log >&2; "
                "exit 1; elif test -f ready; then cat ready; elif test -f done; then echo done; fi"
            )
            if reply.startswith("{"):
                return StepReady.model_validate_json(reply)
            if reply == "done":
                raise ProviderStartError(
                    f"Slurm job {self.id} ended Sandbox {index}'s step before readiness: "
                    + await self._log(f"sandboxes/{index}/step.log")
                )
            await self._alive()
            await asyncio.sleep(1)
        raise ProviderStartError(f"Slurm job {self.id} did not start Sandbox {index}")

    async def save(
        self, index: str, *, timeout: int = 1800
    ) -> tuple[str, dict[str, dict[str, object]]]:
        """Stop a Sandbox and commit its disk to this job's store; return the image and each
        volume's stack of layers, by path."""
        box = f"{self.directory}/sandboxes/{index}"
        await self.remote(f"echo save > {box}/stop")
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            reply = await self.remote(
                f"cd {box} && if test -f saved; then cat saved; "
                "elif test -f error; then cat error >&2; exit 1; "
                "elif test -f done; then echo done; fi"
            )
            if reply.startswith("{"):
                saved = json.loads(reply)
                return "sha256:" + str(saved["image"]), dict(saved.get("stacks") or {})
            if reply == "done":
                raise ProviderStartError(f"Sandbox {index} ended before its disk was saved")
            await self._alive()
            await asyncio.sleep(1)
        raise ProviderStartError(f"Slurm job {self.id} did not save Sandbox {index} in time")

    async def end(self, index: str, *, timeout: int = 180) -> None:
        """End a Sandbox's step, idempotently, and wait until Slurm has released it."""
        box = f"{self.directory}/sandboxes/{index}"
        await self.remote(
            f"if test -d {box} && ! test -s {box}/stop; then echo destroy > {box}/stop; fi"
        )
        for _ in range(timeout):
            if await self.remote(
                f"if ! test -f {box}/request.json || test -f {box}/done; then echo done; fi"
            ):
                return
            if not await self._state():
                return
            await asyncio.sleep(1)
        raise ProviderStartError(f"Slurm job {self.id} did not end Sandbox {index}'s step")

    async def control(self, *args: str, timeout: int = 60) -> None:
        code, _, error = await run(*self.step(*args), timeout=timeout)
        if code:
            raise ProviderStartError(f"job {self.id} could not {args[0]}: {error.strip()}")

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

    async def stop(self) -> None:
        # Look up by the unique name even if cancellation interrupted sbatch's reply.
        await self.remote(
            f"if cd {self.directory} 2>/dev/null; then "
            "flock submission sh -c 'echo stop > stop'; fi"
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        while loop.time() - started < 900:
            state = await self._state()
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
            # The batch step removes the node's store, which takes about a minute for a
            # 2 GB image: only one that cleanup does not end in ten minutes is cancelled.
            if state == "PENDING" or (loop.time() - started > 600 and state != "COMPLETING"):
                await self.remote(f"scancel --me --name={self.name}")
            await asyncio.sleep(2)
        raise ProviderStartError(f"Slurm did not release job {self.host}/{self.name}")

    async def _state(self) -> str:
        return await self.remote(f"squeue -h --me --name={self.name} -o %T")

    async def _alive(self) -> None:
        if not await self._state():
            raise ProviderStartError(f"Slurm job {self.id} ended: " + await self._log("job.log"))

    async def _log(self, name: str) -> str:
        return await self.remote(f"cd {self.directory} && tail -c 2000 {name} 2>/dev/null || true")


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
        if counted := self.watcher.oom_killed():
            return MemoryExhaustedError(f"Slurm job {self.steps.job.id}: {counted} ({error})")
        job, box = self.steps.job, f"sandboxes/{self.steps.index}"
        with contextlib.suppress(ProviderStartError):
            evidence = await job.remote(
                f"cd {job.directory} && for i in 1 2 3 4 5; do "
                f"if test -f {box}/memory; then cat {box}/memory; break; fi; sleep 1; done; "
                f"sacct -nj {job.id}.{self.ready.step} --format=State%32 -P"
            )
            if "OUT_OF_MEMORY" in evidence or any(
                line.startswith("oom_kill ")
                and line.split()[-1].isdigit()
                and int(line.split()[-1]) > 0
                for line in evidence.splitlines()
            ):
                return MemoryExhaustedError(
                    f"Slurm recorded an OOM kill in step {job.id}.{self.ready.step}"
                )
        return error
