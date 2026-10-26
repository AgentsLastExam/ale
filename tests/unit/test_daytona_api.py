"""The Daytona REST client: retries only where Daytona says "later", no secret in a message."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ale.core.errors import ConfigError, ProviderCapabilityError, ProviderStartError
from ale.run.providers import daytona_api
from ale.run.providers.daytona_api import DaytonaApi, DaytonaError

pytestmark = pytest.mark.unit

KEY = "dtn_secret_value_that_must_not_leak"


class Fake:
    """A Daytona look-alike that records what it was asked and answers from a script."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.answers: dict[tuple[str, str], list[Callable[[web.Request], web.StreamResponse]]] = {}

    def on(
        self, method: str, path: str, *handlers: Callable[[web.Request], web.StreamResponse]
    ) -> None:
        self.answers[(method, path)] = list(handlers)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else None
        self.calls.append((request.method, request.path, {"body": body, **request.query}))
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        script = self.answers.get((request.method, request.path))
        if not script:
            return web.json_response({"message": "no such route"}, status=404)
        handler = script.pop(0) if len(script) > 1 else script[0]
        return handler(request)


def respond(status: int = 200, body: object = None, **headers: str) -> Callable[..., web.Response]:
    return lambda request: web.json_response(body, status=status, headers=headers)


@pytest.fixture
async def daytona() -> AsyncIterator[tuple[DaytonaApi, Fake]]:
    fake = Fake()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    async with TestServer(app) as server:
        yield DaytonaApi(KEY, base_url=str(server.make_url(""))), fake


async def test_a_busy_answer_is_retried_and_a_refusal_is_not(
    daytona: tuple[DaytonaApi, Fake],
) -> None:
    api, fake = daytona
    fake.on(
        "GET",
        "/sandbox/a",
        respond(503, {"message": "busy"}, **{"Retry-After": "0"}),
        respond(200, {"id": "a"}),
    )
    assert (await api.get_sandbox("a")) == {"id": "a"}
    assert len(fake.calls) == 2

    fake.calls.clear()
    fake.on("GET", "/sandbox/b", respond(403, {"message": "quota exceeded"}))
    with pytest.raises(DaytonaError, match="quota exceeded") as refused:
        await api.get_sandbox("b")
    assert refused.value.status == 403
    assert len(fake.calls) == 1


async def test_a_missing_sandbox_is_none_and_deleting_it_is_success(
    daytona: tuple[DaytonaApi, Fake],
) -> None:
    api, fake = daytona
    fake.on("GET", "/sandbox/gone", respond(404, {"message": "not found"}))
    fake.on("DELETE", "/sandbox/gone", respond(404, {"message": "not found"}))
    assert await api.get_sandbox("gone") is None
    await api.delete_sandbox("gone")  # already gone: idempotent


async def test_delete_waits_until_the_sandbox_is_gone(daytona: tuple[DaytonaApi, Fake]) -> None:
    api, fake = daytona
    fake.on("DELETE", "/sandbox/x", respond(200, {}))
    fake.on(
        "GET",
        "/sandbox/x",
        respond(200, {"state": "destroying"}),
        respond(200, {"state": "destroyed"}),
    )
    await api.delete_sandbox("x")
    assert [call[0] for call in fake.calls].count("GET") == 2


async def test_a_create_that_may_have_happened_is_never_repeated(
    daytona: tuple[DaytonaApi, Fake],
) -> None:
    """A repeated create could leave two sandboxes under one name."""
    api, fake = daytona
    fake.on("POST", "/sandbox", respond(503, {"message": "busy"}))
    with pytest.raises(DaytonaError):
        await api.create_sandbox({"name": "n"})
    assert len(fake.calls) == 1


async def test_a_rate_limited_create_waits_and_tries_again(
    daytona: tuple[DaytonaApi, Fake],
) -> None:
    """Daytona made nothing when it rate-limited the create, so trying again is safe."""
    api, fake = daytona
    fake.on(
        "POST",
        "/sandbox",
        respond(429, {"message": "ThrottlerException: Too Many Requests"}, **{"Retry-After": "0"}),
        respond(429, {"message": "ThrottlerException: Too Many Requests"}, **{"Retry-After": "0"}),
        respond(200, {"id": "made"}),
    )
    assert await api.create_sandbox({"name": "n"}) == {"id": "made"}
    assert len(fake.calls) == 3


async def test_a_rate_limit_that_outlasts_the_wait_fails_the_create(
    daytona: tuple[DaytonaApi, Fake], monkeypatch: pytest.MonkeyPatch
) -> None:
    api, fake = daytona
    monkeypatch.setattr(daytona_api, "CREATE_RATE_LIMIT_SEC", 0.3)
    fake.on(
        "POST", "/sandbox", respond(429, {"message": "Too Many Requests"}, **{"Retry-After": "0.1"})
    )
    with pytest.raises(DaytonaError) as limited:
        await api.create_sandbox({"name": "n"})
    assert limited.value.status == 429
    assert 2 <= len(fake.calls) <= 5


async def test_a_rejected_create_is_a_capability_problem(
    daytona: tuple[DaytonaApi, Fake],
) -> None:
    api, fake = daytona
    fake.on("POST", "/sandbox", respond(400, {"message": "cpu exceeds the limit"}))
    with pytest.raises(ProviderCapabilityError, match="cpu exceeds the limit"):
        await api.create_sandbox({"name": "n"})


async def test_start_reports_daytonas_reason_for_a_failed_build(
    daytona: tuple[DaytonaApi, Fake],
) -> None:
    api, fake = daytona
    fake.on(
        "GET",
        "/sandbox/s",
        respond(200, {"state": "pending_build"}),
        respond(200, {"state": "build_failed", "errorReason": "401 Unauthorized"}),
    )
    with pytest.raises(ProviderStartError, match="build_failed: 401 Unauthorized"):
        await api.wait_started("s", timeout_sec=30)


async def test_listing_follows_pages_and_filters_by_labels(
    daytona: tuple[DaytonaApi, Fake],
) -> None:
    api, fake = daytona

    def page(request: web.Request) -> web.Response:
        if request.query.get("cursor") == "next":
            return web.json_response({"items": [{"id": "2"}], "nextCursor": None})
        return web.json_response({"items": [{"id": "1"}], "nextCursor": "next"})

    fake.on("GET", "/sandbox", page)
    found = await api.list_sandboxes({"ale.managed": "true"})
    assert [item["id"] for item in found] == ["1", "2"]
    assert json.loads(fake.calls[0][2]["labels"]) == {"ale.managed": "true"}


async def test_a_network_change_the_tier_forbids_says_so(
    daytona: tuple[DaytonaApi, Fake],
) -> None:
    api, fake = daytona
    fake.on("POST", "/sandbox/s/network-settings", respond(403, {"message": "tier too low"}))
    with pytest.raises(ProviderCapabilityError, match="Tier 3"):
        await api.update_network("s", {"networkBlockAll": True})


async def test_no_message_carries_the_key(daytona: tuple[DaytonaApi, Fake]) -> None:
    api, fake = daytona
    fake.on("GET", "/sandbox/s", respond(500, {"message": "boom"}))
    with pytest.raises(DaytonaError) as raised:
        await api.get_sandbox("s")
    assert KEY not in str(raised.value) and KEY not in repr(raised.value)


def test_the_key_comes_from_the_named_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="DAYTONA_API_KEY"):
        DaytonaApi.from_environment()
    monkeypatch.setenv("OTHER_KEY", " abc ")
    assert DaytonaApi.from_environment("OTHER_KEY")._key == "abc"
