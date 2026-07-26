"""The allowlist, tested where it is actually decided.

``network.mode: allowlist`` is enforced here rather than by rules on the host or tools
inside an image, so these are the assertions that matter: a declared host gets through,
everything else does not, and neither outcome depends on what the sandbox chose to run.
"""

from __future__ import annotations

import asyncio

import pytest

from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.session import GatewaySession, SessionRegistry

pytestmark = pytest.mark.unit


class TestAllowlistMatching:
    def session(self, *hosts: str) -> GatewaySession:
        return GatewaySession(episode_id="e", model="m", allowed_hosts=frozenset(hosts))

    def test_declared_host_is_allowed(self) -> None:
        assert self.session("example.com").may_reach("example.com")

    def test_subdomains_of_a_declared_host_are_allowed(self) -> None:
        """Declaring a service means its service, not one exact label."""
        session = self.session("example.com")
        assert session.may_reach("files.example.com")
        assert session.may_reach("a.b.example.com")

    def test_a_lookalike_suffix_is_not_allowed(self) -> None:
        """`notexample.com` ends with the declared string and is a different site."""
        assert not self.session("example.com").may_reach("notexample.com")

    def test_the_port_is_ignored_when_matching(self) -> None:
        assert self.session("example.com").may_reach("example.com:8443")

    def test_matching_is_case_and_trailing_dot_insensitive(self) -> None:
        session = self.session("Example.COM")
        assert session.may_reach("EXAMPLE.com")
        assert session.may_reach("example.com.")

    def test_declaring_nothing_allows_nothing(self) -> None:
        """The default is no additional egress, and it cannot be acquired by asking."""
        assert not self.session().may_reach("example.com")


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
        session = registry.open(
            GatewaySession(episode_id="e", model="m", allowed_hosts=frozenset({"example.com"}))
        )
        proxy = EgressProxy(registry, host="127.0.0.1")
        await proxy.start()
        try:
            yield proxy, session
        finally:
            await proxy.stop()

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
