"""Load self-contained Task folders from filesystem paths."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ale.core.environment import EpisodeContext
from ale.core.errors import TaskDefinitionError
from ale.core.task import Task, TaskSourceContext
from ale.core.taskspec import (
    PhaseTimeouts,
    Resources,
    TaskManifestV1,
    TaskSpec,
    VariantOverride,
    VerificationMode,
)
from ale.core.template import render_instruction
from ale.core.verdict import Rewards
from ale.run.content import tree_digest

__all__ = [
    "INSTRUCTION",
    "ORACLE_DIR",
    "SETUP_DIR",
    "STAGE_ENTRY",
    "TASK_MANIFEST",
    "VERIFY_DIR",
    "ManifestTask",
    "TaskFolder",
    "discover_task_folders",
    "load_task_folder",
    "load_tasks",
]

TASK_MANIFEST = "task.yaml"
INSTRUCTION = "instruction.md"
STAGE_ENTRY = "run.sh"
VERIFY_DIR = "verify"
ORACLE_DIR = "oracle"
SETUP_DIR = "setup"
IMAGE_DIR = "image"
_SOURCE_EXCLUDES = (
    "image/assets",
    "setup/assets",
    "verify/assets",
    "oracle/assets",
    ".ale-cache",
)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise TaskDefinitionError(f"missing {path.name}: {path}") from exc
    except yaml.YAMLError as exc:
        raise TaskDefinitionError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise TaskDefinitionError(f"{path} must contain a mapping")
    return loaded


class TaskFolder:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @property
    def image_dir(self) -> Path:
        return self.root / IMAGE_DIR

    @property
    def image_dockerfile(self) -> Path | None:
        path = self.image_dir / "Dockerfile"
        return path if path.is_file() else None

    @property
    def verifier_dockerfile(self) -> Path | None:
        path = self.root / VERIFY_DIR / "Dockerfile"
        return path if path.is_file() else None

    def stage_dir(self, name: str) -> Path | None:
        path = self.root / name
        return path if path.is_dir() else None

    def stage_entry(self, name: str) -> Path | None:
        directory = self.stage_dir(name)
        entry = directory / STAGE_ENTRY if directory else None
        return entry if entry and entry.is_file() else None

    @cached_property
    def content_digest(self) -> str:
        return tree_digest(self.root, exclude=_SOURCE_EXCLUDES)

    @cached_property
    def image_source_digest(self) -> str | None:
        if self.image_dockerfile is None:
            return None
        return tree_digest(self.image_dir, exclude=("assets",))

    @cached_property
    def verifier_image_source_digest(self) -> str | None:
        if self.verifier_dockerfile is None:
            return None
        return tree_digest(self.root / VERIFY_DIR, exclude=("assets",))

    @cached_property
    def source(self) -> TaskSourceContext:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return TaskSourceContext()
        if result.returncode != 0:
            return TaskSourceContext()
        repository_root = Path(result.stdout.strip()).resolve()
        try:
            relative = self.root.relative_to(repository_root).as_posix()
        except ValueError:
            return TaskSourceContext()
        return TaskSourceContext(
            repository_root=repository_root,
            repository_name=repository_root.name,
            task_relative_path=relative,
        )


class ManifestTask(Task):
    folder: TaskFolder

    def __init__(self, spec: TaskSpec, folder: TaskFolder) -> None:
        super().__init__(
            spec,
            folder=folder,
            source=folder.source,
            task_digest=folder.content_digest,
            image_source_digest=folder.image_source_digest,
            verifier_image_source_digest=folder.verifier_image_source_digest,
        )

    async def score(self, ctx: EpisodeContext) -> Rewards:
        if ctx.verified_rewards is None:
            raise TaskDefinitionError(
                "verification produced no rewards; verify/run.sh must write them"
            )
        return {str(key): float(value) for key, value in ctx.verified_rewards.items()}


def discover_task_folders(path: Path) -> tuple[TaskFolder, ...]:
    source = path.expanduser().resolve()
    if not source.exists():
        raise TaskDefinitionError(f"Task path does not exist: {source}")
    if (source / TASK_MANIFEST).is_file():
        return (TaskFolder(source),)
    manifests = sorted(
        source.rglob(TASK_MANIFEST),
        key=lambda item: item.relative_to(source).as_posix(),
    )
    if not manifests:
        raise TaskDefinitionError(f"no {TASK_MANIFEST} found below {source}")
    return tuple(TaskFolder(manifest.parent) for manifest in manifests)


def load_tasks(path: Path) -> list[ManifestTask]:
    tasks: list[ManifestTask] = []
    seen: dict[str, Path] = {}
    repositories: dict[str, Path] = {}
    for folder in discover_task_folders(path):
        source = folder.source
        if source.repository_name and source.repository_root:
            previous_root = repositories.get(source.repository_name)
            if previous_root is not None and previous_root != source.repository_root:
                raise TaskDefinitionError(
                    f"ambiguous Task repository name {source.repository_name!r}: "
                    f"{previous_root} and {source.repository_root}"
                )
            repositories[source.repository_name] = source.repository_root
        for task in _load_folder(folder):
            previous = seen.get(str(task.spec.name))
            if previous is not None and previous != folder.root:
                raise TaskDefinitionError(
                    f"duplicate Task name {task.spec.name!r}: {previous} and {folder.root}"
                )
            seen[str(task.spec.name)] = folder.root
            tasks.append(task)
    return tasks


def load_task_folder(path: Path, *, variant: str = "base") -> ManifestTask:
    tasks = [task for task in load_tasks(path) if task.spec.variant == variant]
    if not tasks:
        raise TaskDefinitionError(f"{path} has no variant {variant!r}")
    if len(tasks) > 1:
        raise TaskDefinitionError(f"{path} selects more than one Task")
    return tasks[0]


def _load_folder(folder: TaskFolder) -> Iterator[ManifestTask]:
    _require_folder(folder)
    try:
        manifest = TaskManifestV1.model_validate(_read_yaml(folder.root / TASK_MANIFEST))
    except ValidationError as exc:
        raise TaskDefinitionError(
            f"invalid task manifest {folder.root / TASK_MANIFEST}: {exc}"
        ) from exc
    if folder.image_dockerfile is None and manifest.image.ref is None:
        raise TaskDefinitionError("solver requires image/Dockerfile or image.ref")
    if folder.image_dockerfile is None and _has_files(folder.image_dir / "assets"):
        raise TaskDefinitionError("image/assets is unused when solver uses image.ref")

    verifier_dockerfile = folder.verifier_dockerfile
    if verifier_dockerfile is not None:
        if manifest.verify.environment_mode is VerificationMode.SHARED:
            raise TaskDefinitionError("verify/Dockerfile requires separate verification")
        if manifest.verify.image is None:
            raise TaskDefinitionError("verify/Dockerfile requires verify.image.kind")
    elif manifest.verify.image is not None and manifest.verify.image.ref is None:
        raise TaskDefinitionError("external verifier requires verify.image.ref")
    template = (folder.root / INSTRUCTION).read_text(encoding="utf-8")
    yield ManifestTask(_effective_spec(manifest, template, "base", None), folder)
    for variant in manifest.variants:
        yield ManifestTask(_effective_spec(manifest, template, str(variant.name), variant), folder)


def _require_folder(folder: TaskFolder) -> None:
    required = (
        folder.root / TASK_MANIFEST,
        folder.root / INSTRUCTION,
        folder.root / VERIFY_DIR / STAGE_ENTRY,
        folder.root / ORACLE_DIR / STAGE_ENTRY,
    )
    missing = [path.relative_to(folder.root).as_posix() for path in required if not path.is_file()]
    if missing:
        raise TaskDefinitionError(
            f"{folder.root} is missing required Task file(s): {', '.join(missing)}"
        )


def _effective_spec(
    manifest: TaskManifestV1,
    template: str,
    variant_name: str,
    variant: VariantOverride | None,
) -> TaskSpec:
    params = manifest.params | (variant.params if variant else {})
    resources = _overlay(manifest.resources, variant.resources if variant else None)
    timeouts = _overlay(manifest.timeouts, variant.timeouts if variant else None)
    instruction = render_instruction(
        template,
        params,
        where=f"{manifest.name}/{INSTRUCTION}",
    )
    return TaskSpec(
        name=manifest.name,
        variant=variant_name,
        environment=manifest.environment,
        image=manifest.image,
        instruction=instruction,
        resources=Resources.model_validate(resources),
        network=manifest.network,
        timeouts=PhaseTimeouts.model_validate(timeouts),
        artifacts=manifest.artifacts,
        tools=manifest.tools,
        params=params,
        verify=manifest.verify,
        metadata=manifest.metadata,
        extras=manifest.extras,
    )


def _overlay(base: object, override: object | None) -> dict[str, Any]:
    values = base.model_dump(mode="python")  # type: ignore[attr-defined]
    if override is None:
        return values
    updates = override.model_dump(mode="python", exclude_none=True)  # type: ignore[attr-defined]
    return values | updates


def _has_files(path: Path) -> bool:
    return path.is_dir() and any(item.is_file() for item in path.rglob("*"))
