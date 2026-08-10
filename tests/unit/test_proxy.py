from __future__ import annotations

import base64

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
    encoded = base64.b64encode(b"episode-token:").decode()
    assert (
        EgressProxy(sessions)._authenticate(
            ["CONNECT example.com:443 HTTP/1.1", f"Proxy-Authorization: Basic {encoded}"]
        )
        is session
    )
