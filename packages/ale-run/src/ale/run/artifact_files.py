"""Dependency-free file inspection, executed inside either kind of Sandbox.

Only ordinary files and directories cross the boundary. Links are followed; cycles,
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

    def visit(path: Path, parts: tuple[str, ...], ancestors: frozenset, depth: int) -> None:
        nonlocal visited
        visited += 1
        if depth > MAX_DEPTH:
            skipped.append(f"{path}: directory depth limit reached")
            return
        if visited > MAX_ENTRIES:
            raise ValueError("artifact traversal limit reached")
        try:
            info = path.stat()  # Follow both file and directory links.
            directory = stat.S_ISDIR(info.st_mode)
            identity = (info.st_dev, info.st_ino)
            if directory and identity in ancestors:
                skipped.append(f"{path}: directory link cycle")
                return
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
                visit(path, parts[1:], ancestors, depth)
            ancestors = ancestors | {identity}
            with os.scandir(path) as children:
                for child in sorted(children, key=lambda item: item.name):
                    if not parts or (parts[0] == "**" and not child.name.startswith(".")):
                        visit(Path(child.path), parts, ancestors, depth + 1)
                    elif (
                        not child.name.startswith(".") or parts[0].startswith(".")
                    ) and fnmatch.fnmatch(child.name, parts[0]):
                        visit(Path(child.path), parts[1:], ancestors, depth + 1)
        except OSError as exc:
            skipped.append(f"{path}: {exc}")

    path = Path(pattern)
    # Start at the longest literal prefix to avoid scanning unrelated directories.
    prefix = Path(path.anchor)
    parts = path.parts[1:]
    while parts and not any(char in parts[0] for char in "*?["):
        prefix /= parts[0]
        parts = parts[1:]
    try:
        visit(prefix, parts, frozenset(), 0)
    except ValueError as exc:
        skipped.append(str(exc))
    return {"entries": entries, "skipped": skipped}


def read(path: str) -> None:
    # O_NONBLOCK prevents a file replaced by a FIFO after inspection from hanging.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("artifact is no longer a regular file")
        while block := source.read(1024 * 1024):
            sys.stdout.buffer.write(block)


if __name__ == "__main__":
    if sys.argv[1] == "inspect":
        print(json.dumps(inspect(sys.argv[2])))
    else:
        read(sys.argv[2])
