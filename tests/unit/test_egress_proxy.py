"""The allowlist and the blocklist, tested where they are actually decided.

Both lists are enforced here rather than by rules on the host or tools inside an image, so
these are the assertions that matter: an admitted host gets through, everything else does
not, and neither outcome depends on what the sandbox chose to run.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
from collections.abc import AsyncIterator

import pytest
from aiohttp import ClientSession

from orbit.core.taskspec import NetworkMode, NetworkPolicy
from orbit.run.gateway import proxy as proxy_module
from orbit.run.gateway.proxy import EgressProxy
from orbit.run.gateway.server import Gateway
from orbit.run.gateway.session import GatewaySession, SessionRegistry

pytestmark = pytest.mark.unit


def allowlist(*hosts: str) -> NetworkPolicy:
    return NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=hosts)


def blocklist(*hosts: str) -> NetworkPolicy:
    return NetworkPolicy(mode=NetworkMode.BLOCKLIST, blocked_hosts=hosts)


def egress(*hosts: str) -> GatewaySession:
    """An episode's egress session, open for ``hosts``."""
    return GatewaySession(episode_id="e", model="", purpose="egress", policy=allowlist(*hosts))


class TestAllowlistMatching:
    def session(self, *hosts: str) -> GatewaySession:
        return egress(*hosts)

    def test_declared_host_is_allowed(self) -> None:
        assert self.session("example.com").may_reach("example.com")

    def test_a_declared_host_is_that_host_only(self) -> None:
        """A Task reaches below a name by writing ``*.name``."""
        assert not self.session("example.com").may_reach("files.example.com")

    def test_a_pattern_matches_its_name_and_every_name_below_it(self) -> None:
        """As Daytona's allowlist does, so every Provider admits the same hosts."""
        session = self.session("*.example.com")
        assert session.may_reach("files.example.com")
        assert session.may_reach("a.b.example.com")
        assert session.may_reach("example.com")
        assert not session.may_reach("notexample.com")

    def test_a_lookalike_suffix_is_not_allowed(self) -> None:
        """`notexample.com` ends with the declared string and is a different site."""
        assert not self.session("example.com").may_reach("notexample.com")

    def test_the_port_is_ignored_when_matching(self) -> None:
        assert self.session("example.com").may_reach("example.com:8443")

    def test_matching_is_case_and_trailing_dot_insensitive(self) -> None:
        session = self.session("Example.COM")
        assert session.may_reach("EXAMPLE.com")
        assert session.may_reach("example.com.")

    def test_addresses_match_with_their_colons_intact(self) -> None:
        session = self.session("2001:db8::1", "10.0.0.0/8")
        assert session.may_reach("2001:db8::1")
        assert session.may_reach("10.1.2.3:443")
        assert not session.may_reach("2001:db8::2")

    def test_declaring_nothing_allows_nothing(self) -> None:
        """The default is no additional egress, and it cannot be acquired by asking."""
        assert not GatewaySession(episode_id="e", model="", purpose="egress").may_reach(
            "example.com"
        )


class TestProxyEnforcement:
    """Drive the proxy over a real socket; the wire behaviour is the contract."""

    async def connect(self, proxy: EgressProxy, target: str, token: str | None) -> tuple[int, str]:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        if token is not None:
            head += f"Proxy-Authorization: Bearer {token}\r\n"
        writer.write((head + "\r\n").encode())
        await writer.drain()
        status_line = (await reader.readline()).decode().strip()
        writer.close()
        parts = status_line.split(maxsplit=2)
        return int(parts[1]), status_line

    @pytest.fixture
    async def proxy(self):  # type: ignore[no-untyped-def]
        registry = SessionRegistry()
        session = registry.open(egress("example.com"))
        proxy = EgressProxy(registry, host="127.0.0.1")
        await proxy.start()
        try:
            yield proxy, session
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_a_bracketed_address_is_matched_without_its_brackets(self) -> None:
        registry = SessionRegistry()
        session = registry.open(egress("::2"))
        server = EgressProxy(registry, host="127.0.0.1")
        await server.start()
        try:
            allowed, _ = await self.connect(server, "[::2]:1", session.token)
            refused, _ = await self.connect(server, "[::3]:1", session.token)
        finally:
            await server.stop()
        assert (allowed, refused) == (502, 403)

    @pytest.mark.asyncio
    async def test_an_undeclared_host_is_refused(self, proxy) -> None:  # type: ignore[no-untyped-def]
        server, session = proxy
        status, _ = await self.connect(server, "pypi.org:443", session.token)
        assert status == 403

    @pytest.mark.asyncio
    async def test_an_unknown_token_is_refused(self, proxy) -> None:  # type: ignore[no-untyped-def]
        server, _ = proxy
        status, _ = await self.connect(server, "example.com:443", "not-a-real-token")
        assert status == 407

    @pytest.mark.asyncio
    async def test_no_token_at_all_is_refused(self, proxy) -> None:  # type: ignore[no-untyped-def]
        server, _ = proxy
        status, _ = await self.connect(server, "example.com:443", None)
        assert status == 407

    @pytest.mark.asyncio
    async def test_plain_http_forwarding_is_refused(self, proxy) -> None:  # type: ignore[no-untyped-def]
        """Only CONNECT is served; absolute-form requests are a second thing to get wrong."""
        server, session = proxy
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        request = (
            f"GET http://example.com/ HTTP/1.1\r\n"
            f"Proxy-Authorization: Bearer {session.token}\r\n\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        status_line = (await reader.readline()).decode()
        writer.close()
        assert "405" in status_line

    @pytest.mark.asyncio
    async def test_a_revoked_session_loses_its_egress(self, proxy) -> None:  # type: ignore[no-untyped-def]
        """Tokens die with their episode, so a leaked one is not a standing grant."""
        server, session = proxy
        server.sessions.close(session)
        status, _ = await self.connect(server, "example.com:443", session.token)
        assert status == 407

    @pytest.mark.asyncio
    async def test_a_suspended_egress_session_reaches_nothing(self, proxy) -> None:  # type: ignore[no-untyped-def]
        """Between phases, what the agent left running reaches no host, its own included;
        a phase's allowlist takes effect only while that phase runs."""
        server, session = proxy
        session.suspended = True
        assert (await self.connect(server, "example.com:443", session.token))[0] == 407
        # The verifier's phase: its hosts, not the agent's.
        session.policy, session.suspended = allowlist("localhost"), False
        assert (await self.connect(server, "example.com:443", session.token))[0] == 403
        assert (await self.connect(server, "localhost:1", session.token))[0] == 502

    @pytest.mark.asyncio
    @pytest.mark.parametrize("purpose", ["agent", "judge"])
    async def test_a_model_session_is_no_proxy_credential(self, proxy, purpose: str) -> None:  # type: ignore[no-untyped-def]
        """The agent's model token, which its CLI holds, opens no tunnel, whatever hosts it
        carries: only the episode's egress session does."""
        server, _ = proxy
        model = server.sessions.open(
            GatewaySession(
                episode_id="e",
                model="m",
                purpose=purpose,  # type: ignore[arg-type]
                policy=allowlist("example.com"),
            )
        )
        status, _ = await self.connect(server, "example.com:443", model.token)
        assert status == 407


@pytest.mark.asyncio
async def test_the_gateway_refuses_an_egress_session_for_model_calls() -> None:
    """An egress token in the Sandbox's relay is no model credential: every route refuses it
    as unknown, open or suspended."""
    gateway = Gateway(host="127.0.0.1")
    await gateway.start()
    try:
        session = gateway.sessions.open(egress("example.com"))
        async with ClientSession() as client:
            for suspended in (False, True):
                session.suspended = suspended
                for method, path, headers in (
                    ("POST", "/v1/messages", {"x-api-key": session.token}),
                    ("POST", "/v1/messages", {"authorization": f"Bearer {session.token}"}),
                    ("GET", "/v1/models", {"x-api-key": session.token}),
                    ("POST", f"/v1beta/models/m:generateContent?key={session.token}", {}),
                ):
                    async with client.request(
                        method, f"{gateway.base_url}{path}", headers=headers, json={}
                    ) as response:
                        assert response.status == 401, (suspended, path)
        assert gateway.sessions.resolve(session.token) is None
        assert gateway.sessions.known(session.token) is None
        session.suspended = False
        assert gateway.sessions.egress(session.token) is session
    finally:
        await gateway.stop()


def client_hello(server_name: str) -> bytes:
    """The first record a real TLS client sends for ``server_name``."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname=server_name)
    with contextlib.suppress(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def in_two_records(record: bytes) -> bytes:
    """``record``'s handshake split across two TLS records, which servers accept."""
    body = record[5:]
    return b"".join(
        record[:3] + len(part).to_bytes(2, "big") + part for part in (body[:60], body[60:])
    )


class _Upstream:
    """A server the proxy tunnels to, recording what reaches it; with ``banner``, it speaks
    first. On 127.0.0.2, so that the proxy's connection to it comes from another address."""

    def __init__(self, banner: bytes = b"") -> None:
        self.banner = banner
        self.received = b""
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connected.set()
        writer.write(self.banner)
        with contextlib.suppress(ConnectionError):
            while chunk := await reader.read(65536):
                self.received += chunk
        self.closed.set()
        writer.close()


class TestBlocklist:
    """A blocklist admits every public host but the listed ones, judged by the name asked
    for, every address it resolves to, and the server name its TLS handshake asks for."""

    @pytest.fixture
    async def proxy(self) -> AsyncIterator[tuple[EgressProxy, GatewaySession]]:
        registry = SessionRegistry()
        session = registry.open(
            GatewaySession(
                episode_id="e",
                model="",
                purpose="egress",
                policy=blocklist("*.blocked.test", "1.2.3.0/24"),
            )
        )
        server = EgressProxy(registry, host="127.0.0.1")
        await server.start()
        try:
            yield server, session
        finally:
            await server.stop()

    @pytest.fixture
    def loopback_is_public(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Lets the tunnel reach a server on this machine's loopback, as it would a public
        host, so the rest of the path can be tested without the Internet."""

        def unreachable(address: str, policy: NetworkPolicy) -> str | None:
            if address.startswith("127."):
                return None if policy.admits(address) else "is blocked"
            return original(address, policy)

        original = proxy_module._unreachable
        monkeypatch.setattr(proxy_module, "_unreachable", unreachable)

    @staticmethod
    async def open(
        server: EgressProxy, target: str, token: str
    ) -> tuple[str, asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(
            f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Bearer {token}\r\n\r\n".encode()
        )
        await writer.drain()
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        if " 200 " not in head.split("\r\n")[0]:
            head += (await reader.read()).decode()
        return head, reader, writer

    @staticmethod
    @contextlib.asynccontextmanager
    async def upstream(banner: bytes = b"") -> AsyncIterator[tuple[_Upstream, int]]:
        server = _Upstream(banner)
        try:
            listener = await asyncio.start_server(server.serve, "127.0.0.2", 0)
        except OSError:
            pytest.skip("127.0.0.2 is not a loopback address here")
        try:
            yield server, listener.sockets[0].getsockname()[1]
        finally:
            listener.close()

    @pytest.mark.asyncio
    async def test_a_blocked_host_is_refused_without_naming_the_list(self, proxy) -> None:  # type: ignore[no-untyped-def]
        """A Task may block a site because it holds the answer: the refusal names neither it
        nor the rest of the list."""
        server, session = proxy
        for target in ("blocked.test:443", "www.Blocked.test.:443", "1.2.3.9:443"):
            head, _, writer = await self.open(server, target, session.token)
            writer.close()
            assert head.startswith("HTTP/1.1 403"), target
            assert "blocked by this task's network policy" in head
            assert "*.blocked.test" not in head and "1.2.3.0/24" not in head

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "target",
        [
            "127.0.0.1:22",
            "[::1]:22",
            "localhost:22",
            "10.0.0.1:443",
            "169.254.169.254:80",
            "[::ffff:169.254.169.254]:80",
            "[64:ff9b::a9fe:a9fe]:80",
        ],
    )
    async def test_the_host_s_own_networks_are_refused(self, proxy, target: str) -> None:  # type: ignore[no-untyped-def]
        """The tunnel leaves from the Host, so a host that is not public would reach its
        loopback, its networks or its cloud metadata, which no Sandbox reaches."""
        server, session = proxy
        head, _, writer = await self.open(server, target, session.token)
        writer.close()
        assert head.startswith("HTTP/1.1 403")
        assert "is not a public address" in head

    @pytest.mark.asyncio
    async def test_a_name_is_judged_by_every_address_it_resolves_to(  # type: ignore[no-untyped-def]
        self, proxy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wildcard DNS name spells a blocked address differently; a name that resolves
        to a public and a private address alike is the private host too."""
        resolve = socket.getaddrinfo

        def getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
            fake = {"mirror.test": ["8.8.8.8", "1.2.3.5"], "split.test": ["8.8.8.8", "10.9.9.9"]}
            if host in fake:
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
                    for address in fake[host]
                ]
            return resolve(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        server, session = proxy
        for target, reason in (
            ("mirror.test:443", "(1.2.3.5) is blocked"),
            ("split.test:443", "(10.9.9.9) is not a public address"),
        ):
            head, _, writer = await self.open(server, target, session.token)
            writer.close()
            assert head.startswith("HTTP/1.1 403"), target
            assert reason in head

    @pytest.mark.asyncio
    async def test_a_connection_that_reaches_the_host_itself_is_closed(  # type: ignore[no-untyped-def]
        self, proxy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An address of the Host's own may be public; a connection to it comes from that
        same address, so it is told apart without listing the Host's addresses."""
        monkeypatch.setattr(proxy_module, "_unreachable", lambda address, policy: None)
        server, session = proxy
        upstream = _Upstream()
        listener = await asyncio.start_server(upstream.serve, "127.0.0.1", 0)
        try:
            port = listener.sockets[0].getsockname()[1]
            head, _, writer = await self.open(server, f"127.0.0.1:{port}", session.token)
            writer.close()
        finally:
            listener.close()
        assert head.startswith("HTTP/1.1 403")
        assert "is the Host itself" in head
        assert upstream.received == b""

    @pytest.mark.asyncio
    async def test_an_admitted_server_name_is_tunneled_unchanged(  # type: ignore[no-untyped-def]
        self, proxy, loopback_is_public: None
    ) -> None:
        server, session = proxy
        hello = client_hello("example.org")
        async with self.upstream() as (upstream, port):
            head, _, writer = await self.open(server, f"127.0.0.2:{port}", session.token)
            assert head.startswith("HTTP/1.1 200")
            writer.write(hello + b"and then the rest")
            await writer.drain()
            writer.write_eof()
            await asyncio.wait_for(upstream.closed.wait(), 5)
            writer.close()
        assert upstream.received == hello + b"and then the rest"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("split", [False, True])
    async def test_a_handshake_naming_a_blocked_host_is_closed(  # type: ignore[no-untyped-def]
        self, proxy, loopback_is_public: None, split: bool
    ) -> None:
        """At an address the list does not name, a client that validates the blocked host's
        certificate asks for it by name, in one record or several."""
        server, session = proxy
        hello = client_hello("www.blocked.test")
        async with self.upstream() as (upstream, port):
            head, reader, writer = await self.open(server, f"127.0.0.2:{port}", session.token)
            assert head.startswith("HTTP/1.1 200")
            writer.write(in_two_records(hello) if split else hello)
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 5) == b""
            await asyncio.wait_for(upstream.closed.wait(), 5)
            writer.close()
        assert upstream.received == b""

    @pytest.mark.asyncio
    async def test_a_handshake_that_cannot_be_read_is_closed(  # type: ignore[no-untyped-def]
        self, proxy, loopback_is_public: None
    ) -> None:
        """Unjudged is not admitted."""
        server, session = proxy
        async with self.upstream() as (upstream, port):
            head, reader, writer = await self.open(server, f"127.0.0.2:{port}", session.token)
            assert head.startswith("HTTP/1.1 200")
            writer.write(b"\x16\x03\x01\x00\x06\x01\x00\x00\x02\x03\x03")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()
        assert upstream.received == b""

    @pytest.mark.asyncio
    async def test_a_server_that_speaks_first_is_not_held_up(  # type: ignore[no-untyped-def]
        self, proxy, loopback_is_public: None
    ) -> None:
        """SSH and SMTP servers speak before their clients; waiting for a ClientHello must
        not stall them."""
        server, session = proxy
        async with self.upstream(banner=b"SSH-2.0-test\r\n") as (upstream, port):
            head, reader, writer = await self.open(server, f"127.0.0.2:{port}", session.token)
            assert head.startswith("HTTP/1.1 200")
            assert await asyncio.wait_for(reader.readline(), 5) == b"SSH-2.0-test\r\n"
            writer.write(b"SSH-2.0-client\r\n")
            await writer.drain()
            writer.write_eof()
            await asyncio.wait_for(upstream.closed.wait(), 5)
            writer.close()
        assert upstream.received == b"SSH-2.0-client\r\n"
