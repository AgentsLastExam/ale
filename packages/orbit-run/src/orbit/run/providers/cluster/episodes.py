"""Episodes run as batch jobs: what the login node's dispatcher and each of an episode's jobs
say to each other, through the job's private directory below the cache.

The dispatcher writes the job's input, ``episode.json``, and the Host's credentials for it,
``credentials.env``, and submits the job. The job runs the whole episode on its first node
and writes its outcome, ``outcome.json``, last: the episode's result, in the Run's
directory, or a continuation, ``continuation.json``, when the job's wall time came first
and the episode goes on in the next job. A job that ends with neither lost the episode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from orbit.core.config import ClusterProviderConfig
from orbit.core.errors import ProviderStartError
from orbit.core.result import FailureInfo, ResultRecord
from orbit.core.verdict import Status
from orbit.run.providers.cluster.job import Job, exists, read, tail
from orbit.run.providers.cluster.managers import create_job

if TYPE_CHECKING:
    from orbit.run.providers.cluster.provider import EpisodeJob

SPEC = "episode.json"
CREDENTIALS = "credentials.env"
OUTCOME = "outcome.json"
CONTINUATION = "continuation.json"
#: How long a job's last files may take to show on the login node after it ends.
SETTLE_SEC = 90
#: How often the dispatcher looks at a job, and says why one still waits in the queue.
POLL_SEC = 5
NOTICE_SEC = 600


class EpisodeJobSpec(BaseModel):
    """What one job of an episode runs: the episode, as the dispatcher prepared it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_dir: str
    episode_id: str
    task: str
    """The Task's folder, on the shared file system, as the dispatcher loaded it."""
    task_digest: str
    """Its content, which the job checks before it runs anything."""
    config: dict[str, Any]
    """The Run's effective configuration."""
    provenance: dict[str, Any]
    """What the dispatcher observed before the episode: the Task's source, the agent (with
    the preset and authentication its configuration chose), the Gateway's limits; the job
    observes its own engine."""
    prepared: dict[str, Any]
    """The prepared solver, verifier and validation images, by role."""
    validation_rounds: int
    cpus: int
    memory_mb: int
    gpus: int
    nodes: int
    walltime_minutes: int
    continuation: str | None = None
    """The previous job's continuation, copied into this job's directory."""
    continue_from: str | None = None
    """The earlier episode's directory whose kept solver and session this one continues."""


class Outcome(BaseModel):
    """What a job did, written last."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["result", "continuation", "error"]
    message: str = ""


def write_private(path: Path, data: str) -> None:
    """Write ``data`` that only this account may read."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(data)


def credentials(env_file: Path | None, names: dict[str, str]) -> str:
    """The Host's credentials an episode's job needs, as a ``.env``: the Run's ``.env``, with
    the credentials set in this process's environment, which win over it as they do here."""
    lines = env_file.read_text().splitlines() if env_file is not None else []
    lines += [f"{name}={value}" for name, value in sorted(names.items())]
    return "\n".join(lines) + "\n"


class Dispatcher:
    """Run each attempt of an episode as one batch job, or a chain of them where each job's
    wall time comes before the episode ends, and wait for its outcome."""

    def __init__(self, config: ClusterProviderConfig, python: Path) -> None:
        self.config = config
        self.python = python

    async def attempt(self, spec: EpisodeJobSpec, size: EpisodeJob, secrets: str) -> None:
        """Run one attempt of ``spec``'s episode until it writes its result; a job that ends
        without one is recorded as the episode's failure."""
        started = datetime.now(UTC)
        continuation: str | None = None
        while True:
            job = create_job(self.config, size.partition)
            # A job that wrote its outcome ends by itself; one abandoned is cancelled.
            ended = False
            try:
                job.stage({}, name="stage.json")
                if continuation is not None:
                    write_private(job.directory / CONTINUATION, continuation)
                write_private(job.directory / CREDENTIALS, secrets)
                current = spec.model_copy(
                    update={"continuation": CONTINUATION if continuation is not None else None}
                )
                (job.directory / SPEC).write_text(current.model_dump_json())
                argv = [str(self.python), "-I", "-m", "orbit.run.cli", "episode-job", "."]
                await job.submit(
                    cpus=size.cpus,
                    memory_mb=size.memory_mb,
                    gpus=size.gpus,
                    minutes=spec.walltime_minutes,
                    nodes=size.nodes,
                    argv=argv,
                )
                outcome = await self._outcome(
                    job, None if continuation is not None else self.config.queue_timeout_sec
                )
                ended = True
                if outcome.kind == "continuation":
                    continuation = (job.directory / CONTINUATION).read_text()
                    continue
                if outcome.kind == "error":
                    _lost(spec, started, outcome.message, "ProviderStartError")
                return
            except ProviderStartError as error:
                _lost(spec, started, str(error), type(error).__name__)
                return
            finally:
                try:
                    await asyncio.shield(job.stop(cancel=not ended))
                except ProviderStartError as error:
                    logging.getLogger("orbit.execution").warning(
                        "an episode's job did not end cleanly; its directory stays",
                        extra={"orbit_data": {"job": job.id, "error": str(error)}},
                    )

    async def _outcome(self, job: Job, queue_timeout: int | None) -> Outcome:
        """Wait for ``job``'s outcome; ``queue_timeout`` seconds at most in the queue."""
        loop = asyncio.get_running_loop()
        submitted = noticed = loop.time()
        gone: float | None = None
        logger = logging.getLogger("orbit.execution")
        while True:
            if exists(job.directory / OUTCOME):
                return Outcome.model_validate_json(read(job.directory / OUTCOME))
            state = await job.state() if gone is None else ""
            if not state:
                gone = loop.time() if gone is None else gone
                if loop.time() - gone > SETTLE_SEC:
                    ended = await job.ended()
                    raise ProviderStartError(
                        f"{job.job_label} ended ({ended or 'gone'}) before the "
                        f"episode did: {tail(job.directory / 'job.log')}"
                    )
            elif state == "PENDING":
                if queue_timeout is not None and loop.time() - submitted > queue_timeout:
                    raise ProviderStartError(
                        f"{job.job_label} waited {queue_timeout} s in the queue"
                    )
                if loop.time() - noticed > NOTICE_SEC:
                    noticed = loop.time()
                    logger.info(
                        "an episode's job waits in the queue",
                        extra={"orbit_data": {"job": job.id, "why": await job.pending_reason()}},
                    )
            await asyncio.sleep(POLL_SEC)


def _lost(spec: EpisodeJobSpec, started: datetime, message: str, error: str) -> None:
    """Record an attempt whose job ended without its result as the episode's failure, unless
    it wrote one after all."""
    episode = Path(spec.run_dir) / spec.episode_id
    episode.mkdir(parents=True, exist_ok=True)
    if (episode / "result.json").is_file():
        return
    record = ResultRecord(
        episode_id=spec.episode_id,
        status=Status.ENV_ERROR,
        failure=FailureInfo(error_type=error, message=message),
        started_at=started,
        finished_at=datetime.now(UTC),
    )
    (episode / "result.json").write_text(record.model_dump_json(indent=2))


def read_spec(directory: Path) -> EpisodeJobSpec:
    return EpisodeJobSpec.model_validate(json.loads((directory / SPEC).read_text()))


def finish(
    directory: Path, kind: Literal["result", "continuation", "error"], message: str = ""
) -> None:
    """Write the job's outcome, last, as the dispatcher waits for it."""
    partial = directory / f".{OUTCOME}.part"
    partial.write_text(Outcome(kind=kind, message=message).model_dump_json())
    partial.replace(directory / OUTCOME)
