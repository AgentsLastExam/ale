"""Canonical filesystem content identities."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath

from ale.core.errors import TaskDefinitionError

__all__ = ["tree_digest"]


def tree_digest(root: Path, *, exclude: tuple[str, ...] = ()) -> str:
    """Hash one tree independent of its absolute parent path."""
    root = root.resolve()
    if not root.is_dir():
        raise TaskDefinitionError(f"content root is not a directory: {root}")
    digest = hashlib.sha256()
    excluded = tuple(PurePosixPath(item) for item in exclude)
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative_path = PurePosixPath(path.relative_to(root).as_posix())
        if any(relative_path == item or item in relative_path.parents for item in excluded):
            continue
        relative = relative_path.as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise TaskDefinitionError(f"symlink escapes content root: {relative} -> {target}")
            _field(digest, b"symlink")
            _field(digest, relative.encode())
            _field(digest, target.encode())
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            raise TaskDefinitionError(f"unsupported filesystem entry: {path}")
        data = path.read_bytes()
        _field(digest, b"file")
        _field(digest, relative.encode())
        _field(digest, b"1" if path.stat().st_mode & 0o111 else b"0")
        _field(digest, len(data).to_bytes(8, "big"))
        _field(digest, data)
    return f"sha256:{digest.hexdigest()}"


def _field(digest: hashlib._Hash, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)
