"""Images prepared on the cluster, as the machine Orbit runs on has no container runtime.

A Task's image is pulled, or built from its context, the framework layer is added, and the
result is saved below the shared cache under a key of everything that went into it, by a job
of its own on a compute node. Every allocation loads it from there.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from orbit.core.config import SlurmPartition, SlurmProviderConfig
from orbit.core.errors import ProviderStartError, TaskDefinitionError
from orbit.core.ids import content_hash
from orbit.core.sandbox import ImageKind, PreparedTaskImage
from orbit.run.framework import (
    KEEP_ALIVE,
    container_framework,
    framework_identity,
    stage_container_framework,
)
from orbit.run.images import acquire_lock
from orbit.run.oci import Reference, Registry, api, qualified
from orbit.run.providers.slurm.job import IMAGES, Job, cache, read, tail, write

#: What a preparation job holds, at most: Podman builds, pulls and saves in it.
BUILD_CPUS = 8
BUILD_MEMORY_MB = 32768
#: Beyond a build's own limit, for the pull of its base, the framework layer and the save.
BUILD_MARGIN_MINUTES = 30
#: How long after a job ends what it wrote last may take to show on the login node.
NFS_SETTLE_SEC = 90
#: Below the cache: the framework layer's files, staged once per framework.
FRAMEWORK = "framework"


@dataclass(frozen=True)
class Recipe:
    """What one image is prepared from: a registry ``reference`` pinned by digest, or a build
    ``context`` less its ``exclude`` directories, with the framework for ``user``."""

    key: str
    source: Literal["solver-local", "verifier-local", "validation-local", "external-ref"]
    reference: str | None = None
    context: Path | None = None
    exclude: tuple[str, ...] = ()
    source_identity: str | None = None
    user: str = "user"
    keep_alive: bool = False
    timeout_sec: float = 3600.0


async def pin(reference: str) -> str:
    """``reference`` pinned by the digest its registry names it by now, read over HTTP, as
    no container runtime runs here to pull it."""
    parsed = Reference.parse(qualified(reference))
    if parsed.digest is not None:
        return parsed.pinned(parsed.digest)
    async with Registry(api(parsed)) as registry:
        data, _ = await registry.manifest(parsed.tag or "latest")
    return parsed.pinned("sha256:" + hashlib.sha256(data).hexdigest())


def key(
    source: str, *, reference: str | None, context: str | None, user: str, keep_alive: bool
) -> str:
    """The key of an image prepared from a pinned ``reference``, or a build context whose
    identity is ``context``, with the framework for ``user``: everything that goes into it."""
    return content_hash(
        {
            "source": source,
            "reference": reference,
            "context": context,
            "framework": framework_identity(user=user, vm=False),
            # Only when set, so every other image keeps its key.
            **({"command": KEEP_ALIVE} if keep_alive else {}),
        }
    )


def archive(key: str) -> str:
    """Where below the cache the image ``key`` names is saved."""
    return f"{IMAGES}/{key.removeprefix('sha256:')}.tar.gz"


def build_partition(config: SlurmProviderConfig) -> SlurmPartition:
    """Where images are prepared: the first partition without GPUs, else the first."""
    return next((part for part in config.partitions if not part.max_gpus), config.partitions[0])


def recorded(config: SlurmProviderConfig, image: PreparedTaskImage) -> dict[str, object] | None:
    """What the cache records of ``image``, if it still holds it as it was prepared."""
    record = cache(config) / IMAGES / f"{image.input_identity.removeprefix('sha256:')}.json"
    try:
        prepared = json.loads(record.read_text())
    except (OSError, ValueError):
        return None
    if prepared.get("image") != image.prepared_identity:
        return None
    return prepared if (cache(config) / image.runtime_ref).is_file() else None


async def prepare(config: SlurmProviderConfig, recipe: Recipe) -> tuple[PreparedTaskImage, bool]:
    """The image ``recipe`` describes, prepared once per key, and whether an earlier
    preparation had prepared it."""
    key = recipe.key.removeprefix("sha256:")
    images = cache(config) / IMAGES
    images.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = images / f"{key}.json"
    with (images / f"{key}.lock").open("a") as lock:
        await acquire_lock(lock)
        try:
            try:
                prepared = json.loads(record.read_text())
            except (OSError, ValueError):
                prepared = None
            reused = prepared is not None and (cache(config) / archive(recipe.key)).is_file()
            if not reused:
                prepared = await _prepare(config, recipe) | {"user": recipe.user}
                write(record, json.dumps(prepared).encode())
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    assert prepared is not None
    return (
        PreparedTaskImage(
            kind=ImageKind.CONTAINER,
            source=recipe.source,
            input_identity=recipe.key,
            image_source_identity=recipe.source_identity,
            runtime_ref=archive(recipe.key),
            prepared_identity=str(prepared["image"]),
            resolved_reference=recipe.reference,
        ),
        reused,
    )


async def _prepare(config: SlurmProviderConfig, recipe: Recipe) -> dict[str, object]:
    """Run the job that prepares ``recipe``; what it prepared."""
    part = build_partition(config)
    body, tail_lines = container_framework(user=recipe.user, keep_alive=recipe.keep_alive)
    context, python = await asyncio.to_thread(_framework, config, recipe.user)
    job = Job(config, part)
    try:
        job.stage(
            dict(
                key=recipe.key.removeprefix("sha256:"),
                reference=recipe.reference,
                framework=dict(
                    context=str(context), python=str(python), body=body, tail=tail_lines
                ),
            ),
            name="recipe.json",
        )
        if recipe.context is not None:
            excluded = set(recipe.exclude)
            await asyncio.to_thread(
                shutil.copytree,
                recipe.context,
                job.directory / "context",
                symlinks=True,
                ignore=lambda directory, names: (
                    [name for name in names if name in excluded]
                    if Path(directory) == recipe.context
                    else []
                ),
            )
        await job.submit(
            cpus=min(BUILD_CPUS, part.max_cpus),
            memory_mb=min(BUILD_MEMORY_MB, part.max_memory_mb),
            gpus=0,
            minutes=math.ceil(recipe.timeout_sec / 60) + BUILD_MARGIN_MINUTES,
            command="prepare",
        )
        return await _prepared(job, config.queue_timeout_sec, recipe)
    finally:
        # Its batch step reads no stop, and the job's own outcome is what stands.
        try:
            await asyncio.shield(job.stop(cancel=True))
        except ProviderStartError as error:
            logging.getLogger("orbit.execution").warning(
                "an image preparation job did not end cleanly",
                extra={"orbit_data": {"job": job.id, "error": str(error)}},
            )


async def _prepared(job: Job, queue_timeout: int, recipe: Recipe) -> dict[str, object]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + queue_timeout
    started = False
    ended: float | None = None
    while True:
        if prepared := read(job.directory / "prepared"):
            return dict(json.loads(prepared))
        if error := read(job.directory / "error"):
            what = recipe.reference or "the Task's image"
            # The Task's own build failing is the Task's; any other step's, the cluster's.
            build = read(job.directory / "failed") == "build"
            failure = TaskDefinitionError if build else ProviderStartError
            raise failure(
                f"Slurm job {job.id} could not prepare {what}: {error}\n"
                + tail(job.directory / "job.log")
            )
        state = "" if ended is not None else await job.state()
        if state == "RUNNING" and not started:
            started, deadline = True, math.inf
        if not state:
            # What a compute node wrote last reaches this NFS client once it no longer
            # trusts its cached listing of the directory, up to a minute later.
            ended = loop.time() if ended is None else ended
            if loop.time() - ended > NFS_SETTLE_SEC:
                raise ProviderStartError(
                    f"Slurm job {job.id} ended before it prepared the image: "
                    + tail(job.directory / "job.log")
                )
        if loop.time() > deadline:
            raise ProviderStartError(f"Slurm job {job.id} timed out during queueing")
        await asyncio.sleep(2)


def _framework(config: SlurmProviderConfig, user: str) -> tuple[Path, Path]:
    """The framework layer's files and its Python, below the cache, staged once."""
    root = cache(config) / FRAMEWORK
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = framework_identity(user=user, vm=False).removeprefix("sha256:")
    staged = root / identity
    with (root / f"{identity}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not staged.is_dir():
            # Whole or not at all: staged apart, then renamed into place.
            partial = root / f".{identity}.part"
            shutil.rmtree(partial, ignore_errors=True)
            source = stage_container_framework(partial / "context")
            # Copied, as the engine's own cache need not be on the shared file system.
            shutil.copytree(source, partial / "python", symlinks=True)
            partial.rename(staged)
    return staged / "context", staged / "python"
