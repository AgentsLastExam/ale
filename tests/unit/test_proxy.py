from __future__ import annotations

import asyncio
import base64
import urllib.error
import urllib.request

import pytest

from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.session import GatewaySession, SessionRegistry

pytestmark = pytest.mark.unit


def test_proxy_accepts_standard_basic_auth_for_the_episode_token() -> None:
    sessions = SessionRegistry()
    session = sessions.open(
        GatewaySession(
            episode_id="episode",
            model="none",
            token="episode-token",
            allowed_hosts=frozenset({"example.com"}),
        )
    )
    encoded = base64.b64encode(b"ale:episode-token").decode()
    assert (
        EgressProxy(sessions)._authenticate(
            ["CONNECT example.com:443 HTTP/1.1", f"Proxy-Authorization: Basic {encoded}"]
        )
        is session
    )


@pytest.mark.asyncio
async def test_python_urllib_authenticates_with_the_token_as_its_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python's urllib sends proxy credentials only when they include a password."""
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    sessions = SessionRegistry()
    session = sessions.open(
        GatewaySession(episode_id="e", model="none", allowed_hosts=frozenset({"example.com"}))
    )
    proxy = EgressProxy(sessions, host="127.0.0.1")
    await proxy.start()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": f"http://ale:{session.token}@127.0.0.1:{proxy.port}"})
    )
    try:
        # An undeclared host: authenticated, the proxy refuses it with 403 rather than 407.
        with pytest.raises(urllib.error.URLError, match="403"):
            await asyncio.to_thread(opener.open, "https://pypi.org/", timeout=10)
    finally:
        await proxy.stop()
