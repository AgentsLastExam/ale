"""Keep a Host copy of a Harness's declared logs while its agent is still running."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path, PurePosixPath, PureWindowsPath

from ale.core.sandbox import Sandbox, SandboxFile
from ale.core.taskspec import OperatingSystem

__all__ = ["FINAL_SYNC_TIMEOUT_SEC", "SYNC_INTERVAL_SEC", "SYNC_TIMEOUT_SEC", "LogMirror"]

SYNC_INTERVAL_SEC = 60.0
SYNC_TIMEOUT_SEC = 30.0
#: The last copy runs after the agent stopped and may have to catch up on a large backlog.
FINAL_SYNC_TIMEOUT_SEC = 300.0
#: Bytes copied from one file before the next file gets a turn.
_PIECE_BYTES = 8 * 1024 * 1024
_LOGGER = logging.getLogger("ale.execution")


class LogMirror:
    """Mirror ``Harness.logs`` below ``target``, transferring only bytes it does not hold.

    While the agent runs, every log is treated as append-only: a sync copies the bytes
    beyond the Host copy, and a file that shrank is copied again from its start. Session
    files are appended to, but a CLI may rewrite a small index or repair a session in
    place, so the final sync, after the agent stopped, compares each file's guest digest
    with its Host copy once and copies a rewritten file again. One check per file and run
    keeps the cost linear in the size of the logs. The rule needs no file identity, so
    Linux and Windows guests behave alike.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        home: str,
        os: OperatingSystem,
        paths: tuple[str, ...],
        target: Path,
    ) -> None:
        self._sandbox = sandbox
        self._path_type = PureWindowsPath if os is OperatingSystem.WINDOWS else PurePosixPath
        self._home = self._path_type(home)
        self._paths = paths
        self._target = target
        self._copies: dict[str, _Copy] = {}
        self._failures: dict[str, str] = {}
        self._reported: dict[str, str] = {}

    @property
    def incomplete(self) -> bool:
        """Whether the latest sync left some declared evidence uncopied."""
        return bool(self._failures)

    async def sync(self, *, final: bool = False) -> None:
        """Copy what the guest appended; the final sync also confirms every copy."""
        self._failures.clear()
        pending: list[tuple[SandboxFile, _Copy]] = []
        for declared in self._paths:
            root = self._home / declared
            try:
                listed = await self._sandbox.list_files(str(root))
            except Exception as exc:
                self._failed(str(root), type(exc).__name__)
                continue
            for file in listed:
                try:
                    relative = self._path_type(file.path).relative_to(root)
                except ValueError:
                    self._failed(file.path, "outside its declared log")
                    continue
                if file.path not in self._copies:
                    self._copies[file.path] = _Copy(
                        self._target.joinpath(*PurePosixPath(declared).parts, *relative.parts)
                    )
                copy = self._copies[file.path]
                if file.size < copy.size:
                    copy.reset()
                if final or file.size > copy.size or not copy.path.exists():
                    pending.append((file, copy))
        await self._pull_all(pending)
        if not final:
            return
        for file, copy in pending:
            if file.path in self._failures:
                continue
            try:
                await self._confirm(file, copy)
            except Exception as exc:
                self._failed(file.path, type(exc).__name__)

    async def _pull_all(self, pending: list[tuple[SandboxFile, _Copy]]) -> None:
        # One piece per file per round, so a large backlog cannot starve the other files.
        while pending:
            remaining = []
            for file, copy in pending:
                try:
                    if await self._pull(file, copy):
                        remaining.append((file, copy))
                except Exception as exc:
                    self._failed(file.path, type(exc).__name__)
            pending = remaining

    async def _pull(self, file: SandboxFile, copy: _Copy) -> bool:
        """Copy one piece of what the Host copy lacks; return whether more remains."""
        length = min(file.size - copy.size, _PIECE_BYTES)
        data = (
            await self._sandbox.read_file(file.path, offset=copy.size, length=length)
            if length
            else b""
        )
        if len(data) != length:
            raise _ChangedError
        copy.append(data)
        return copy.size < file.size

    async def _confirm(self, file: SandboxFile, copy: _Copy) -> None:
        """Copy a rewritten file again; the agent has stopped, so once is enough."""
        if await self._holds(file, copy):
            return
        copy.reset()
        while await self._pull(file, copy):
            pass
        if not await self._holds(file, copy):
            copy.reset()
            self._failed(file.path, "changed while it was copied")

    async def _holds(self, file: SandboxFile, copy: _Copy) -> bool:
        """Whether the guest file still begins with the bytes the Host copy holds."""
        if copy.size == 0:
            return True
        return await self._sandbox.file_digest(file.path, length=copy.size) == copy.hexdigest()

    def _failed(self, source: str, detail: str) -> None:
        self._failures[source] = detail
        if self._reported.get(source) != detail:
            self._reported[source] = detail
            _LOGGER.warning(
                "harness log copy will retry",
                extra={"ale_data": {"source": source, "error": detail}},
            )


class _ChangedError(Exception):
    """The guest file shrank while a piece was being read; the next sync retries it."""


class _Copy:
    """The Host copy of one guest file and the digest of the bytes it holds."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.digest = hashlib.sha256()
        self.size = 0
        # A copy left by an earlier mirror is extended and confirmed like any other.
        if path.is_file():
            with path.open("rb") as stream:
                while block := stream.read(_PIECE_BYTES):
                    self.digest.update(block)
                    self.size += len(block)

    def hexdigest(self) -> str:
        return f"sha256:{self.digest.hexdigest()}"

    def append(self, data: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as stream:
            stream.write(data)
        self.digest.update(data)
        self.size += len(data)

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")
        self.digest = hashlib.sha256()
        self.size = 0
