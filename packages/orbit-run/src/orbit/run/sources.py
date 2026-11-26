"""Filesystem Task references, and the operator's cache for everything else."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from orbit.core.errors import RegistryError
from orbit.core.lock import TaskSource

__all__ = ["ResolvedSource", "cache_root", "cached", "resolve"]


def cache_root() -> Path:
    """Operator cache retained for non-Task assets such as VM disks and GPU locks."""
    base = os.environ.get("ORBIT_CACHE_DIR") or (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "orbit"
    )
    return Path(base)


def cached(root: Path, fill: Callable[[Path], None]) -> Path:
    """``root``, made once: ``fill`` writes a fresh directory, which then takes its name.

    Processes wait on one lock beside ``root``, so each entry is made once, and a fill that
    fails leaves nothing behind.
    """
    if root.is_dir():
        return root
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent / f".{root.name}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if root.is_dir():
            return root
        partial = Path(tempfile.mkdtemp(prefix=f".{root.name}-", dir=root.parent))
        try:
            fill(partial)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        partial.rename(root)
    return root


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
