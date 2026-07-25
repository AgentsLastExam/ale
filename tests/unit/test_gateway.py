"""Gateway behaviour: credentials, ceilings, retries, records.

The upstream is a local stub, so these tests exercise the real proxy path without
spending anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from ale.core.trace import TraceLayer, TraceWriter, read_records
from ale.run.gateway.anthropic import estimate_cost, refusal_body, usage_from_stream
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, LimitReached, Limits

pytestmark = pytest.mark.unit

REAL_KEY = "sk-ant-real-credential"


class Upstream:
    """A stub Anthropic endpoint that counts how often it was actually called."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_keys: list[str] = []
        self.seen_models: list[str] = []
        self.runner: web.AppRunner | None = None
        self.url = ""

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/messages", self._messages)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def _messages(self, request: web.Request) -> web.Response:
        self.calls += 1
        self.seen_keys.append(request.headers.get("x-api-key", ""))
        payload = await request.json()
        self.seen_models.append(payload.get("model", ""))
        return web.json_response(
            {
                "type": "message",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            }
        )


@pytest.fixture
async def stack(tmp_path: Path):
    upstream = Upstream()
    await upstream.start()
    gateway = Gateway(api_key=REAL_KEY, upstream=upstream.url, host="127.0.0.1")
    await gateway.start()
    trace = TraceWriter(tmp_path)
    session = gateway.open_session(
        GatewaySession(episode_id="ep-1", model="claude-opus-4-8", limits=Limits()), trace
    )
    try:
        yield gateway, upstream, session, trace
    finally:
        await gateway.stop()
        await upstream.stop()


async def call(gateway: Gateway, session: GatewaySession, **body: object) -> tuple[int, dict]:
    payload = {
        "model": "whatever-the-agent-asked-for",
        "messages": [{"role": "user", "content": "hi"}],
    }
    payload.update(body)
    async with (
        ClientSession() as client,
        client.post(
            f"{gateway.base_url}/v1/messages",
            json=payload,
            headers={"authorization": f"Bearer {session.token}"},
        ) as response,
    ):
        return response.status, json.loads(await response.text())


class TestCredentialIsolation:
    async def test_sandbox_token_is_swapped_for_the_real_key(self, stack) -> None:
        gateway, upstream, session, _ = stack
        status, _body = await call(gateway, session)

        assert status == 200
        assert upstream.seen_keys == [REAL_KEY]
        assert REAL_KEY not in session.token

    async def test_unknown_token_is_rejected(self, stack) -> None:
        gateway, upstream, _session, _ = stack
        async with (
            ClientSession() as client,
            client.post(
                f"{gateway.base_url}/v1/messages",
                json={"messages": []},
                headers={"authorization": "Bearer forged"},
            ) as response,
        ):
            assert response.status == 401
        assert upstream.calls == 0

    async def test_revoked_session_stops_working(self, stack) -> None:
        gateway, _upstream, session, _ = stack
        gateway.close_session(session)
        async with (
            ClientSession() as client,
            client.post(
                f"{gateway.base_url}/v1/messages",
                json={"messages": []},
                headers={"authorization": f"Bearer {session.token}"},
            ) as response,
        ):
            assert response.status == 401


class TestAuthority:
    async def test_the_run_decides_the_model(self, stack) -> None:
        """An agent asking for another model gets the one the run declared."""
        gateway, upstream, session, _ = stack
        await call(gateway, session)
        assert upstream.seen_models == ["claude-opus-4-8"]


class TestLimits:
    async def test_ceiling_refuses_the_next_call(self, stack) -> None:
        gateway, upstream, session, _ = stack
        session.limits = Limits(max_turns=1)

        first, _ = await call(gateway, session)
        second, body = await call(gateway, session, messages=[{"role": "user", "content": "again"}])

        assert first == 200
        assert second == 429
        assert body["error"]["ale"]["limit"] == "max_turns"
        assert upstream.calls == 1, "the refused call must not reach the provider"

    async def test_cost_ceiling_uses_recorded_usage(self, stack) -> None:
        gateway, _upstream, session, _ = stack
        session.limits = Limits(max_cost_usd=0.0001)
        await call(gateway, session)

        second, body = await call(gateway, session, messages=[{"role": "user", "content": "x"}])
        assert second == 429
        assert body["error"]["ale"]["limit"] == "max_cost_usd"

    def test_check_is_pre_emptive(self) -> None:
        session = GatewaySession(episode_id="e", model="m", limits=Limits(max_total_tokens=10))
        session.record(input_tokens=8, output_tokens=4, cost_usd=0.0)
        with pytest.raises(LimitReached):
            session.check()


class TestRetryIdempotency:
    async def test_identical_request_is_served_from_cache(self, stack) -> None:
        """An SDK retry must not sample twice, double-count, or fork the conversation."""
        gateway, upstream, session, trace = stack

        first = await call(gateway, session)
        second = await call(gateway, session)

        assert first == second
        assert upstream.calls == 1
        assert session.usage.turns == 1
        records = list(read_records(trace.path(TraceLayer.TRANSPORT)))
        assert len(records) == 1

    async def test_a_different_request_still_goes_upstream(self, stack) -> None:
        gateway, upstream, session, _ = stack
        await call(gateway, session)
        await call(gateway, session, messages=[{"role": "user", "content": "different"}])
        assert upstream.calls == 2


class TestRecording:
    async def test_every_call_lands_in_the_transport_trace(self, stack) -> None:
        gateway, _upstream, session, trace = stack
        await call(gateway, session)

        (record,) = list(read_records(trace.path(TraceLayer.TRANSPORT)))
        assert record["episode_id"] == "ep-1"
        assert record["input_tokens"] == 100
        assert record["output_tokens"] == 20
        assert record["cost_usd"] > 0
        assert record["refused"] is False

    async def test_a_refusal_is_recorded_too(self, stack) -> None:
        gateway, _upstream, session, trace = stack
        session.limits = Limits(max_turns=0)
        await call(gateway, session)

        (record,) = list(read_records(trace.path(TraceLayer.TRANSPORT)))
        assert record["refused"] is True
        assert record["refusal_limit"] == "max_turns"


class TestDialect:
    def test_stream_usage_needs_both_events(self) -> None:
        events = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":50}}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":12},'
            b'"delta":{"stop_reason":"end_turn"}}\n\n',
        ]
        assert usage_from_stream(events) == (50, 12, "end_turn")

    def test_truncated_stream_still_reports_what_it_saw(self) -> None:
        events = [b'data: {"type":"message_start","message":{"usage":{"input_tokens":50}}}\n\n']
        assert usage_from_stream(events) == (50, 0, None)

    def test_cost_scales_with_the_model_family(self) -> None:
        assert estimate_cost("claude-haiku-4-5", 1000, 1000) < estimate_cost(
            "claude-opus-4-8", 1000, 1000
        )

    def test_refusal_looks_like_a_provider_error(self) -> None:
        """Harnesses stop on provider errors already; none should need to know about ALE."""
        body = refusal_body("max_cost_usd", 5.0)
        assert body["type"] == "error"
        assert body["error"]["type"] == "ale_limit_reached"
