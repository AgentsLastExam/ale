"""Filesystem Task references and ordered variant selection."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ale.core.errors import RegistryError, TaskDefinitionError
from ale.core.lock import TaskSource

__all__ = [
    "ResolvedSource",
    "TaskReference",
    "cache_root",
    "parse_task_reference",
    "resolve",
    "select_tasks",
]

_VARIANT = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class TaskReference:
    source: str
    variants: tuple[str, ...] = ("base",)


def parse_task_reference(reference: str) -> TaskReference:
    candidate = Path(reference).expanduser()
    if candidate.exists():
        return TaskReference(reference)
    source, separator, selector = reference.rpartition("@")
    if not separator:
        return TaskReference(reference)
    names = (
        tuple(selector[1:-1].split(","))
        if selector.startswith("{") and selector.endswith("}")
        else (selector,)
    )
    if not source or not names or any(not _VARIANT.fullmatch(name) for name in names):
        raise RegistryError(f"malformed Task variant selector in {reference!r}")
    if len(set(names)) != len(names):
        raise RegistryError(f"duplicate Task variant in {reference!r}")
    return TaskReference(source=source, variants=names)


def select_tasks(tasks: list[object], variants: tuple[str, ...]) -> list[object]:
    families: dict[object, dict[str, object]] = {}
    order: list[object] = []
    for task in tasks:
        spec = task.spec  # type: ignore[attr-defined]
        if spec.name not in families:
            families[spec.name] = {}
            order.append(spec.name)
        families[spec.name][spec.variant] = task
    selected: list[object] = []
    for name in order:
        available = families[name]
        missing = [variant for variant in variants if variant not in available]
        if missing:
            choices = ", ".join(available)
            raise TaskDefinitionError(f"{name} has no variant {missing[0]!r}; available: {choices}")
        selected.extend(available[variant] for variant in variants)
    return selected


def cache_root() -> Path:
    """Operator cache retained for non-Task assets such as VM disks and GPU locks."""
    base = os.environ.get("ALE_CACHE_DIR") or (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ale"
    )
    return Path(base)


@dataclass(frozen=True)
class ResolvedSource:
    task_dir: Path
    source: TaskSource


def resolve(reference: str) -> ResolvedSource:
    candidate = Path(reference).expanduser()
    if not candidate.exists():
        raise RegistryError(
            f"{reference!r} is not a filesystem Task or collection path; "
            "remote Task registries are not part of the standard contract"
        )
    resolved = candidate.resolve()
    return ResolvedSource(
        task_dir=resolved,
        source=TaskSource(kind="local", path=str(resolved)),
    )
