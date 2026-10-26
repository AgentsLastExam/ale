"""The sandbox tunnel: streams in both directions over one pipe, bounded and contained."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from ale.run.guestd.tunnel import CHUNK_BYTES, WINDOW_BYTES, Mux

pytestmark = pytest.mark.unit


@dataclass
class Rig:
    host: Mux
    guest: Mux
    ports: dict[int, int]
    dialed: list[int] = field(default_factory=list)
    listening: dict[int, int] = field(default_factory=dict)


async def _end(sock: socket.socket) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(sock=sock)


@contextlib.asynccontextmanager
async def rig(targets: dict[int, int], *, max_streams: int = 256) -> AsyncIterator[Rig]:
    """A listener (the sandbox) wired to a dialer (the Host) by a socket pair."""
    left, right = socket.socketpair()
    guest_reader, guest_writer = await _end(left)
    host_reader, host_writer = await _end(right)
    state = Rig(host=None, guest=None, ports=targets)  # type: ignore[arg-type]

    async def dial(index: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        state.dialed.append(index)
        return await asyncio.open_connection("127.0.0.1", targets[index])

    state.host = Mux(host_reader, host_writer, dial=dial, max_streams=max_streams)
    state.guest = Mux(guest_reader, guest_writer)
    runners = [asyncio.ensure_future(state.host.run()), asyncio.ensure_future(state.guest.run())]
    servers: list[asyncio.Server] = []

    async def listen(target: int) -> int:
        server = await state.guest.listen(0, target)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        state.listening[target] = port
        return port

    state.listen = listen  # type: ignore[attr-defined]
    try:
        yield state
    finally:
        for server in servers:
            server.close()
        for writer in (host_writer, guest_writer):
            writer.close()
        for runner in runners:
            runner.cancel()
        await asyncio.gather(*runners, return_exceptions=True)


async def serve(handler) -> tuple[asyncio.Server, int]:  # type: ignore[no-untyped-def]
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    writer.close()


async def test_a_stream_carries_bytes_both_ways() -> None:
    server, port = await serve(echo)
    async with rig({0: port}) as r:
        local = await r.listen(0)  # type: ignore[attr-defined]
        reader, writer = await asyncio.open_connection("127.0.0.1", local)
        writer.write(b"hello through the tunnel")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(24), 5) == b"hello through the tunnel"
        writer.close()
    server.close()


async def test_large_transfers_in_both_directions_arrive_intact_under_flow_control() -> None:
    size = 12 * 1024 * 1024
    upload, download = os.urandom(size), os.urandom(size)
    received = bytearray()
    done = asyncio.Event()

    async def target(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def drain() -> None:
            while data := await reader.read(65536):
                received.extend(data)
                await asyncio.sleep(0)  # a consumer slower than the producer
            done.set()

        reading = asyncio.ensure_future(drain())
        for start in range(0, size, 1 << 20):
            writer.write(download[start : start + (1 << 20)])
            await writer.drain()
        await reading
        writer.close()

    server, port = await serve(target)
    async with rig({0: port}) as r, asyncio.timeout(60):
        local = await r.listen(0)  # type: ignore[attr-defined]
        reader, writer = await asyncio.open_connection("127.0.0.1", local)
        got = bytearray()

        async def pull() -> None:
            while len(got) < size:
                got.extend(await reader.read(65536))

        pulling = asyncio.ensure_future(pull())
        for start in range(0, size, 1 << 20):
            writer.write(upload[start : start + (1 << 20)])
            await writer.drain()
        writer.write_eof()
        await asyncio.gather(pulling, done.wait())
        assert hashlib.sha256(got).digest() == hashlib.sha256(download).digest()
        assert hashlib.sha256(received).digest() == hashlib.sha256(upload).digest()
    server.close()


async def test_many_streams_run_at_once_without_mixing_their_bytes() -> None:
    server, port = await serve(echo)
    async with rig({0: port}) as r:
        local = await r.listen(0)  # type: ignore[attr-defined]

        async def one(index: int) -> bool:
            reader, writer = await asyncio.open_connection("127.0.0.1", local)
            payload = os.urandom(200_000) + str(index).encode()
            writer.write(payload)
            await writer.drain()
            echoed = await asyncio.wait_for(reader.readexactly(len(payload)), 30)
            writer.close()
            return echoed == payload

        assert all(await asyncio.gather(*(one(index) for index in range(64))))
    server.close()


async def test_half_close_reaches_the_target_and_the_reply_still_returns() -> None:
    async def target(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.read()  # until the client's EOF
        writer.write(b"reply to " + request)
        await writer.drain()
        writer.close()

    server, port = await serve(target)
    async with rig({0: port}) as r:
        local = await r.listen(0)  # type: ignore[attr-defined]
        reader, writer = await asyncio.open_connection("127.0.0.1", local)
        writer.write(b"request")
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), 5) == b"reply to request"
    server.close()


async def test_a_target_that_cannot_be_dialed_closes_the_local_connection() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    refused = probe.getsockname()[1]
    probe.close()  # nothing listens here any more
    async with rig({0: refused}) as r:
        local = await r.listen(0)  # type: ignore[attr-defined]
        reader, writer = await asyncio.open_connection("127.0.0.1", local)
        writer.write(b"anyone there?")
        assert await asyncio.wait_for(reader.read(), 5) == b""


async def test_the_sandbox_cannot_make_the_host_dial_an_address_it_names() -> None:
    reached: list[bytes] = []

    async def decoy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        reached.append(await reader.read(1))
        writer.close()

    server, port = await serve(decoy)
    async with rig({0: port}) as r:
        local = await r.listen(5)  # type: ignore[attr-defined]  # an index the Host never listed
        reader, writer = await asyncio.open_connection("127.0.0.1", local)
        writer.write(b"x")
        assert await asyncio.wait_for(reader.read(), 5) == b""
        assert r.dialed == [5]  # asked for, and the Host's own table had no such entry
    assert reached == []
    server.close()


async def test_a_target_that_hangs_up_ends_the_stream() -> None:
    async def hang_up(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server, port = await serve(hang_up)
    async with rig({0: port}) as r:
        local = await r.listen(0)  # type: ignore[attr-defined]
        reader, _ = await asyncio.open_connection("127.0.0.1", local)
        assert await asyncio.wait_for(reader.read(), 5) == b""
    server.close()


async def test_streams_beyond_the_limit_are_refused() -> None:
    server, port = await serve(echo)
    async with rig({0: port}, max_streams=2) as r:
        local = await r.listen(0)  # type: ignore[attr-defined]
        kept = []
        for _ in range(2):
            reader, writer = await asyncio.open_connection("127.0.0.1", local)
            writer.write(b"ping")
            await asyncio.wait_for(reader.readexactly(4), 5)
            kept.append((reader, writer))
        reader, writer = await asyncio.open_connection("127.0.0.1", local)
        writer.write(b"ping")
        assert await asyncio.wait_for(reader.read(), 5) == b""
        for _, held in kept:
            held.close()
    server.close()


async def test_the_pipe_ending_closes_every_stream_and_stops_the_mux() -> None:
    server, port = await serve(echo)
    async with rig({0: port}) as r:
        local = await r.listen(0)  # type: ignore[attr-defined]
        reader, writer = await asyncio.open_connection("127.0.0.1", local)
        writer.write(b"ping")
        await asyncio.wait_for(reader.readexactly(4), 5)
        r.host._writer.close()  # the SSH session dies
        assert await asyncio.wait_for(reader.read(), 5) == b""
        await asyncio.wait_for(r.guest.closed.wait(), 5)
        await asyncio.wait_for(r.host.closed.wait(), 5)
    server.close()


def test_a_window_holds_several_frames() -> None:
    assert WINDOW_BYTES >= 2 * CHUNK_BYTES
