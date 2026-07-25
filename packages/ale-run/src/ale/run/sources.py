"""Where task content comes from.

Two sources, one execution path. A local checkout is what an author iterates against; a
registry reference is what everyone else runs. They differ only in how the folder gets
onto disk and in what provenance records — never in how the task is loaded or run, so
"works on my machine" and "works in the run" cannot come apart.

Fetched content is cached by resolved commit, so a repository is cloned once per version
no matter how many episodes use it.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ale.core.errors import RegistryError
from ale.core.lock import TaskSource

__all__ = ["Registry", "ResolvedSource", "cache_root", "resolve"]

REGISTRY_FILE = "registry.toml"


def cache_root() -> Path:
    """Shared across worktrees so a second checkout re-downloads nothing."""
    base = os.environ.get("ALE_CACHE_DIR") or (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ale"
    )
    return Path(base)


@dataclass(frozen=True)
class DomainEntry:
    """One registry line: where a domain's tasks live."""

    repo: str
    path: str = "tasks"
    default_ref: str = "main"


class Registry:
    """Domain name to repository. Several domains may share one repository."""

    def __init__(self, entries: dict[str, DomainEntry]) -> None:
        self.entries = entries

    @classmethod
    def load(cls, path: Path) -> Registry:
        if not path.is_file():
            raise RegistryError(f"no {REGISTRY_FILE} at {path}")
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        domains = raw.get("domains") or {}
        return cls({name: DomainEntry(**entry) for name, entry in domains.items()})

    def entry(self, domain: str) -> DomainEntry:
        if domain not in self.entries:
            known = ", ".join(sorted(self.entries)) or "none"
            raise RegistryError(f"unknown domain {domain!r}; registered: {known}")
        return self.entries[domain]


@dataclass(frozen=True)
class ResolvedSource:
    """A task folder on disk, and the provenance that describes it."""

    task_dir: Path
    source: TaskSource


def resolve(
    reference: str, *, registry: Registry | None = None, ref: str | None = None
) -> ResolvedSource:
    """Resolve a task reference to a folder on disk.

    A path is used as it stands; anything else is ``<domain>/<task-path>`` and is
    fetched at a pinned commit.
    """
    candidate = Path(reference).expanduser()
    if candidate.exists():
        return ResolvedSource(
            task_dir=candidate.resolve(),
            source=TaskSource(kind="local", path=str(candidate.resolve())),
        )

    if registry is None:
        raise RegistryError(f"{reference} is not a path, and no registry was provided")

    domain, _, task_path = reference.partition("/")
    if not task_path:
        raise RegistryError(f"expected <domain>/<task>, got {reference!r}")

    entry = registry.entry(domain)
    checkout, commit = _fetch(entry.repo, ref or entry.default_ref)
    task_dir = checkout / entry.path / task_path
    if not task_dir.is_dir():
        raise RegistryError(f"{reference} not found in {entry.repo} at {commit[:12]}")

    return ResolvedSource(
        task_dir=task_dir,
        source=TaskSource(
            kind="registry",
            repo=entry.repo,
            commit=commit,
            path=f"{entry.path}/{task_path}",
        ),
    )


def _fetch(repo: str, ref: str) -> tuple[Path, str]:
    """Clone or update ``repo`` at ``ref``, returning the checkout and its commit.

    Keyed by resolved commit rather than by ref, so a branch that moves produces a new
    checkout instead of quietly changing what an old result meant.
    """
    commit = _resolve_commit(repo, ref)
    target = cache_root() / "tasks" / _slug(repo) / commit
    if (target / ".git").is_dir():
        return target, commit

    target.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "--quiet", "--filter=blob:none", "--no-checkout", repo, str(target))
    _git("-C", str(target), "checkout", "--quiet", commit)
    return target, commit


def _resolve_commit(repo: str, ref: str) -> str:
    if len(ref) == 40 and all(char in "0123456789abcdef" for char in ref):
        return ref
    out = _git("ls-remote", repo, ref)
    if not out.strip():
        raise RegistryError(f"{repo} has no ref {ref!r}")
    return out.split()[0]


def _git(*argv: str) -> str:
    result = subprocess.run(["git", *argv], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RegistryError(f"git {' '.join(argv[:2])} failed: {result.stderr.strip()}")
    return result.stdout


def _slug(repo: str) -> str:
    return repo.rstrip("/").removesuffix(".git").replace("://", "-").replace("/", "-")
