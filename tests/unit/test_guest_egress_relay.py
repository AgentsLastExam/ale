"""guestd's relay to the Host proxy, and the agent's system proxy that names it."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.session import GatewaySession, SessionRegistry
from ale.run.guestd import egress

pytestmark = pytest.mark.unit

BYPASS = ["172.30.0.1", "localhost", "127.0.0.1"]


def _tunnel(relay: egress.Relay, target: str, authorization: str = "") -> tuple[str, bytes]:
    """CONNECT through the relay as a browser does, without the token."""
    host, _, port = relay.address.rpartition(":")
    extra = f"Proxy-Authorization: {authorization}\r\n" if authorization else ""
    with socket.create_connection((host, int(port)), timeout=10) as client:
        client.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{extra}\r\n".encode())
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
    relay = egress.Relay(f"http://{session.token}:@127.0.0.1:{proxy.port}")
    try:
        status, echoed = await asyncio.to_thread(_tunnel, relay, target)
        assert status.split()[1] == "200", status
        assert echoed == b"hello"
        refused, _ = await asyncio.to_thread(_tunnel, relay, "pypi.org:443")
        assert refused.split()[1] == "403", refused
        # Credentials a client brings of its own are replaced, not passed on beside the token.
        status, _ = await asyncio.to_thread(_tunnel, relay, target, "Basic Zm9vOmJhcg==")
        assert status.split()[1] == "200", status
    finally:
        relay.close()
        await proxy.stop()
        destination.close()
        await destination.wait_closed()


class _Account:
    """An account's system proxy: three settings, and every write made to them."""

    def __init__(self) -> None:
        self.settings: list[Any] = ["own-server", "own-bypass", "own-flags"]
        self.writes: list[list[Any]] = []

    def write(self, settings: list[Any]) -> None:
        self.settings = list(settings)
        self.writes.append(list(settings))


@pytest.fixture
def account(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Account:
    account = _Account()
    monkeypatch.setattr(egress, "_relay", None)
    monkeypatch.setattr(egress, "_CHANGED", tmp_path / "run" / "system-proxy.json")
    monkeypatch.setattr(egress, "_has_settings", lambda: True)
    monkeypatch.setattr(egress, "_read_settings", lambda: list(account.settings))
    monkeypatch.setattr(
        egress, "_relay_settings", lambda address, bypass: [address, ";".join(bypass), "on"]
    )
    monkeypatch.setattr(egress, "_write_settings", account.write)
    return account


OWN = ["own-server", "own-bypass", "own-flags"]


def test_restore_gives_back_the_settings_from_before_the_first_seal(account: _Account) -> None:
    """A continuation replaces the relay; the account's own settings still come back once."""
    try:
        first = egress.apply("http://one:@127.0.0.1:9", BYPASS, desktop=True)
        second = egress.apply("http://two:@127.0.0.1:9", BYPASS, desktop=True)
    finally:
        egress.restore()
    egress.restore()

    assert first != second
    assert [write[0] for write in account.writes] == [first, second, "own-server"]
    assert account.settings == OWN
    assert not egress._CHANGED.exists()


def test_a_guestd_that_ends_while_sealed_leaves_the_settings_to_the_next(
    account: _Account,
) -> None:
    """A guestd connected over ``docker exec`` ends with its run, sealed or not; the next one
    restores what the account had before the relay, not the relay that is gone."""
    egress.apply("http://one:@127.0.0.1:9", BYPASS, desktop=True)
    egress._relay.close()  # type: ignore[union-attr]
    egress._relay = None  # a new guestd
    egress.apply("http://two:@127.0.0.1:9", BYPASS, desktop=True)
    egress.restore()
    assert account.settings == OWN


def test_restore_keeps_what_the_agent_changed(account: _Account) -> None:
    """A Task may ask the agent to set the proxy itself; verification sees what it set."""
    egress.apply("http://one:@127.0.0.1:9", BYPASS, desktop=True)
    account.settings[1] = "the-agent's-bypass"
    egress.restore()
    assert account.settings == ["own-server", "the-agent's-bypass", "own-flags"]


def test_settings_written_before_they_were_read_back_all_go_back(account: _Account) -> None:
    egress._CHANGED.parent.mkdir(parents=True)
    egress._CHANGED.write_text(json.dumps({"own": OWN, "relay": None}))
    account.settings = ["127.0.0.1:9", "anything", "on"]
    egress.restore()
    assert account.settings == OWN


@pytest.mark.parametrize(("desktop", "settings"), [(False, True), (True, False)])
def test_without_a_desktop_or_its_settings_only_the_relay_runs(
    account: _Account, monkeypatch: pytest.MonkeyPatch, desktop: bool, settings: bool
) -> None:
    monkeypatch.setattr(egress, "_has_settings", lambda: settings)
    address = egress.apply("http://one:@127.0.0.1:9", BYPASS, desktop=desktop)
    egress.restore()
    assert address.startswith("127.0.0.1:")
    assert account.writes == []


@pytest.mark.skipif(sys.platform != "linux", reason="GNOME settings are Linux guestd's")
def test_gnome_names_the_relay_for_http_and_https_and_turns_it_on_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def gsettings(_session: object, *args: str) -> str:
        calls.append(args)
        return "'none'" if args[-1] == "mode" else "''"

    monkeypatch.setattr(egress, "_session", lambda: ({}, {}))
    monkeypatch.setattr(egress, "_gsettings", gsettings)
    egress._write_settings(egress._relay_settings("127.0.0.1:41999", BYPASS))
    own = egress._read_settings()

    proxy = "org.gnome.system.proxy"
    http, https = f"{proxy}.http", f"{proxy}.https"
    assert calls[:6] == [
        ("set", proxy, "ignore-hosts", json.dumps(BYPASS)),
        ("set", http, "host", '"127.0.0.1"'),
        ("set", http, "port", "41999"),
        ("set", https, "host", '"127.0.0.1"'),
        ("set", https, "port", "41999"),
        ("set", proxy, "mode", "'manual'"),
    ]
    assert own[-1] == [proxy, "mode", "'none'"]
    assert calls[6:] == [("get", schema, key) for schema, key, _ in own]


@pytest.mark.skipif(sys.platform != "linux", reason="GNOME settings are Linux guestd's")
def test_a_desktop_without_gsettings_has_no_system_proxy_to_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(egress.shutil, "which", lambda _name: None)
    assert egress._has_settings() is False
