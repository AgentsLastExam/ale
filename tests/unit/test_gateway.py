"""Gateway behaviour: credentials, ceilings, retries, records.

The upstream is a local stub, so these tests exercise the real proxy path without
spending anything.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from ale.core.errors import ConfigError
from ale.core.trace import read_jsonl
from ale.run.gateway.anthropic import estimate_cost, refusal_body, usage_from_stream
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, Limits
from ale.run.recording import EpisodeRecording

pytestmark = pytest.mark.unit

REAL_KEY = "sk-ant-real-credential"


class Upstream:
    """A stub Anthropic endpoint that counts how often it was actually called."""

    def __init__(self) -> None:
        self.calls = 0
        self.count_calls = 0
        self.counted_input = 2
        self.output_tokens = 20
        self.status = 200
        self.delay = 0.0
        self.token_data: dict[str, object] | None = None
        self.seen_keys: list[str] = []
        self.seen_authorizations: list[str] = []
        self.seen_models: list[str] = []
        self.seen_max_tokens: list[int] = []
        self.required_authorization = ""
        self.statuses: list[int] = []
        self.runner: web.AppRunner | None = None
        self.url = ""

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/messages", self._messages)
        app.router.add_post("/v1/messages/count_tokens", self._count_tokens)
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
        if self.delay:
            await asyncio.sleep(self.delay)
        self.seen_keys.append(request.headers.get("x-api-key", ""))
        self.seen_authorizations.append(request.headers.get("authorization", ""))
        payload = await request.json()
        self.seen_models.append(payload.get("model", ""))
        self.seen_max_tokens.append(payload.get("max_tokens", 0))
        if (
            self.required_authorization
            and self.seen_authorizations[-1] != self.required_authorization
        ):
            return web.json_response({"type": "error"}, status=401)
        status = self.statuses.pop(0) if self.statuses else self.status
        if status != 200:
            return web.json_response({"type": "error"}, status=status, headers={"retry-after": "0"})
        body = {
            "type": "message",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": self.counted_input,
                "output_tokens": min(self.output_tokens, payload["max_tokens"]),
            },
        }
        if self.token_data is not None:
            body["ale_token_data"] = self.token_data
        return web.json_response(body)

    async def _count_tokens(self, request: web.Request) -> web.Response:
        self.count_calls += 1
        return web.json_response({"input_tokens": self.counted_input})


@pytest.fixture
async def stack(tmp_path: Path):
    upstream = Upstream()
    await upstream.start()
    gateway = Gateway(api_key=REAL_KEY, upstream=upstream.url, host="127.0.0.1")
    await gateway.start()
    trace = EpisodeRecording(tmp_path).transport
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
        "max_tokens": 100,
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

    async def test_anthropic_subscription_uses_bearer_upstream(self) -> None:
        upstream = Upstream()
        await upstream.start()
        gateway = Gateway(
            api_key="oauth-token",
            upstream=upstream.url,
            bearer_auth=True,
            host="127.0.0.1",
        )
        await gateway.start()
        session = gateway.open_session(GatewaySession(episode_id="oauth", model="claude-opus-4-8"))
        try:
            status, _ = await call(gateway, session)
            assert status == 200
            assert upstream.seen_authorizations == ["Bearer oauth-token"]
            assert upstream.seen_keys == [""]
        finally:
            await gateway.stop()
            await upstream.stop()

    async def test_subscription_refreshes_once_after_unauthorized(self) -> None:
        upstream = Upstream()
        await upstream.start()
        upstream.required_authorization = "Bearer fresh"

        class Credential:
            upstream_path = "/v1/messages"
            dialect = "anthropic"
            supports_output_limit = True
            token = "stale"
            refreshes = 0

            def __init__(self, url: str) -> None:
                self.upstream = url

            async def headers(self) -> dict[str, str]:
                return {"authorization": f"Bearer {self.token}"}

            async def refresh(self) -> bool:
                self.refreshes += 1
                self.token = "fresh"
                return True

        credential = Credential(upstream.url)
        gateway = Gateway(subscription=credential, host="127.0.0.1")  # type: ignore[arg-type]
        await gateway.start()
        session = gateway.open_session(GatewaySession(episode_id="oauth", model="claude-opus-4-8"))
        try:
            status, _ = await call(gateway, session)
            assert status == 200
            assert credential.refreshes == 1
            assert upstream.seen_authorizations == ["Bearer stale", "Bearer fresh"]
        finally:
            await gateway.stop()
            await upstream.stop()

    async def test_subscription_retries_one_transient_rate_limit(self) -> None:
        upstream = Upstream()
        await upstream.start()
        upstream.statuses = [429, 200]

        class Credential:
            upstream_path = "/v1/messages"
            dialect = "anthropic"
            supports_output_limit = True

            def __init__(self, url: str) -> None:
                self.upstream = url

            async def headers(self) -> dict[str, str]:
                return {"authorization": "Bearer subscription"}

            async def refresh(self) -> bool:
                return False

        gateway = Gateway(
            subscription=Credential(upstream.url),  # type: ignore[arg-type]
            host="127.0.0.1",
        )
        await gateway.start()
        session = gateway.open_session(GatewaySession(episode_id="oauth", model="claude-opus-4-8"))
        try:
            status, _ = await call(gateway, session)
            assert status == 200
            assert upstream.calls == 2
        finally:
            await gateway.stop()
            await upstream.stop()

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
    def test_finite_cost_limit_requires_known_model_pricing(self) -> None:
        gateway = Gateway(api_key=REAL_KEY)
        with pytest.raises(ConfigError, match="known pricing"):
            gateway.open_session(
                GatewaySession(
                    episode_id="unknown",
                    model="vendor-new-model",
                    limits=Limits(max_cost_usd=1.0),
                )
            )

    async def test_ceiling_refuses_the_next_call(self, stack) -> None:
        gateway, upstream, session, _ = stack
        session.limits = Limits(max_model_calls=1)

        first, _ = await call(gateway, session)
        second, body = await call(gateway, session, messages=[{"role": "user", "content": "again"}])

        assert first == 200
        assert second == 429
        assert body["error"]["ale"]["limit"] == "max_model_calls"
        assert upstream.calls == 1, "the refused call must not reach the provider"

    async def test_cost_ceiling_uses_recorded_usage(self, stack) -> None:
        gateway, _upstream, session, _ = stack
        session.limits = Limits(max_cost_usd=0.0005)

        first, _ = await call(gateway, session)
        second, body = await call(gateway, session, messages=[{"role": "user", "content": "x"}])

        assert first == 200
        assert second == 429
        assert body["error"]["ale"]["limit"] == "max_cost_usd"

    async def test_input_ceiling_stops_after_provider_usage_crosses_it(self, stack) -> None:
        gateway, upstream, session, _ = stack
        upstream.counted_input = 6
        session.limits = Limits(max_input_tokens=5)

        first, _ = await call(gateway, session)
        second, body = await call(
            gateway,
            session,
            messages=[{"role": "user", "content": "different"}],
        )

        assert first == 200
        assert second == 429
        assert body["error"]["ale"]["limit"] == "max_input_tokens"
        assert upstream.count_calls == 0
        assert upstream.calls == 1

    async def test_output_ceiling_stops_after_provider_usage_crosses_it(self, stack) -> None:
        gateway, upstream, session, _ = stack
        session.limits = Limits(max_output_tokens=7)

        first, _ = await call(gateway, session, max_tokens=100)
        second, body = await call(
            gateway,
            session,
            messages=[{"role": "user", "content": "different"}],
        )

        assert first == 200
        assert second == 429
        assert body["error"]["ale"]["limit"] == "max_output_tokens"
        assert upstream.seen_max_tokens == [100]

    @pytest.mark.parametrize(
        "limits",
        [
            Limits(max_total_tokens=10),
            Limits(max_cost_usd=0.00075),
        ],
    )
    async def test_total_and_cost_limits_do_not_preempt_the_current_call(
        self, stack, limits: Limits
    ) -> None:
        gateway, upstream, session, _ = stack
        upstream.counted_input = 100
        session.limits = limits

        status, _ = await call(gateway, session, max_tokens=100)

        assert status == 200
        assert upstream.seen_max_tokens == [100]
        assert upstream.count_calls == 0

    async def test_failed_forwarded_call_counts_toward_model_call_limit(self, stack) -> None:
        gateway, upstream, session, _ = stack
        session.limits = Limits(max_model_calls=1)
        upstream.status = 529

        first, _ = await call(gateway, session)
        upstream.status = 200
        second, body = await call(
            gateway,
            session,
            messages=[{"role": "user", "content": "retry differently"}],
        )

        assert first == 529
        assert second == 429
        assert body["error"]["ale"]["limit"] == "max_model_calls"
        assert upstream.calls == 1

    async def test_concurrent_calls_cannot_oversubscribe(self, stack) -> None:
        gateway, upstream, session, _ = stack
        session.limits = Limits(max_model_calls=1)
        upstream.delay = 0.05

        results = await asyncio.gather(
            call(gateway, session, messages=[{"role": "user", "content": "one"}]),
            call(gateway, session, messages=[{"role": "user", "content": "two"}]),
        )

        assert sorted(status for status, _ in results) == [200, 429]
        assert upstream.calls == 1


class TestRetryIdempotency:
    async def test_identical_request_is_served_from_cache(self, stack) -> None:
        """An SDK retry must not sample twice, double-count, or fork the conversation."""
        gateway, upstream, session, trace = stack
        session.limits = Limits(max_input_tokens=10)

        first = await call(gateway, session)
        second = await call(gateway, session)

        assert first == second
        assert upstream.calls == 1
        assert upstream.count_calls == 0
        assert session.usage.turns == 1
        records = read_jsonl(trace.path).records
        assert [record["kind"] for record in records] == ["call", "replay"]
        assert records[1]["charged"] is False

    async def test_a_different_request_still_goes_upstream(self, stack) -> None:
        gateway, upstream, session, _ = stack
        await call(gateway, session)
        await call(gateway, session, messages=[{"role": "user", "content": "different"}])
        assert upstream.calls == 2


class TestRecording:
    async def test_every_call_lands_in_the_transport_trace(self, stack) -> None:
        gateway, _upstream, session, trace = stack
        await call(gateway, session)

        (record,) = read_jsonl(trace.path).records
        assert record["episode_id"] == "ep-1"
        assert record["input_tokens"] == 2
        assert record["output_tokens"] == 20
        assert record["cost_usd"] > 0
        assert record["disposition"] == "forwarded"

    async def test_a_refusal_is_recorded_too(self, stack) -> None:
        gateway, _upstream, session, trace = stack
        session.limits = Limits(max_model_calls=0)
        await call(gateway, session)

        (record,) = read_jsonl(trace.path).records
        assert record["disposition"] == "refused"
        assert record["refusal_limit"] == "max_model_calls"

    async def test_default_retention_keeps_only_digests(self, stack) -> None:
        gateway, _upstream, session, trace = stack
        await call(gateway, session)
        assert not (trace.path.parent / "logs" / "gateway").exists()

    async def test_debug_retention_keeps_raw_request_and_response(
        self, stack, tmp_path: Path
    ) -> None:
        gateway, _upstream, session, _trace = stack
        session.payload_dir = tmp_path / "logs" / "gateway"
        await call(gateway, session)

        request = next(session.payload_dir.glob("*.request.json")).read_text()
        response = next(session.payload_dir.glob("*.response.json")).read_text()
        assert '"messages"' in request and '"content"' in response
        assert REAL_KEY not in request + response

    async def test_exact_token_retention_requires_explicit_destination(
        self, stack, tmp_path: Path
    ) -> None:
        gateway, upstream, session, _trace = stack
        upstream.token_data = {
            "prompt_token_ids": [1, 2],
            "completion_token_ids": [3],
            "logprobs": [-0.1],
            "sampled_mask": [False, False, True],
        }

        await call(gateway, session, temperature=0.7)
        assert not (tmp_path / "logs" / "gateway" / "tokens").exists()

        session.exact_token_dir = tmp_path / "logs" / "gateway" / "tokens"
        await call(
            gateway,
            session,
            temperature=0.7,
            messages=[{"role": "user", "content": "different"}],
        )

        (path,) = session.exact_token_dir.glob("*.json")
        assert json.loads(path.read_text()) == {
            "prompt_token_ids": [1, 2],
            "completion_token_ids": [3],
            "logprobs": [-0.1],
            "sampled_mask": [False, False, True],
            "temperature": 0.7,
        }


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

    def test_stream_events_may_cross_network_chunk_boundaries(self) -> None:
        events = [
            b'data: {"type":"message_start","message":{"usage":{"input_',
            b'tokens":50}}}\n\ndata: {"type":"message_delta","usage":{"output_tokens":12},',
            b'"delta":{"stop_reason":"end_turn"}}\n\n',
        ]
        assert usage_from_stream(events) == (50, 12, "end_turn")

    def test_cost_scales_with_the_model_family(self) -> None:
        assert estimate_cost("claude-haiku-4-5", 1000, 1000) < estimate_cost(
            "claude-opus-4-8", 1000, 1000
        )

    def test_refusal_looks_like_a_provider_error(self) -> None:
        """Harnesses stop on provider errors already; none should need to know about ALE."""
        body = refusal_body("max_cost_usd", 5.0)
        assert body["type"] == "error"
        assert body["error"]["type"] == "ale_limit_reached"
