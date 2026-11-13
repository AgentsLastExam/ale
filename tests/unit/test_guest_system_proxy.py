"""guestd's relay to the Host proxy, behind a Windows agent's system proxy."""

from __future__ import annotations

import asyncio
import socket

import pytest

from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.session import GatewaySession, SessionRegistry
from ale.run.guestd import egress

pytestmark = pytest.mark.unit


def _tunnel(relay: egress.Relay, target: str) -> tuple[str, bytes]:
    """CONNECT through the relay as a browser does, without credentials."""
    host, _, port = relay.address.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=10) as client:
        client.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        reply = client.makefile("rb")
        status = reply.readline().decode().strip()
        while reply.readline() not in (b"\r\n", b""):
            pass
        if status.split()[1] != "200":
            return status, b""
        client.sendall(b"hello")
        return status, reply.read(5)


@pytest.mark.asyncio
async def test_the_relay_opens_tunnels_for_clients_that_carry_no_token() -> None:
    """The relay adds the episode token; the Host proxy still decides every connection."""

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.readexactly(5))
        await writer.drain()
        writer.close()

    destination = await asyncio.start_server(echo, "127.0.0.1", 0)
    target = f"127.0.0.1:{destination.sockets[0].getsockname()[1]}"
    sessions = SessionRegistry()
    session = sessions.open(
        GatewaySession(episode_id="e", model="m", allowed_hosts=frozenset({"127.0.0.1"}))
    )
    proxy = EgressProxy(sessions, host="127.0.0.1")
    await proxy.start()
    relay = egress.Relay(f"http://ale:{session.token}@127.0.0.1:{proxy.port}")
    try:
        status, echoed = await asyncio.to_thread(_tunnel, relay, target)
        assert status.split()[1] == "200", status
        assert echoed == b"hello"
        refused, _ = await asyncio.to_thread(_tunnel, relay, "pypi.org:443")
        assert refused.split()[1] == "403", refused
    finally:
        relay.close()
        await proxy.stop()
        destination.close()
        await destination.wait_closed()


def test_restore_gives_back_the_settings_from_before_the_first_seal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation replaces the relay; the account's own settings still come back once."""
    own = (1, "10.0.0.9:3128", None)
    written: list[tuple[int, str | None, str | None]] = []
    monkeypatch.setattr(egress, "_relay", None)
    monkeypatch.setattr(egress, "_saved", None)
    # What the account has now is whatever was written last.
    monkeypatch.setattr(egress, "_read_settings", lambda: written[-1] if written else own)
    monkeypatch.setattr(egress, "_write_settings", lambda *settings: written.append(settings))
    try:
        first = egress.apply("http://ale:one@127.0.0.1:9", ["172.30.0.1", "localhost"])
        second = egress.apply("http://ale:two@127.0.0.1:9", ["172.30.0.1", "localhost"])
    finally:
        egress.restore()
    egress.restore()

    assert first != second
    assert written == [
        (3, first, "172.30.0.1;localhost"),
        (3, second, "172.30.0.1;localhost"),
        own,
    ]
