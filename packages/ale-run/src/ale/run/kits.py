"""Locate and hash framework and flat repository verification packages."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
from pathlib import Path

from ale.core.errors import TaskDefinitionError
from ale.core.ids import content_hash
from ale.core.kit import resolve_kit

__all__ = ["hash_kit", "installed_ale_verify", "resolve_kit"]

_IGNORED = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def hash_kit(directory: Path) -> str:
    entries: list[tuple[str, str]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or any(part in _IGNORED for part in path.parts):
            continue
        entries.append(
            (
                str(path.relative_to(directory)),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    return content_hash(entries)


def installed_ale_verify() -> tuple[Path, str, str]:
    source = Path(str(importlib.resources.files("ale_verify")))
    if not source.is_dir():
        raise TaskDefinitionError("installed ale_verify package directory is unavailable")
    return source, importlib.metadata.version("ale-verify"), hash_kit(source)
