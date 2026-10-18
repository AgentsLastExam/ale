"""Filesystem Task references and ordered variant selection."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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
    return ResolvedSource(task_dir=resolved, source=_observe(resolved))


def _observe(path: Path) -> TaskSource:
    """Record a Task's git origin, so a clean committed Task can be run again exactly.

    A Task outside any git repository, in one this account may not read (git refuses
    repositories owned by another account), or on a host without git, is recorded as a
    local path.
    """
    directory = path if path.is_dir() else path.parent

    def git(*argv: str, cwd: Path | str = directory) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(cwd), *argv], capture_output=True, text=True, check=False
            )
        except OSError:
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    root = git("rev-parse", "--show-toplevel")
    commit = git("rev-parse", "HEAD")
    if not root or not commit:
        return TaskSource(kind="local", path=str(path))
    relative = path.relative_to(Path(root).resolve()).as_posix() or "."
    status = git("status", "--porcelain", "--untracked-files=all", "--", relative, cwd=root)
    return TaskSource(
        kind="git",
        repo=_without_credentials(git("config", "--get", "remote.origin.url") or root),
        commit=commit,
        path=relative,
        dirty=status is None or bool(status),
    )


def _without_credentials(url: str) -> str:
    """The origin as recorded: a token embedded in its URL never reaches a RunLock."""
    parts = urlsplit(url)
    if not parts.scheme or "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))
