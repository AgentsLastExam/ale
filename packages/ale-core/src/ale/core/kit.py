"""Flat Task-repository Python Kit contract."""

from __future__ import annotations

import keyword
from pathlib import Path

from ale.core.errors import TaskDefinitionError

__all__ = ["resolve_kit", "validate_kit_name"]


def validate_kit_name(name: str) -> str:
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        raise TaskDefinitionError(
            f"kit name {name!r} must be a non-keyword Python package identifier"
        )
    return name


def resolve_kit(repo_root: Path, name: str) -> Path:
    package = repo_root / "kits" / validate_kit_name(name)
    if not package.is_dir() or not (package / "__init__.py").is_file():
        raise TaskDefinitionError(f"kit {name!r} must exist as kits/{name}/__init__.py")
    return package
