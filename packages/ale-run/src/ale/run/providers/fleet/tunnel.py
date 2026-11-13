"""The tunnel that carries a Run's Gateway and egress proxy to its Sandboxes on a Node.

A Node's Provider points its Sandbox at the Gateway the way it would on the Run's own
machine: at a port on the Node. The node agent listens there and carries each connection
over one WebSocket the Run opened to it, to the Run's Gateway or proxy. So the Node's
Provider is unchanged, and the Sandbox only ever reaches an address on its Node.

The streams are ``guestd/tunnel.py``'s, as for Daytona: the Node names only an index into
the Run's own list of addresses, so a Node cannot make the Run connect anywhere else.
The Run opens the WebSocket because the Node can always be reached from the Run, while the
Run's machine need not accept connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType, web

from ale.run.guestd.tunnel import READY, Mux

__all__ = ["NodeListeners", "RunTunnel", "with_port"]

_log = logging.getLogger(__name__)

#: How long a Node waits for a Run's tunnel before it starts the Run's first Sandbox there.
ATTACH_WAIT_SEC = 30.0

WebSocket = web.WebSocketResponse | ClientWebSocketResponse


class _Pipe:
    """A WebSocket as the reader and writer a :class:`Mux` runs over."""

    def __init__(self, socket: WebSocket) -> None:
        self._socket = socket
        self.reader = asyncio.StreamReader(limit=2**20)
        self._buffer = bytearray()
        self._pump = asyncio.ensure_future(self._receive())

    async def _receive(self) -> None:
        try:
            async for message in self._socket:
                if message.type is WSMsgType.BINARY:
                    self.reader.feed_data(message.data)
                elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSE}:
                    break
        finally:
            self.reader.feed_eof()

    def write(self, data: bytes) -> None:
        self._buffer += data

    async def drain(self) -> None:
        if self._buffer:
            data, self._buffer = bytes(self._buffer), bytearray()
            await self._socket.send_bytes(data)

    def close(self) -> None:
        self._pump.cancel()
        self._closing = asyncio.ensure_future(self._socket.close())


class NodeListeners:
    """The ports on a Node where one Run's Sandboxes reach that Run's addresses.

    A port stays the same while the Run has Sandboxes here, since a Sandbox's Provider
    pointed it there when it started. When the Run's tunnel drops and opens again, the
    ports listen again on the new one; a connection in between is refused, and the Sandbox's
    client connects again, as through Daytona's tunnel.
    """

    def __init__(self, run_id: str, *, host: str = "0.0.0.0") -> None:
        self.run_id = run_id
        self._host = host
        self._ports: dict[int, int] = {}
        self._servers: list[asyncio.Server] = []
        self._mux: Mux | None = None
        self._attached = asyncio.Event()
        self._opening = asyncio.Lock()

    @property
    def attached(self) -> bool:
        return self._attached.is_set()

    def keep(self, ports: dict[int, int]) -> None:
        """Listen on these ports again, which an earlier agent gave this Run's Sandboxes,
        once the Run's tunnel opens."""
        self._ports.update(ports)

    async def port(self, target: int) -> int:
        """The Node port that leads to the Run's ``target``; the Run's tunnel must be open.

        Opening is serialized with re-attaching, so every port is recorded and listens again
        on each new tunnel.
        """
        if target in self._ports:
            return self._ports[target]
        try:
            await asyncio.wait_for(self._attached.wait(), ATTACH_WAIT_SEC)
        except TimeoutError:
            raise ConnectionError(f"Run {self.run_id} opened no tunnel to this Node") from None
        async with self._opening:
            if target not in self._ports:
                if self._mux is None:
                    raise ConnectionError(f"the tunnel of Run {self.run_id} closed")
                server = await self._mux.listen(0, target, self._host)
                self._servers.append(server)
                self._ports[target] = server.sockets[0].getsockname()[1]
        return self._ports[target]

    async def attach(self, socket: web.WebSocketResponse) -> None:
        """Carry streams over ``socket`` until it closes; a newer tunnel replaces this one."""
        pipe = _Pipe(socket)
        mux = Mux(pipe.reader, pipe)  # type: ignore[arg-type]
        async with self._opening:
            await self._close_servers()
            self._mux = mux
            for target, port in self._ports.items():
                self._servers.append(await mux.listen(port, target, self._host))
            self._attached.set()
        try:
            await mux.send(READY, 0)
            await mux.run()
        finally:
            if self._mux is mux:
                self._mux = None
                self._attached.clear()

    async def _close_servers(self) -> None:
        servers, self._servers = self._servers, []
        for server in servers:
            server.close()
            await server.wait_closed()

    async def close(self) -> None:
        await self._close_servers()
        self._ports.clear()


#: The longest a Run waits between attempts to open its tunnel to a Node again.
RECONNECT_DELAY_SEC = 4.0


class RunTunnel:
    """The Run's end of its tunnel to one Node; it reconnects when the WebSocket drops.

    ``targets`` grows as the Run's Sandboxes name new addresses; an index, once given, keeps
    its address.
    """

    def __init__(self, session: ClientSession, url: str, *, headers: dict[str, str]) -> None:
        self._session = session
        self._url = url
        self._headers = headers
        self._targets: list[tuple[str, int]] = []
        self._ready = asyncio.Event()
        self._closing = False
        self._task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None

    def target(self, host: str, port: int) -> int:
        """The index the Node names to reach ``host:port`` on the Run's machine."""
        address = ("127.0.0.1" if host in {"0.0.0.0", "::", ""} else host, port)
        if address not in self._targets:
            if len(self._targets) >= 256:
                raise ValueError("a Run's tunnel carries at most 256 addresses")
            self._targets.append(address)
        return self._targets.index(address)

    async def start(self, timeout_sec: float = 30.0) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._supervise())
        waiting = asyncio.ensure_future(self._ready.wait())
        try:
            await asyncio.wait(
                {waiting, self._task}, timeout=timeout_sec, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            waiting.cancel()
        if not self._ready.is_set():
            raise ConnectionError(f"the tunnel to {self._url} did not open: {self._failure}")

    async def _dial(self, index: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(*self._targets[index])

    async def _supervise(self) -> None:
        delay = 1.0
        dropped = False
        while not self._closing:
            try:
                async with self._session.ws_connect(
                    self._url, headers=self._headers, heartbeat=30
                ) as socket:
                    pipe = _Pipe(socket)
                    mux = Mux(pipe.reader, pipe, dial=self._dial)  # type: ignore[arg-type]
                    running = asyncio.ensure_future(mux.run())
                    try:
                        await asyncio.wait_for(mux.ready.wait(), 30)
                        self._ready.set()
                        delay = 1.0
                        if dropped:
                            _log.info("tunnel to %s reconnected", self._url)
                            dropped = False
                        await running
                    finally:
                        running.cancel()
                        self._ready.clear()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._failure = error
                if not dropped:
                    _log.warning("tunnel to %s dropped: %s; reconnecting", self._url, error)
                dropped = True
            if not self._closing:
                await asyncio.sleep(delay)
                # A node agent that restarts is back within seconds, with its Sandboxes.
                delay = min(delay * 2, RECONNECT_DELAY_SEC)

    async def close(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task


def with_port(url: str, port: int) -> str:
    """``url`` at another port, so a Node's Provider rewrites its host as it would locally."""
    parts = urlsplit(url)
    host = parts.hostname or "0.0.0.0"
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return parts._replace(netloc=netloc).geturl()
