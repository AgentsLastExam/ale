"""The images a Node prepared, each under its key.

Every Node prepares the images it serves itself, through the very preparation a local Run
uses, and Nodes never copy images from one another. A Run sends a Node what preparing one
of its images needs: its inputs (one of a Task's images, named by the Task's
specification, or a bare reference) and, for an image built from the Task folder, the
build context. The Node keeps both and the image it prepared under the image's key, and
lists what it holds in its heartbeat. Like a local Run, it runs the preparation once per
Run, which finds its own caches and so takes seconds for an image it built before, and
within a Run checks the image is still there each time it hands it out.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ale.core.config import RunConfig
from ale.core.episode import EpisodeContext
from ale.core.errors import AleError, ProviderCapabilityError
from ale.core.fleet import ImageHolding, ImageInputs
from ale.core.sandbox import PreparedTaskImage, Provider
from ale.core.task import Task
from ale.core.taskspec import ImageKind, OperatingSystem
from ale.core.verdict import Rewards
from ale.run.process import run
from ale.run.providers import ProviderRegistry
from ale.run.task_images import prepare_task_image_result, prepare_verifier_image_result
from ale.run.tasksets.manifest import TaskFolder

__all__ = ["Lend", "NodeImages"]

#: The Node's Provider for a kind and operating system, as its profiles name it.
Lend = Callable[[ImageKind, OperatingSystem], Provider]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    image: PreparedTaskImage
    bytes: int
    prepare_sec: float


class _Lent(ProviderRegistry):
    """The Node's own Providers, where preparation asks a Run's configuration for them."""

    def __init__(self, lend: Lend) -> None:
        super().__init__(RunConfig())
        self._lend = lend

    def get(self, kind: ImageKind, operating_system: OperatingSystem) -> Provider:
        return self._lend(kind, operating_system)


class _BuildTask(Task):
    """A Task as far as preparing one of its images needs it: its specification and the
    build context of that image."""

    def __init__(self, inputs: ImageInputs, root: Path) -> None:
        assert inputs.task is not None
        spec = inputs.task
        folder = TaskFolder(root)
        solver = inputs.role == "solver"
        super().__init__(
            spec,
            folder=folder,
            task_digest=inputs.key,
            image_source_digest=folder.image_source_digest(spec.image) if solver else None,
            verifier_image_source_digest=(
                None if solver else folder.verifier_image_source_digest(spec.verify.image)
            ),
        )

    async def score(self, ctx: EpisodeContext) -> Rewards:
        raise NotImplementedError("a Node only prepares this Task's images")


def _built(inputs: ImageInputs) -> bool:
    """Whether the image is built from the Task folder, rather than named by a reference."""
    assert inputs.task is not None
    image = inputs.task.image if inputs.role == "solver" else inputs.task.verify.image
    return image is not None and image.ref is None


async def _size(image: PreparedTaskImage) -> int:
    if image.kind is ImageKind.CONTAINER:
        code, out, _ = await run(
            "docker", "image", "inspect", "--format", "{{.Size}}", image.runtime_ref, timeout=30
        )
        return int(out.strip() or 0) if code == 0 else 0
    disk = Path(image.runtime_ref)
    # A macOS image is named in Tart's own store, not by a path.
    return disk.stat().st_size if disk.is_file() else 0


class NodeImages:
    def __init__(self, root: Path, lend: Lend) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._lend = lend
        self._providers = _Lent(lend)
        self._locks: dict[str, asyncio.Lock] = {}
        self._records: dict[str, _Record] = {}
        self._runs: dict[str, set[str]] = {}
        """The Runs each image was prepared for since the agent started."""
        for path in sorted(root.glob("*.json")):
            try:
                record = _Record.model_validate_json(path.read_text())
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            self._records[f"sha256:{path.stem}"] = record

    def _folder(self, key: str) -> Path:
        return self.root / key.removeprefix("sha256:")

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def has_inputs(self, key: str) -> bool:
        return (self._folder(key) / "task").is_dir()

    async def keep_inputs(self, key: str, tree: Path) -> None:
        """Keep a build context the Run sent, unpacked at ``tree``, unless one is kept."""
        async with self._lock(key):
            if self.has_inputs(key):
                return
            self._folder(key).mkdir(parents=True, exist_ok=True)
            os.replace(tree, self._folder(key) / "task")

    def prepared(self, key: str) -> PreparedTaskImage | None:
        record = self._records.get(key)
        return record.image if record is not None else None

    async def prepare(self, inputs: ImageInputs) -> PreparedTaskImage:
        """The image ``inputs`` names, prepared here once per Run and checked on every later
        call of that Run."""
        key = inputs.key
        async with self._lock(key):
            record = self._records.get(key)
            if record is not None and inputs.run_id in self._runs.get(key, set()):
                provider = self._lend(record.image.kind, inputs.os)
                try:
                    await provider.prepare_image(record.image)
                except AleError:
                    self._forget(key)
                else:
                    # The recorded image, which admission compares a creating request to.
                    return record.image
            started = time.monotonic()
            image = await self._prepare(inputs)
            # How long the first preparation took is what a Node lacking the image faces.
            if record is None or record.image != image:
                record = _Record(
                    image=image,
                    bytes=await _size(image),
                    prepare_sec=round(time.monotonic() - started, 1),
                )
                path = self._folder(key).with_suffix(".json")
                partial = path.with_suffix(".partial")
                partial.write_text(record.model_dump_json())
                os.replace(partial, path)
                self._records[key] = record
            self._runs.setdefault(key, set()).add(inputs.run_id)
            return image

    async def _prepare(self, inputs: ImageInputs) -> PreparedTaskImage:
        if inputs.reference is not None:
            provider = self._lend(inputs.reference.kind, inputs.os)
            return await provider.prepare_image(inputs.reference)
        if _built(inputs) and not self.has_inputs(inputs.key):
            raise ProviderCapabilityError(
                f"this Node holds no build context for {inputs.key}; the Run sends it first"
            )
        # Off the event loop, as is all hashing of build inputs: heartbeats and leases go on.
        task = await asyncio.to_thread(_BuildTask, inputs, self._folder(inputs.key) / "task")
        if inputs.role == "solver":
            return (await prepare_task_image_result(task, self._providers)).image
        result = await prepare_verifier_image_result(task, self._providers)
        if result is None:
            raise ProviderCapabilityError("this Task's verifier has no image of its own")
        return result.image

    def _forget(self, key: str) -> None:
        self._records.pop(key, None)
        self._runs.pop(key, None)
        self._folder(key).with_suffix(".json").unlink(missing_ok=True)

    def inventory(self) -> tuple[ImageHolding, ...]:
        return tuple(
            ImageHolding(key=key, bytes=record.bytes, prepare_sec=record.prepare_sec)
            for key, record in sorted(self._records.items())
        )

    def free_gb(self) -> int:
        return shutil.disk_usage(self.root).free // 1024**3
