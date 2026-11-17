"""The OOM kills in a Sandbox that nothing outside it records.

A cgroup counts in ``memory.events``, from its creation on, each time its memory reached its
limit (``oom``) and each of its processes an OOM killer ended (``oom_kill``), whichever
killer. Under Kata only the guest's kernel sees the container's processes, and on Daytona
nothing outside the sandbox says why it was lost; there a watcher in the Sandbox reads its
cgroup's ``memory.events`` at every change and writes it to a session of its own, whose
last line ALE keeps.
"""

from __future__ import annotations

import asyncio
import contextlib

from ale.core.errors import ProviderStartError

__all__ = ["WATCHER", "GuestEvents"]

#: Writes its cgroup's ``memory.events`` on one line at every change, until its standard
#: input closes; run as root on the framework's Python, isolated.
WATCHER = """
import os, select, sys
events = os.open("/sys/fs/cgroup/memory.events", os.O_RDONLY)
poller = select.poll()
poller.register(events, select.POLLPRI | select.POLLERR)
poller.register(0, select.POLLIN | select.POLLHUP)
last = None
while True:
    raw = os.pread(events, 4096, 0)
    if raw != last:
        sys.stdout.write(" ".join(raw.decode().split()) + "\\n")
        sys.stdout.flush()
        last = raw
    if any(fd == 0 for fd, _ in poller.poll()):
        break
"""


class GuestEvents:
    """A Sandbox's ``memory.events`` inside it, which :data:`WATCHER` writes at every change
    to a session of its own. Its session runs beside guestd's, so a change the kernel made
    just before guestd's session ended can arrive after it."""

    def __init__(self, process: asyncio.subprocess.Process, counts: dict[str, int]) -> None:
        self._process = process
        self._counts = counts
        self._reading = asyncio.ensure_future(self._read())

    @classmethod
    async def start(cls, argv: list[str]) -> GuestEvents:
        """The watcher that ``argv`` runs in the Sandbox."""
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        try:
            first = await asyncio.wait_for(process.stdout.readline(), timeout=60)
            counts = _pairs(first)
        except (TimeoutError, ValueError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            stderr = await process.stderr.read() if process.stderr is not None else b""
            raise ProviderStartError(
                f"could not watch the guest's memory: {stderr.decode(errors='replace').strip()}"
            ) from None
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise
        return cls(process, counts)

    async def _read(self) -> None:
        assert self._process.stdout is not None
        while line := await self._process.stdout.readline():
            with contextlib.suppress(ValueError):
                self._counts = _pairs(line)

    async def settled(self, timeout_sec: float) -> None:
        """Wait, up to ``timeout_sec``, for the watcher's session to end, as it does with
        its Sandbox, so that what it wrote before has arrived."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._reading), timeout_sec)

    def oom_killed(self) -> str | None:
        """What it counted since the Sandbox started, once an OOM killer ended a process
        there or its memory reached its limit, or None. ``oom`` tells its own limit from
        memory that ran out around it."""
        killed, reached = self._counts.get("oom_kill", 0), self._counts.get("oom", 0)
        if not killed and not reached:
            return None
        return (
            f"its cgroup counted {killed} OOM kills and {reached} times its memory reached "
            "its limit"
        )

    async def close(self) -> None:
        self._reading.cancel()
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                assert self._process.stdin is not None
                self._process.stdin.close()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._process.kill()


def _pairs(raw: bytes) -> dict[str, int]:
    """``memory.events`` as the watcher writes it, every pair on one line."""
    words = raw.decode().split()
    if not words or len(words) % 2:
        raise ValueError(f"not memory.events: {raw!r}")
    return {words[i]: int(words[i + 1]) for i in range(0, len(words), 2)}
