"""Dependency-free file inspection, executed inside either kind of Sandbox.

Only ordinary files and directories cross the boundary. No link is followed anywhere on a
path, since one the agent made can point at anything the framework may read; links,
special files and failed reads are skipped. Scoring remains the Task's responsibility.
"""

from __future__ import annotations

import fnmatch
import json
import os
import stat
import sys
from pathlib import Path

MAX_DEPTH = 64
MAX_ENTRIES = 100_000


def inspect(pattern: str) -> dict[str, list]:
    entries: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()
    visited = 0

    def visit(path: Path, parts: tuple[str, ...], depth: int) -> None:
        nonlocal visited
        visited += 1
        if depth > MAX_DEPTH:
            skipped.append(f"{path}: directory depth limit reached")
            return
        if visited > MAX_ENTRIES:
            raise ValueError("artifact traversal limit reached")
        try:
            info = path.lstat()
            if _is_link(info):
                skipped.append(f"{path}: link")
                return
            directory = stat.S_ISDIR(info.st_mode)
            if not parts:
                if str(path) in seen:
                    return
                if not directory and not stat.S_ISREG(info.st_mode):
                    skipped.append(f"{path}: special file")
                    return
                seen.add(str(path))
                entries.append(
                    {
                        "source": str(path),
                        "kind": "directory" if directory else "file",
                        "mode": stat.S_IMODE(info.st_mode),
                    }
                )
            if not directory:
                return
            if parts and parts[0] == "**":
                visit(path, parts[1:], depth)
            with os.scandir(path) as children:
                for child in sorted(children, key=lambda item: item.name):
                    if not parts or (parts[0] == "**" and not child.name.startswith(".")):
                        visit(Path(child.path), parts, depth + 1)
                    elif (
                        not child.name.startswith(".") or parts[0].startswith(".")
                    ) and fnmatch.fnmatch(child.name, parts[0]):
                        visit(Path(child.path), parts[1:], depth + 1)
        except OSError as exc:
            skipped.append(f"{path}: {exc}")

    path = Path(pattern)
    # Start at the longest literal prefix to avoid scanning unrelated directories.
    prefix = Path(path.anchor)
    parts = path.parts[1:]
    while parts and not any(char in parts[0] for char in "*?["):
        prefix /= parts[0]
        parts = parts[1:]
    if (link := _link_on(prefix.parent)) is not None:
        return {"entries": [], "skipped": [f"{link}: link"]}
    try:
        visit(prefix, parts, 0)
    except ValueError as exc:
        skipped.append(str(exc))
    return {"entries": entries, "skipped": skipped}


def read(path: str) -> None:
    if (link := _link_on(Path(path))) is not None:
        raise ValueError(f"{link} is a link")
    # O_NONBLOCK prevents a file replaced by a FIFO after inspection from hanging, and
    # O_NOFOLLOW one replaced by a link from being followed.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("artifact is no longer a regular file")
        while block := source.read(1024 * 1024):
            sys.stdout.buffer.write(block)


def _is_link(info: os.stat_result) -> bool:
    # A Windows junction is a reparse point rather than a symbolic link.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _link_on(path: Path) -> Path | None:
    """The first part of ``path`` that is a link; a missing part ends the search."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if _is_link(current.lstat()):
                return current
        except OSError:
            return None
    return None


if __name__ == "__main__":
    if sys.argv[1] == "inspect":
        print(json.dumps(inspect(sys.argv[2])))
    else:
        read(sys.argv[2])
