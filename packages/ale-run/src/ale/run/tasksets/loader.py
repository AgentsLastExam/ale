"""Dispatch Task loading from the manifest format actually present."""

from __future__ import annotations

from pathlib import Path

from ale.core.errors import TaskDefinitionError
from ale.core.task import Task
from ale.run.harbor.task import load_harbor_task_folder, load_harbor_tasks
from ale.run.tasksets.manifest import load_standard_task_folder, load_standard_tasks

__all__ = ["load_task_folder", "load_tasks"]


def load_tasks(path: Path) -> list[Task]:
    kind = _manifest_kind(path)
    if kind == "standard":
        return list(load_standard_tasks(path))
    return list(load_harbor_tasks(path))


def load_task_folder(path: Path, *, variant: str = "base") -> Task:
    kind = _manifest_kind(path)
    if kind == "standard":
        return load_standard_task_folder(path, variant=variant)
    return load_harbor_task_folder(path, variant=variant)


def _manifest_kind(path: Path) -> str:
    source = path.expanduser().resolve()
    standard = list(source.rglob("task.yaml")) if source.is_dir() else []
    harbor = list(source.rglob("task.toml")) if source.is_dir() else []
    if standard and harbor:
        raise TaskDefinitionError(
            f"mixed task.yaml and task.toml collections are not supported: {source}"
        )
    if standard:
        return "standard"
    if harbor:
        return "harbor"
    raise TaskDefinitionError(f"no task.yaml or task.toml found below {source}")
