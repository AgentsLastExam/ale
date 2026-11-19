"""guestd's relay to the Host proxy, and the agent's system proxy that names it."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
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
        GatewaySession(
            episode_id="e", model="", purpose="egress", allowed_hosts=frozenset({"127.0.0.1"})
        )
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


def test_a_tunnel_to_the_proxy_probes_itself_when_quiet() -> None:
    """A Mac's Softnet forgets a VM's connection that carried nothing for five minutes."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        connection = socket.create_connection(server.getsockname())
        with connection:
            egress._keep_alive(connection)
            assert connection.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            idle = getattr(socket, "TCP_KEEPALIVE", None) or socket.TCP_KEEPIDLE
            assert connection.getsockopt(socket.IPPROTO_TCP, idle) == egress._IDLE_SEC


@pytest.fixture
def account(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Account:
    account = _Account()
    monkeypatch.setattr(egress, "_relay", None)
    monkeypatch.setattr(egress, "_KEYED", False)  # three settings by position, as Windows'
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


def test_keyed_settings_go_back_only_where_they_still_hold_the_relays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As macOS's network services: one turned off meanwhile is left off, one turned on is
    left alone, and a proxy the agent pointed elsewhere keeps the agent's server."""
    monkeypatch.setattr(egress, "_KEYED", True)
    own = [["Ethernet", "webproxy", [False, "", 0]], ["Wi-Fi", "webproxy", [False, "", 0]]]
    relayed = [
        ["Ethernet", "webproxy", [True, "127.0.0.1", 9]],
        ["Wi-Fi", "webproxy", [True, "127.0.0.1", 9]],
    ]
    current = [
        ["Ethernet", "webproxy", [True, "127.0.0.1", 9]],
        ["USB", "webproxy", [True, "10.0.0.1", 3128]],
    ]
    assert egress._unchanged(own, relayed, current) == [own[0]]
    current[0] = ["Ethernet", "webproxy", [True, "10.0.0.2", 8080]]
    assert egress._unchanged(own, relayed, current) == []


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


class _NetworkSetup:
    """macOS's ``networksetup`` over three services, one disabled, as its output reads."""

    def __init__(self) -> None:
        self.services: dict[str, dict[str, Any]] = {
            "Ethernet": {
                "proxybypassdomains": ["*.local", "169.254/16"],
                "webproxy": [False, "", 0],
                "securewebproxy": [False, "", 0],
            },
            "USB 10/100/1000 LAN": {
                "proxybypassdomains": [],
                "webproxy": [True, "proxy.corp", 3128],
                "securewebproxy": [False, "proxy.corp", 3129],
            },
            "Thunderbolt Bridge": {},
        }
        self.writes: list[tuple[str, ...]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "/usr/sbin/networksetup"
        flag, *args = argv[1:]
        if flag == "-listallnetworkservices":
            out = "An asterisk (*) denotes that a network service is disabled.\n"
            out += "Ethernet\nUSB 10/100/1000 LAN\n*Thunderbolt Bridge\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        service = self.services[args[0]]
        kind = flag[4:].removesuffix("state")
        if flag.startswith("-get"):
            if kind == "proxybypassdomains":
                domains = service[kind]
                out = "".join(f"{domain}\n" for domain in domains) or (
                    f"There aren't any bypass domains set on {args[0]}.\n"
                )
            else:
                enabled, server, port = service[kind]
                out = (
                    f"Enabled: {'Yes' if enabled else 'No'}\nServer: {server}\nPort: {port}\n"
                    "Authenticated Proxy Enabled: 0\n"
                )
            return subprocess.CompletedProcess(argv, 0, out, "")
        self.writes.append((flag, *args))
        if kind == "proxybypassdomains":
            service[kind] = [] if args[1:] == ["Empty"] else args[1:]
        elif flag.endswith("state"):
            service[kind][0] = args[1] == "on"
        else:
            service[kind] = [True, args[1], int(args[2])]
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def mac(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _NetworkSetup:
    """guestd's macOS system proxy, over a fake ``networksetup``."""
    networksetup = _NetworkSetup()
    monkeypatch.setattr(egress.subprocess, "run", networksetup)
    monkeypatch.setattr(egress, "_relay", None)
    monkeypatch.setattr(egress, "_CHANGED", tmp_path / "db" / "system-proxy.json")
    monkeypatch.setattr(egress, "_has_settings", lambda: True)
    monkeypatch.setattr(egress, "_read_settings", egress._read_network_proxies)
    monkeypatch.setattr(egress, "_relay_settings", egress._relay_network_proxies)
    monkeypatch.setattr(egress, "_write_settings", egress._write_network_proxies)
    return networksetup


def test_every_enabled_service_names_the_relay_and_gets_its_own_proxies_back(
    mac: _NetworkSetup,
) -> None:
    own = json.loads(json.dumps(mac.services))
    address = egress.apply("http://token:@192.168.64.1:9", BYPASS, desktop=True)
    host, _, port = address.rpartition(":")
    relayed = [True, host, int(port)]
    for name in ("Ethernet", "USB 10/100/1000 LAN"):
        service = mac.services[name]
        assert service == {
            "proxybypassdomains": BYPASS,
            "webproxy": relayed,
            "securewebproxy": relayed,
        }
        # The bypass domains are set first, so a proxy is on only once they are.
        flags = [write[0] for write in mac.writes if write[1] == name]
        assert flags[:3] == ["-setproxybypassdomains", "-setwebproxy", "-setsecurewebproxy"]
    assert mac.services["Thunderbolt Bridge"] == {}
    assert json.loads(egress._CHANGED.read_text())["relay"] == egress._read_network_proxies()

    egress.restore()
    assert mac.services["USB 10/100/1000 LAN"] == own["USB 10/100/1000 LAN"]
    ethernet = mac.services["Ethernet"]
    assert ethernet["proxybypassdomains"] == own["Ethernet"]["proxybypassdomains"]
    # A proxy that had no server is turned off; the relay's address stays behind, unused.
    assert ethernet["webproxy"][0] is False and ethernet["securewebproxy"][0] is False
    assert not egress._CHANGED.exists()


def test_what_the_agent_set_in_a_service_stays_for_verification(mac: _NetworkSetup) -> None:
    egress.apply("http://token:@192.168.64.1:9", BYPASS, desktop=True)
    mac.services["Ethernet"]["securewebproxy"] = [True, "agent.example", 8443]
    mac.services["Ethernet"]["proxybypassdomains"] = ["agent.example"]
    egress.restore()
    assert mac.services["Ethernet"]["securewebproxy"] == [True, "agent.example", 8443]
    assert mac.services["Ethernet"]["proxybypassdomains"] == ["agent.example"]
    assert mac.services["Ethernet"]["webproxy"][0] is False
    assert mac.services["USB 10/100/1000 LAN"]["webproxy"] == [True, "proxy.corp", 3128]


def test_a_service_without_bypass_domains_gets_none_back(mac: _NetworkSetup) -> None:
    mac.services["Ethernet"]["proxybypassdomains"] = []
    egress.apply("http://token:@192.168.64.1:9", BYPASS, desktop=True)
    egress.restore()
    assert ("-setproxybypassdomains", "Ethernet", "Empty") in mac.writes
    assert mac.services["Ethernet"]["proxybypassdomains"] == []


def test_a_failed_networksetup_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        egress.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 4, "", "** Error: denied"),
    )
    with pytest.raises(OSError, match="denied"):
        egress._read_network_proxies()
