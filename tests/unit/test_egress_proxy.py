"""The allowlist, tested where it is actually decided.

``network.mode: allowlist`` is enforced here rather than by rules on the host or tools
inside an image, so these are the assertions that matter: a declared host gets through,
everything else does not, and neither outcome depends on what the sandbox chose to run.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import ClientSession

from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, SessionRegistry

pytestmark = pytest.mark.unit


def egress(*hosts: str) -> GatewaySession:
    """An episode's egress session, open for ``hosts``."""
    return GatewaySession(
        episode_id="e", model="", purpose="egress", allowed_hosts=frozenset(hosts)
    )


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
        session.allowed_hosts, session.suspended = frozenset({"localhost"}), False
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
                allowed_hosts=frozenset({"example.com"}),
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
