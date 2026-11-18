"""Many TCP streams over one byte pipe, so a sandbox can reach the Host without a route to it.

A sandbox on a cloud platform has no path back to the Host's Gateway: the platform lets the
Host open connections in and refuses to carry connections out. The Host therefore opens one
long-lived pipe into the sandbox, and this module turns it into streams in both directions:

* the **listener** side (in the sandbox) accepts a local connection and sends ``OPEN`` with a
  small target index;
* the **dialer** side (on the Host) connects that index to an address it chose in advance.

The sandbox can only name an index into the Host's own list, never an address, so a process
inside cannot make the Host connect anywhere it was not told to.

Framing is ``type:1 stream:4 length:4`` followed by the payload. Each stream has a send
window that the receiver refills as it writes to its socket, so one slow stream never makes
the reader of the shared pipe wait and never grows an unbounded buffer.

The sandbox side listens on loopback, at ``127.0.0.1`` unless ``--listen`` names another
address, so that sandboxes sharing one machine's loopback each listen on an address of
their own. On Windows it binds exclusively: a port bound for reuse there can be bound again
by another account's socket, which would take the connections meant for this one. Where
several accounts share the loopback, the side that starts the tunnel may bind the socket
itself and hand it over (``--accept``), so that the port stays taken between two tunnels.

Standard library only, and no engine imports: this file is copied into images (as
``/opt/ale/guestd/tunnel.py``) and imported by the Host from the same source.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import struct
import sys
import threading
from collections.abc import Awaitable, Callable

__all__ = ["Mux", "TunnelError"]

OPEN, DATA, EOF, CLOSE, CREDIT, READY = range(1, 7)
_HEADER = struct.Struct("!BII")
CHUNK_BYTES = 64 * 1024
#: Bytes a stream may send before the receiver has written any of them out.
WINDOW_BYTES = 256 * 1024
MAX_STREAMS = 256

Dial = Callable[[int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class TunnelError(Exception):
    """The pipe carried something that is not a frame, or ended mid-frame."""


class _Stream:
    def __init__(self, mux: Mux, sid: int) -> None:
        self.mux = mux
        self.sid = sid
        self.inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.queued = 0
        self.window = WINDOW_BYTES
        self.aborted = False
        self.tasks: list[asyncio.Future[None]] = []
        self._credit = asyncio.Event()
        self._credit.set()

    def add_credit(self, count: int) -> None:
        self.window += count
        self._credit.set()

    async def send(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            while self.window <= 0:
                self._credit.clear()
                await self._credit.wait()
            take = min(len(view), self.window, CHUNK_BYTES)
            self.window -= take
            await self.mux.send(DATA, self.sid, bytes(view[:take]))
            view = view[take:]


class Mux:
    """One end of the pipe. ``dial`` makes it a dialer; ``listen`` makes it a listener."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        dial: Dial | None = None,
        max_streams: int = MAX_STREAMS,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._dial = dial
        self._max_streams = max_streams
        self._streams: dict[int, _Stream] = {}
        self._next_stream = 1
        self._write_lock = asyncio.Lock()
        self._background: set[asyncio.Future[None]] = set()
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()

    # --- sending ---------------------------------------------------------------

    async def send(self, kind: int, sid: int, payload: bytes = b"") -> None:
        async with self._write_lock:
            self._writer.write(_HEADER.pack(kind, sid, len(payload)) + payload)
            await self._writer.drain()

    # --- running ---------------------------------------------------------------

    async def run(self) -> None:
        """Read frames until the pipe ends; then end every stream."""
        try:
            while True:
                header = await self._reader.readexactly(_HEADER.size)
                kind, sid, length = _HEADER.unpack(header)
                if length > CHUNK_BYTES:
                    raise TunnelError(f"frame of {length} bytes exceeds the {CHUNK_BYTES} limit")
                payload = await self._reader.readexactly(length) if length else b""
                self._dispatch(kind, sid, payload)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.closed.set()
            for stream in list(self._streams.values()):
                stream.aborted = True
                self._finish(stream)
            with contextlib.suppress(Exception):
                self._writer.close()

    def _dispatch(self, kind: int, sid: int, payload: bytes) -> None:
        if kind == OPEN:
            self._accept(sid, payload)
            return
        if kind == READY:
            self.ready.set()
            return
        stream = self._streams.get(sid)
        if stream is None:
            return  # a frame for a stream that already finished
        if kind == DATA:
            stream.queued += len(payload)
            if stream.queued > 2 * WINDOW_BYTES:
                self._later(self._abort(stream))  # the peer ignored the window
                return
            stream.inbox.put_nowait(payload)
        elif kind == EOF:
            stream.inbox.put_nowait(None)
        elif kind == CLOSE:
            stream.aborted = True
            self._finish(stream)
        elif kind == CREDIT:
            stream.add_credit(int.from_bytes(payload, "big"))
        else:
            raise TunnelError(f"unknown frame type {kind}")

    def _accept(self, sid: int, payload: bytes) -> None:
        target = payload[0] if len(payload) == 1 else -1
        if self._dial is None or sid in self._streams or len(self._streams) >= self._max_streams:
            self._later(self.send(CLOSE, sid))
            return
        stream = _Stream(self, sid)
        self._streams[sid] = stream

        async def serve() -> None:
            try:
                reader, writer = await self._dial(target)  # type: ignore[misc]
            except Exception:
                await self._abort(stream)
                return
            await self._bridge(stream, reader, writer)

        stream.tasks.append(asyncio.ensure_future(serve()))

    def _later(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(self._quietly(coroutine))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    async def _quietly(coroutine: Awaitable[None]) -> None:
        with contextlib.suppress(Exception):
            await coroutine

    # --- listening -------------------------------------------------------------

    async def listen(self, port: int, target: int, host: str = "127.0.0.1") -> asyncio.Server:
        accepted = self._accepted(target)
        if os.name != "nt":
            return await asyncio.start_server(accepted, host, port, reuse_address=True)
        listener = socket.socket()
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((host, port))
            return await asyncio.start_server(accepted, sock=listener)
        except BaseException:
            listener.close()
            raise

    def accept(self, listener: socket.socket, target: int) -> None:
        """Take connections from a listening socket this process was handed, in a thread of
        its own: the socket outlives the process, so it must not be tied to the event loop's
        completion port, which a socket can be only once."""
        loop = asyncio.get_running_loop()
        accepted = self._accepted(target)

        async def bridge(connection: socket.socket) -> None:
            reader, writer = await asyncio.open_connection(sock=connection)
            await accepted(reader, writer)

        def take() -> None:
            with contextlib.suppress(OSError, RuntimeError):  # the socket or the loop closed
                while True:
                    connection, _ = listener.accept()
                    loop.call_soon_threadsafe(self._later, bridge(connection))

        threading.Thread(target=take, daemon=True).start()

    def _accepted(
        self, target: int
    ) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
        async def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if len(self._streams) >= self._max_streams or self.closed.is_set():
                writer.close()
                return
            sid = self._next_stream
            self._next_stream += 1
            stream = _Stream(self, sid)
            self._streams[sid] = stream
            try:
                await self.send(OPEN, sid, bytes([target]))
            except Exception:
                self._finish(stream)
                writer.close()
                return
            await self._bridge(stream, reader, writer)

        return accepted

    # --- one stream ------------------------------------------------------------

    async def _bridge(
        self, stream: _Stream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        up = asyncio.ensure_future(self._upstream(stream, reader))
        down = asyncio.ensure_future(self._downstream(stream, writer))
        stream.tasks.extend([up, down])
        failed = False
        try:
            done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_EXCEPTION)
            failed = any(task.exception() for task in done if not task.cancelled())
        finally:
            for task in (up, down):
                task.cancel()
            with contextlib.suppress(Exception):
                writer.close()
            if failed or stream.aborted:
                await self._abort(stream)
            else:
                self._finish(stream)

    async def _upstream(self, stream: _Stream, reader: asyncio.StreamReader) -> None:
        """Local socket to the pipe."""
        while data := await reader.read(CHUNK_BYTES):
            await stream.send(data)
        await self.send(EOF, stream.sid)

    async def _downstream(self, stream: _Stream, writer: asyncio.StreamWriter) -> None:
        """The pipe to the local socket, returning window as bytes are written."""
        while True:
            data = await stream.inbox.get()
            if data is None:
                if writer.can_write_eof():
                    writer.write_eof()
                return
            stream.queued -= len(data)
            writer.write(data)
            await writer.drain()
            await self.send(CREDIT, stream.sid, len(data).to_bytes(4, "big"))

    async def _abort(self, stream: _Stream) -> None:
        if stream.sid in self._streams and not self.closed.is_set():
            with contextlib.suppress(Exception):
                await self.send(CLOSE, stream.sid)
        self._finish(stream)

    def _finish(self, stream: _Stream) -> None:
        self._streams.pop(stream.sid, None)
        for task in stream.tasks:
            if task is not asyncio.current_task():
                task.cancel()


# --- the sandbox side ----------------------------------------------------------


async def _stdio() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if os.name == "nt":
        # The Proactor loop reads and writes a pipe with overlapped I/O, which the
        # synchronous anonymous pipes a Windows process is given for stdio do not take.
        return _threaded_pipe(sys.stdin.fileno(), sys.stdout.fileno())
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)


def _threaded_pipe(
    incoming: int, outgoing: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A pipe's two descriptors as a reader and a writer, read and written by threads."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()

    def pump() -> None:
        with contextlib.suppress(RuntimeError):  # the loop closed first
            try:
                while block := os.read(incoming, CHUNK_BYTES):
                    loop.call_soon_threadsafe(reader.feed_data, block)
            except OSError:
                pass
            loop.call_soon_threadsafe(reader.feed_eof)

    threading.Thread(target=pump, daemon=True).start()
    return reader, _ThreadedWriter(outgoing)  # type: ignore[return-value]


class _ThreadedWriter:
    """A descriptor as the writer a :class:`Mux` runs over, written whole by a worker thread."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        self._pending = bytearray()

    def write(self, data: bytes) -> None:
        self._pending += data

    async def drain(self) -> None:
        if self._pending:
            data, self._pending = bytes(self._pending), bytearray()
            await asyncio.to_thread(self._write, memoryview(data))

    def _write(self, data: memoryview) -> None:
        while data:
            data = data[os.write(self._descriptor, data) :]

    def close(self) -> None:
        """Nothing: the descriptor is the process's own and ends with it."""


async def _serve(listeners: list[tuple[str, int, int]], sockets: list[tuple[int, int]]) -> int:
    reader, writer = await _stdio()
    mux = Mux(reader, writer)
    servers = []
    try:
        for host, port, target in listeners:
            servers.append(await mux.listen(port, target, host))
    except OSError as exc:
        print(f"tunnel: cannot listen on {host}:{port}: {exc}", file=sys.stderr, flush=True)
        return 1
    for descriptor, target in sockets:
        mux.accept(socket.socket(fileno=descriptor), target)
    await mux.send(READY, 0)
    await mux.run()
    for server in servers:
        server.close()
    return 0


def _listener(spec: str) -> tuple[str, int, int]:
    """``[HOST:]PORT:TARGET``: where to listen, on ``127.0.0.1`` unless HOST names another
    address, and the index of the Host's target that each connection goes to."""
    parts = spec.split(":")
    host = parts.pop(0) if len(parts) == 3 else "127.0.0.1"
    try:
        port, target = (int(part) for part in parts)
    except ValueError:  # not two numbers
        port = target = -1
    if not host or not 0 <= port <= 65535 or not 0 <= target <= 255:
        raise argparse.ArgumentTypeError(f"not [HOST:]PORT:TARGET with TARGET below 256: {spec!r}")
    return host, port, target


def _socket(spec: str) -> tuple[int, int]:
    """``SOCKET:TARGET``: a listening socket this process inherited, and the Host's target."""
    try:
        descriptor, target = (int(part) for part in spec.split(":"))
    except ValueError:  # not two numbers
        descriptor = target = -1
    if descriptor < 0 or not 0 <= target <= 255:
        raise argparse.ArgumentTypeError(f"not SOCKET:TARGET with TARGET below 256: {spec!r}")
    return descriptor, target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ale-tunnel", description="ALE sandbox tunnel")
    parser.add_argument(
        "--listen",
        action="append",
        default=[],
        type=_listener,
        metavar="[HOST:]PORT:TARGET",
        help="accept connections on HOST (127.0.0.1) at PORT and send them to the Host's TARGET",
    )
    parser.add_argument(
        "--accept",
        action="append",
        default=[],
        type=_socket,
        metavar="SOCKET:TARGET",
        help="accept connections on the listening SOCKET this process inherited, which its "
        "parent keeps bound, and send them to the Host's TARGET",
    )
    args = parser.parse_args(argv)
    if not args.listen and not args.accept:
        parser.error("name at least one --listen or --accept")
    return asyncio.run(_serve(args.listen, args.accept))


if __name__ == "__main__":
    raise SystemExit(main())
