"""Incrementally mirror one CLI's evidence without owning its execution."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from ale.core.harness import HarnessSession
from ale.core.sandbox import Sandbox, SandboxFile
from ale.core.taskspec import OperatingSystem
from ale.run.harnesses.base import (
    INVOCATIONS_NAME,
    LOG_ROOT,
    STDERR_NAME,
    STDOUT_NAME,
    CliHarness,
    NativeLogs,
)

SYNC_INTERVAL_SEC = 60.0
SYNC_TIMEOUT_SEC = 30.0
_CHUNK_BYTES = 256 * 1024
_POLL_FILE_BYTES = 8 * 1024 * 1024
_POLL_SOURCE_BYTES = 16 * 1024 * 1024
_STREAMS = frozenset((STDOUT_NAME, STDERR_NAME, INVOCATIONS_NAME))
_LOGGER = logging.getLogger("ale.execution")


@dataclass(frozen=True)
class _Copy:
    source: SandboxFile
    target: Path
    staged: Path
    offset: int
    append: bool


class CliLogCollector:
    """Episode-local byte offsets and snapshots; native directory scans are opt-in."""

    def __init__(
        self,
        sandbox: Sandbox,
        session: HarnessSession,
        harness: CliHarness,
        logs_dir: Path,
        *,
        native_logs: bool,
    ) -> None:
        self.sandbox = sandbox
        self.session = session
        self.logs_dir = logs_dir
        self.sources = (NativeLogs(LOG_ROOT, ("*",)),) + (
            harness.native_logs if native_logs else ()
        )
        self._seen: dict[str, SandboxFile] = {}
        self._failures: dict[str, str] = {}
        self._cursor: dict[str, str] = {}
        self._generation = 0
        self._path_type = (
            PureWindowsPath if session.os is OperatingSystem.WINDOWS else PurePosixPath
        )

    @property
    def incomplete(self) -> bool:
        return bool(self._failures)

    async def sync(self, *, final: bool = False) -> bool:
        """Copy changes only; the caller bounds a tick and serializes parsing with it."""
        changed = False
        for source in self.sources:
            changed = await self._sync_source(source, final=final) or changed
        return changed

    async def _sync_source(self, declaration: NativeLogs, *, final: bool) -> bool:
        root = str(self._path_type(self.session.home) / declaration.root)
        copies: list[_Copy] = []
        changed = False
        staged_bytes = 0
        try:
            files = await self.sandbox.list_files(root)
            if final and declaration.root == LOG_ROOT:
                names = {self._path_type(item.path).relative_to(root).as_posix() for item in files}
                for name in sorted(_STREAMS - names):
                    self._failed(str(self._path_type(root) / name), "required CLI log is absent")
            cursor = self._cursor.get(root, "")
            files = tuple(item for item in files if item.path > cursor) + tuple(
                item for item in files if item.path <= cursor
            )
            self._failures.pop(root, None)
            for item in files:
                relative = self._path_type(item.path).relative_to(self.session.home).as_posix()
                if ".." in PurePosixPath(relative).parts:
                    raise ValueError("log path escapes the episode home")
                if declaration.root == LOG_ROOT:
                    name = PurePosixPath(relative).relative_to(LOG_ROOT).as_posix()
                    if name not in _STREAMS:
                        continue
                    target = self.logs_dir / name
                else:
                    target = self.logs_dir / "native" / relative
                append = any(
                    _matches(relative, f"{declaration.root}/{pattern}")
                    for pattern in declaration.append_globs
                )
                try:
                    copy = await self._stage(item, target, append=append, final=final)
                except Exception as exc:
                    self._failed(item.path, type(exc).__name__)
                    continue
                if copy is not None:
                    copies.append(copy)
                    staged_bytes += copy.staged.stat().st_size
                if staged_bytes >= _POLL_SOURCE_BYTES:
                    changed = await self._publish(root, copies) or changed
                    copies.clear()
                    self._cursor[root] = item.path
                    staged_bytes = 0
                    if not final:
                        break
            if not copies:
                return changed
            return await self._publish(root, copies) or changed
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed(root, type(exc).__name__)
            return changed
        finally:
            for copy in copies:
                copy.staged.unlink(missing_ok=True)

    async def _publish(self, root: str, copies: list[_Copy]) -> bool:
        # Validate and commit bounded batches so a timeout cannot lose every file's progress.
        after = {item.path: item for item in await self.sandbox.list_files(root)}
        changed = False
        for copy in copies:
            current = after.get(copy.source.path)
            if current is None or not _stable(copy, current):
                self._failed(copy.source.path, "source changed while being read")
                copy.staged.unlink(missing_ok=True)
                continue
            self._commit(copy)
            self._seen[copy.source.path] = copy.source
            self._failures.pop(copy.source.path, None)
            copy.staged.unlink(missing_ok=True)
            changed = True
        return changed

    async def _stage(
        self, source: SandboxFile, target: Path, *, append: bool, final: bool
    ) -> _Copy | None:
        # Without file identity an offset can belong to a replaced file.
        append = append and source.file_id is not None
        previous = self._seen.get(source.path)
        size = target.stat().st_size if target.exists() else 0
        same_file = previous is not None and previous.file_id == source.file_id
        if append and same_file and size <= source.size:
            # Same-size rewrites are not an append, even if the inode is unchanged.
            if size == source.size and previous.mtime_ns == source.mtime_ns:
                self._failures.pop(source.path, None)
                return None
            offset = size if size < source.size else 0
        else:
            offset = 0
        if not append and previous == source and target.exists():
            self._failures.pop(source.path, None)
            return None
        end = source.size
        if append and not final:
            end = min(end, offset + _POLL_FILE_BYTES)
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.pull")
        try:
            with staged.open("wb") as output:
                cursor = offset
                while cursor < end:
                    data = await self.sandbox.read_file(
                        source.path, offset=cursor, length=min(_CHUNK_BYTES, end - cursor)
                    )
                    if not data:
                        raise OSError("source ended before its observed size")
                    output.write(data)
                    cursor += len(data)
            return _Copy(source, target, staged, offset, append)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def _commit(self, copy: _Copy) -> None:
        if copy.offset:
            # Only the new suffix is staged, so unchanged history is never recopied.
            with copy.target.open("ab") as output, copy.staged.open("rb") as incoming:
                shutil.copyfileobj(incoming, output, length=_CHUNK_BYTES)
        else:
            if copy.append and copy.target.exists() and copy.target.stat().st_size:
                self._generation += 1
                archive = self.logs_dir / "superseded" / copy.target.relative_to(self.logs_dir)
                archive = archive.with_name(f"{archive.name}.{self._generation}")
                archive.parent.mkdir(parents=True, exist_ok=True)
                copy.target.replace(archive)
            os.replace(copy.staged, copy.target)

    def _failed(self, source: str, detail: str) -> None:
        if self._failures.get(source) != detail:
            _LOGGER.warning(
                "CLI evidence collection will retry",
                extra={"ale_data": {"source": source, "error": detail}},
            )
        self._failures[source] = detail


def _stable(copy: _Copy, current: SandboxFile) -> bool:
    if current.file_id != copy.source.file_id:
        return False
    if copy.append:
        return current.size >= copy.source.size and (
            current.size > copy.source.size or current.mtime_ns == copy.source.mtime_ns
        )
    return current == copy.source


def _matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern) or (
        "**/" in pattern and _matches(path, pattern.replace("**/", "", 1))
    )
