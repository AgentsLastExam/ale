"""The OOM kills in a Sandbox that nothing outside it records.

A cgroup counts in ``memory.events``, from its creation on, each time its memory reached its
limit (``oom``) and each of its processes an OOM killer ended (``oom_kill``), whichever
killer. Under Kata only the guest's kernel sees the container's processes; under gVisor the
Sentry kills to keep the sandbox below its cgroup's limit on the Host and counts those kills
in ``oom_kill`` of cgroup v1's ``memory.oom_control``, of which neither the Host's cgroup nor
Docker learns; and on Daytona nothing outside the sandbox says why it was lost. There a
watcher in the Sandbox reads that count at every change and writes it to a session of its
own, whose last line ALE keeps.
"""

from __future__ import annotations

import asyncio
import contextlib

from ale.core.errors import ProviderStartError

__all__ = ["WATCHER", "GuestEvents"]

#: Writes the file of counts it is given on one line at every change, until its standard
#: input closes; run as root on the framework's Python, isolated. It reads again at every
#: change the kernel announces, as Linux does for ``memory.events``, and every second for a
#: file whose changes nothing announces, as the Sentry's ``memory.oom_control``. It asks
#: never to be chosen by an OOM killer, which the Sentry grants and Linux grants only to
#: ``CAP_SYS_RESOURCE``, so that it reports the kills that follow.
WATCHER = """
import os, select, sys
try:
    with open("/proc/self/oom_score_adj", "w") as adj:
        adj.write("-1000")
except OSError:
    pass
events = os.open(sys.argv[1], os.O_RDONLY)
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
    if any(fd == 0 for fd, _ in poller.poll(1000)):
        break
"""


class GuestEvents:
    """A Sandbox's memory counts inside it, which :data:`WATCHER` writes at every change to a
    session of its own. Its session runs beside guestd's, so a change the kernel made just
    before guestd's session ended can arrive after it."""

    def __init__(
        self, process: asyncio.subprocess.Process, counts: dict[str, int], counter: str
    ) -> None:
        self._process = process
        self._counts = counts
        #: What keeps the counts, as a lost Sandbox's message names it.
        self._counter = counter
        self._reading = asyncio.ensure_future(self._read())

    @classmethod
    async def start(cls, argv: list[str], *, counter: str) -> GuestEvents:
        """The watcher that ``argv`` runs in the Sandbox, on counts that ``counter`` keeps,
        such as its cgroup or the Sentry."""
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
        return cls(process, counts, counter)

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
        there or its memory reached its limit, or None. A cgroup's ``oom`` tells its own
        limit from memory that ran out around it; the Sentry counts no ``oom``, and kills
        only for the sandbox's own limit."""
        killed, reached = self._counts.get("oom_kill", 0), self._counts.get("oom", 0)
        if not killed and not reached:
            return None
        if "oom" not in self._counts:
            return f"{self._counter} counted {killed} OOM kills"
        return (
            f"{self._counter} counted {killed} OOM kills and {reached} times its memory "
            "reached its limit"
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
    """Counts as the watcher writes them, every pair on one line."""
    words = raw.decode().split()
    if not words or len(words) % 2:
        raise ValueError(f"not memory.events: {raw!r}")
    return {words[i]: int(words[i + 1]) for i in range(0, len(words), 2)}
