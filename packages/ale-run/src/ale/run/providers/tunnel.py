"""The Host end of ``guestd/tunnel.py``'s stream tunnel, over a command's standard streams.

A Provider whose Sandboxes cannot reach the Host, such as Daytona's or a Station's, starts
the tunnel's listening end in the Sandbox through a command whose standard streams it holds,
an SSH session, and dials each stream the Sandbox opens to the Host address its index names.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.run.guestd.tunnel import Mux

__all__ = ["Tunnel", "target"]


def target(url: str) -> tuple[str, int]:
    """Where the Host dials the service at ``url`` for the tunnel: its host, or loopback for
    one that listens on every interface, at the port ``url`` must name."""
    parts = urlsplit(url)
    if parts.port is None:
        raise ProviderCapabilityError(f"{url!r} has no explicit port to tunnel")
    host = parts.hostname or "127.0.0.1"
    return ("127.0.0.1" if host in {"0.0.0.0", "::"} else host), parts.port


class Tunnel:
    """The Host end of the stream tunnel; starts its command again if it ends.

    ``argv`` gives the command that runs the listening end, anew for each start, and
    ``targets`` the Host addresses its listeners' indices name.
    """

    def __init__(
        self,
        argv: Callable[[], Awaitable[list[str]]],
        *,
        targets: list[tuple[str, int]],
    ) -> None:
        self._argv = argv
        self._targets = targets
        self._closing = False
        self._first: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self, timeout_sec: float = 120) -> None:
        self._task = asyncio.ensure_future(self._supervise())
        try:
            await asyncio.wait_for(asyncio.shield(self._first), timeout_sec)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self._closing = True
        if self._first.done() and not self._first.cancelled():
            self._first.exception()  # mark retrieved
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _dial(self, index: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        host, port = self._targets[index]
        return await asyncio.open_connection(host, port)

    async def _supervise(self) -> None:
        failures = 0
        detail = ""
        while not self._closing:
            ready = False
            try:
                process = await asyncio.create_subprocess_exec(
                    *await self._argv(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._process = process
                assert process.stdin is not None and process.stdout is not None
                mux = Mux(process.stdout, process.stdin, dial=self._dial)
                running = asyncio.ensure_future(mux.run())
                try:
                    waiting = asyncio.ensure_future(mux.ready.wait())
                    await asyncio.wait(
                        {waiting, running}, timeout=60, return_when=asyncio.FIRST_COMPLETED
                    )
                    waiting.cancel()
                    ready = mux.ready.is_set()
                    if ready:
                        failures = 0
                        if not self._first.done():
                            self._first.set_result(None)
                        await running
                finally:
                    running.cancel()
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                    assert process.stderr is not None
                    detail = (await process.stderr.read()).decode("utf-8", "replace").strip()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
            if self._closing:
                return
            if not ready:
                failures += 1
                if failures >= 3:
                    if not self._first.done():
                        self._first.set_exception(
                            ProviderStartError(f"the sandbox tunnel did not start: {detail[-500:]}")
                        )
                    return
            await asyncio.sleep(min(2**failures, 15))
