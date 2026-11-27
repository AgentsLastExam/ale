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
from pydantic import ValidationError

from orbit.core.trace import TransportCall, read_jsonl
from orbit.run.gateway import anthropic, server
from orbit.run.gateway.anthropic import (
    extract_usage,
    refusal_body,
    stream_completed,
    usage_from_stream,
)
from orbit.run.gateway.server import Gateway
from orbit.run.gateway.session import CallUsage, GatewaySession, Limits, Usage
from orbit.run.recording import EpisodeRecording

pytestmark = pytest.mark.unit

REAL_KEY = "sk-ant-real-credential"


class Upstream:
    """A stub Anthropic endpoint that counts how often it was actually called."""

    def __init__(self) -> None:
        self.calls = 0
        self.count_calls = 0
        self.counted_input = 2
        self.cache_read = 0
        self.cache_write = 0
        self.output_tokens = 20
        self.status = 200
        self.delay = 0.0
        self.stream_gap = 0.0
        self.in_band_errors = 0
        self.token_data: dict[str, object] | None = None
        self.seen_keys: list[str] = []
        self.seen_authorizations: list[str] = []
        self.seen_models: list[str] = []
        self.seen_max_tokens: list[int] = []
        self.required_authorization = ""
        self.statuses: list[int] = []
        self.error_body: dict[str, object] = {"type": "error"}
        self.error_headers: dict[str, str] = {}
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
            return web.json_response(
                self.error_body, status=status, headers={"retry-after": "0", **self.error_headers}
            )
        if payload.get("stream"):
            return await self._stream(request, payload)
        body = {
            "type": "message",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": self.counted_input,
                "cache_read_input_tokens": self.cache_read,
                "cache_creation_input_tokens": self.cache_write,
                "output_tokens": min(self.output_tokens, payload["max_tokens"]),
            },
        }
        if self.token_data is not None:
            body["orbit_token_data"] = self.token_data
        return web.json_response(body)

    async def _stream(self, request: web.Request, payload: dict) -> web.StreamResponse:
        """An event stream that is silent for ``stream_gap`` seconds mid-message."""
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        start = {
            "type": "message_start",
            "message": {"id": "msg_1", "usage": {"input_tokens": self.counted_input}},
        }
        await response.write(f"event: message_start\ndata: {json.dumps(start)}\n\n".encode())
        if self.in_band_errors:
            self.in_band_errors -= 1
            error = {"type": "error", "error": {"type": "overloaded_error"}}
            await response.write(f"event: error\ndata: {json.dumps(error)}\n\n".encode())
            await response.write_eof()
            return response
        await asyncio.sleep(self.stream_gap)
        delta = {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": min(self.output_tokens, payload["max_tokens"])},
        }
        await response.write(f"event: message_delta\ndata: {json.dumps(delta)}\n\n".encode())
        await response.write(b'event: message_stop\ndata: {"type": "message_stop"}\n\n')
        await response.write_eof()
        return response

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


async def stream_call(
    gateway: Gateway, session: GatewaySession, **body: object
) -> tuple[int, list[tuple[float, bytes]]]:
    """POST a streaming request and return every chunk with the moment it arrived."""
    payload = {
        "model": "whatever-the-agent-asked-for",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "stream": True,
    }
    payload.update(body)
    loop = asyncio.get_running_loop()
    started = loop.time()
    chunks: list[tuple[float, bytes]] = []
    async with (
        ClientSession() as client,
        client.post(
            f"{gateway.base_url}/v1/messages",
            json=payload,
            headers={"authorization": f"Bearer {session.token}"},
        ) as response,
    ):
        async for chunk in response.content.iter_any():
            chunks.append((loop.time() - started, chunk))
        return response.status, chunks


class TestStreaming:
    async def test_a_stream_passes_through_and_is_accounted_to_its_end(self, stack) -> None:
        gateway, _upstream, session, trace = stack
        gateway.sse_keepalive_sec = None

        status, chunks = await stream_call(gateway, session)

        body = b"".join(chunk for _, chunk in chunks)
        assert status == 200
        assert b'"type": "message_stop"' in body
        (record,) = read_jsonl(trace.path).records
        assert record["disposition"] == "forwarded"
        assert (record["input_tokens"], record["output_tokens"]) == (2, 20)
        assert record["stop_reason"] == "end_turn"

    async def test_a_quiet_upstream_is_bridged_with_sse_comments(self, stack) -> None:
        """The provider may hold a response back for minutes (a buffered tool_use input);
        the client's idle timer must see bytes anyway, and nothing else may change."""
        gateway, upstream, session, trace = stack
        gateway.sse_keepalive_sec = 0.05
        upstream.stream_gap = 0.5

        status, chunks = await stream_call(gateway, session)

        assert status == 200
        heartbeats = [at for at, chunk in chunks if chunk.startswith(b": keep-alive")]
        assert heartbeats, "no heartbeat reached the client during the quiet half second"
        assert heartbeats[0] < 0.5, "the first heartbeat must arrive before the upstream resumes"
        assert len(heartbeats) >= 3
        body = b"".join(chunk for _, chunk in chunks)
        assert b'"type": "message_stop"' in body

        # What is recorded and replayed is the provider's stream, comments excluded.
        (record,) = read_jsonl(trace.path).records
        assert (record["input_tokens"], record["output_tokens"]) == (2, 20)
        upstream.stream_gap = 0.0
        replay_status, replay_chunks = await stream_call(gateway, session)
        replayed = b"".join(chunk for _, chunk in replay_chunks)
        assert replay_status == 200
        assert b": keep-alive" not in replayed
        assert b'"type": "message_stop"' in replayed
        assert upstream.calls == 1, "the identical retry was served from the cache"
        assert record["response_digest"] == f"sha256:{GatewaySession.digest(replayed)}"

    async def test_heartbeats_can_be_switched_off(self, stack) -> None:
        gateway, upstream, session, _ = stack
        gateway.sse_keepalive_sec = None
        upstream.stream_gap = 0.3

        _, chunks = await stream_call(gateway, session)

        assert not any(chunk.startswith(b":") for _, chunk in chunks)


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

    @pytest.mark.usefixtures("quick_retries")
    async def test_a_subscription_rides_out_a_rate_limit_as_any_upstream(self) -> None:
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
    async def test_ceiling_refuses_the_next_call(self, stack) -> None:
        gateway, upstream, session, _ = stack
        session.limits = Limits(max_model_calls=1)

        first, _ = await call(gateway, session)
        second, body = await call(gateway, session, messages=[{"role": "user", "content": "again"}])

        assert first == 200
        assert second == 429
        assert body["error"]["orbit"]["limit"] == "max_model_calls"
        assert upstream.calls == 1, "the refused call must not reach the provider"
        assert session.refusal is not None and session.refusal.limit == "max_model_calls"

    async def test_a_refusal_tells_clients_not_to_retry(self, stack) -> None:
        gateway, _, session, _ = stack
        session.limits = Limits(max_model_calls=0)
        async with (
            ClientSession() as client,
            client.post(
                f"{gateway.base_url}/v1/messages",
                json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                headers={"authorization": f"Bearer {session.token}"},
            ) as response,
        ):
            assert response.status == 429
            assert response.headers["x-should-retry"] == "false"

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
        assert body["error"]["orbit"]["limit"] == "max_input_tokens"
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
        assert body["error"]["orbit"]["limit"] == "max_output_tokens"
        assert upstream.seen_max_tokens == [100]

    async def test_total_limit_does_not_preempt_the_current_call(self, stack) -> None:
        gateway, upstream, session, _ = stack
        upstream.counted_input = 100
        session.limits = Limits(max_total_tokens=10)

        status, _ = await call(gateway, session, max_tokens=100)

        assert status == 200
        assert upstream.seen_max_tokens == [100]
        assert upstream.count_calls == 0

    async def test_a_call_the_provider_failed_does_not_count_toward_the_limit(self, stack) -> None:
        """A failed call gave the agent nothing: an outage must not spend its model calls."""
        gateway, upstream, session, _ = stack
        gateway.outage_retry_sec = 0.0
        session.limits = Limits(max_model_calls=1)
        upstream.status = 529

        first, _ = await call(gateway, session)
        upstream.status = 200
        second, _ = await call(
            gateway,
            session,
            messages=[{"role": "user", "content": "retry differently"}],
        )
        third, body = await call(
            gateway,
            session,
            messages=[{"role": "user", "content": "and once more"}],
        )

        assert (first, second, third) == (529, 200, 429)
        assert body["error"]["orbit"]["limit"] == "max_model_calls"
        assert session.usage.model_calls == 1
        assert upstream.calls == 2

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
        assert session.usage.model_calls == 1
        records = read_jsonl(trace.path).records
        assert [record["kind"] for record in records] == ["call", "replay"]
        assert records[1]["charged"] is False

    async def test_a_different_request_still_goes_upstream(self, stack) -> None:
        gateway, upstream, session, _ = stack
        await call(gateway, session)
        await call(gateway, session, messages=[{"role": "user", "content": "different"}])
        assert upstream.calls == 2

    async def test_a_retry_after_a_provider_error_is_forwarded(self, stack) -> None:
        """A 503 must not be replayed to the client's retries of the same request."""
        gateway, upstream, session, _ = stack
        gateway.outage_retry_sec = 0.0  # the client's retries, not the Gateway's
        upstream.statuses = [503]

        first_status, _ = await call(gateway, session)
        second_status, second = await call(gateway, session)
        third_status, third = await call(gateway, session)

        assert first_status == 503
        assert second_status == 200
        assert third == second and third_status == 200
        assert upstream.calls == 2  # the error was retried upstream; the success is replayed

    async def test_a_streamed_retry_after_a_provider_error_is_forwarded(self, stack) -> None:
        gateway, upstream, session, _ = stack
        gateway.outage_retry_sec = 0.0  # the client's retries, not the Gateway's
        upstream.statuses = [529]

        # The mock upstream only emits a real event stream for a 200; a non-200 body is
        # plain JSON either way, so the first call can still use the non-streaming helper.
        first_status, _ = await call(gateway, session, stream=True)
        second_status, _ = await stream_call(gateway, session)

        assert (first_status, second_status) == (529, 200)
        assert upstream.calls == 2

    async def test_a_streamed_in_band_error_is_not_replayed(self, stack) -> None:
        """An error inside an HTTP 200 stream must reach the provider again on retry."""
        gateway, upstream, session, _ = stack
        upstream.in_band_errors = 1

        first_status, first = await stream_call(gateway, session)
        second_status, second = await stream_call(gateway, session)

        assert (first_status, second_status) == (200, 200)
        assert b"overloaded_error" in b"".join(chunk for _, chunk in first)
        assert b"message_stop" in b"".join(chunk for _, chunk in second)
        assert upstream.calls == 2

    async def test_a_provider_rate_limit_is_not_replayed(self, stack) -> None:
        gateway, upstream, session, _ = stack
        gateway.outage_retry_sec = 0.0  # the client's retries, not the Gateway's
        upstream.statuses = [429]

        assert (await call(gateway, session))[0] == 429
        assert (await call(gateway, session))[0] == 200
        assert upstream.calls == 2


class TestRecording:
    async def test_every_call_lands_in_the_transport_trace(self, stack) -> None:
        gateway, _upstream, session, trace = stack
        await call(gateway, session)

        (record,) = read_jsonl(trace.path).records
        assert record["episode_id"] == "ep-1"
        assert record["input_tokens"] == 2
        assert record["output_tokens"] == 20
        assert not any("cost" in key for key in record), "Orbit reports tokens, not price"
        assert record["disposition"] == "forwarded"

    async def test_session_usage_keeps_cache_reads_and_writes(self, stack) -> None:
        gateway, upstream, session, trace = stack
        upstream.cache_read, upstream.cache_write = 30, 5
        await call(gateway, session)
        await call(gateway, session, messages=[{"role": "user", "content": "different"}])

        # Input counts every token the provider processed, cache reads and writes included.
        assert session.usage == Usage(
            model_calls=2,
            input_tokens=74,
            cache_read_tokens=60,
            cache_write_tokens=10,
            output_tokens=40,
        )
        records = read_jsonl(trace.path).records
        assert [record["cache_write_tokens"] for record in records] == [5, 5]

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

    async def test_debug_retention_leaves_out_the_tasks_secrets(
        self, stack, tmp_path: Path
    ) -> None:
        gateway, _upstream, session, _trace = stack
        session.payload_dir = tmp_path / "logs" / "gateway"
        session.redacted = ("task-secret-value",)
        await call(gateway, session, messages=[{"role": "user", "content": "task-secret-value"}])

        request = next(session.payload_dir.glob("*.request.json")).read_text()
        assert "task-secret-value" not in request and "[REDACTED]" in request

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
        assert usage_from_stream(events) == (
            CallUsage(input_tokens=50, output_tokens=12),
            "end_turn",
        )

    def test_truncated_stream_still_reports_what_it_saw(self) -> None:
        events = [b'data: {"type":"message_start","message":{"usage":{"input_tokens":50}}}\n\n']
        assert usage_from_stream(events) == (CallUsage(input_tokens=50), None)

    def test_stream_events_may_cross_network_chunk_boundaries(self) -> None:
        events = [
            b'data: {"type":"message_start","message":{"usage":{"input_',
            b'tokens":50}}}\n\ndata: {"type":"message_delta","usage":{"output_tokens":12},',
            b'"delta":{"stop_reason":"end_turn"}}\n\n',
        ]
        assert usage_from_stream(events) == (
            CallUsage(input_tokens=50, output_tokens=12),
            "end_turn",
        )

    def test_only_a_stream_ending_in_message_stop_completed(self) -> None:
        start = b'data: {"type":"message_start","message":{}}\n\n'
        stop = b'data: {"type":"message_stop"}\n\n'
        error = b'data: {"type":"error","error":{"type":"overloaded_error"}}\n\n'
        assert stream_completed([start, stop])
        assert not stream_completed([start])
        assert not stream_completed([start, error, stop])

    def test_cache_tokens_count_as_input_and_are_reported_separately(self) -> None:
        usage, stop_reason = extract_usage(
            {
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 9_000,
                    "cache_creation_input_tokens": 900,
                    "output_tokens": 50,
                },
            }
        )
        assert stop_reason == "end_turn"
        assert usage == CallUsage(
            input_tokens=10_000, cache_read_tokens=9_000, cache_write_tokens=900, output_tokens=50
        )

    def test_streamed_cache_tokens_are_read_from_message_start(self) -> None:
        events = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":3,'
            b'"cache_read_input_tokens":40,"cache_creation_input_tokens":7}}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":5},'
            b'"delta":{"stop_reason":"end_turn"}}\n\n',
        ]
        assert usage_from_stream(events) == (
            CallUsage(input_tokens=50, cache_read_tokens=40, cache_write_tokens=7, output_tokens=5),
            "end_turn",
        )

    def test_refusal_looks_like_a_provider_error(self) -> None:
        """Harnesses stop on provider errors already; none should need to know about Orbit."""
        body = refusal_body("max_total_tokens", 5_000)
        assert body["type"] == "error"
        assert body["error"]["type"] == "orbit_limit_reached"


@pytest.mark.parametrize("path", ["/models", "/v1/models", "/muse-code/models"])
async def test_model_catalog_lists_only_the_imposed_model(stack, path: str) -> None:
    gateway, upstream, session, _ = stack
    async with ClientSession() as client:
        async with client.get(
            f"{gateway.base_url}{path}", headers={"authorization": f"Bearer {session.token}"}
        ) as response:
            assert response.status == 200
            catalog = await response.json()
        async with client.get(f"{gateway.base_url}{path}") as anonymous:
            assert anonymous.status == 401
    assert catalog == {"object": "list", "data": [{"id": "claude-opus-4-8", "object": "model"}]}
    assert upstream.calls == 0


@pytest.fixture
def quick_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hundredths of a second between the Gateway's tries, where a provider waits seconds."""
    monkeypatch.setattr(server, "_RETRY_WAITS", (0.01, 0.02, 0.04, 0.08, 0.16, 0.32))


def outcomes(trace) -> list[tuple[str, int | None, str | None]]:  # type: ignore[no-untyped-def]
    return [
        (record["disposition"], record.get("upstream_status"), record.get("upstream_error"))
        for record in read_jsonl(trace.path).records
        if record["kind"] == "call"
    ]


@pytest.mark.usefixtures("quick_retries")
class TestProviderFailures:
    async def test_a_failing_provider_is_tried_again_before_the_client_sees_it(self, stack) -> None:
        gateway, upstream, session, trace = stack
        upstream.statuses = [529, 503]

        status, _ = await call(gateway, session)

        assert status == 200
        assert upstream.calls == 3
        assert outcomes(trace) == [
            ("failed", 529, "overloaded"),
            ("failed", 503, "server_error"),
            ("forwarded", None, None),
        ]
        # Each try is a call of its own.
        assert len({record["call_id"] for record in read_jsonl(trace.path).records}) == 3
        assert session.failure is None
        assert session.usage.model_calls == 1

    async def test_a_stream_is_tried_again_before_it_begins(self, stack) -> None:
        gateway, upstream, session, _ = stack
        upstream.statuses = [429]

        status, chunks = await stream_call(gateway, session)

        assert status == 200
        assert b"message_stop" in b"".join(chunk for _, chunk in chunks)
        assert upstream.calls == 2

    async def test_a_failure_longer_than_the_window_reaches_the_client(self, stack) -> None:
        gateway, upstream, session, trace = stack
        gateway.outage_retry_sec = 0.2
        upstream.status = 529

        status, _ = await call(gateway, session)

        assert status == 529
        assert upstream.calls > 2
        assert session.failure is not None and session.failure.kind == "overloaded"
        assert session.usage.model_calls == 0
        # Once the failure's window is spent, the next call's failure reaches the client at
        # once.
        await asyncio.sleep(0.2)
        tried = upstream.calls
        status, _ = await call(gateway, session, messages=[{"role": "user", "content": "two"}])
        assert (status, upstream.calls) == (529, tried + 1)
        # An answered call ends the failure.
        upstream.status = 200
        status, _ = await call(gateway, session, messages=[{"role": "user", "content": "three"}])
        assert status == 200
        assert session.failure is None
        assert outcomes(trace)[-1] == ("forwarded", None, None)

    async def test_the_providers_own_wait_is_kept(self, stack) -> None:
        gateway, upstream, session, _ = stack
        upstream.statuses = [429]
        upstream.error_headers = {"retry-after": "0.3"}
        started = asyncio.get_running_loop().time()

        status, _ = await call(gateway, session)

        assert status == 200
        assert asyncio.get_running_loop().time() - started >= 0.3

    async def test_a_quota_is_not_tried_again(self, stack) -> None:
        gateway, upstream, session, trace = stack
        upstream.statuses = [429]
        upstream.error_headers = {"anthropic-ratelimit-unified-status": "rejected"}

        status, _ = await call(gateway, session)

        assert (status, upstream.calls) == (429, 1)
        assert session.failure is not None and session.failure.kind == "quota"
        assert outcomes(trace) == [("failed", 429, "quota")]

    async def test_a_request_the_provider_rejects_is_the_clients_and_ends_a_failure(
        self, stack
    ) -> None:
        gateway, upstream, session, trace = stack
        gateway.outage_retry_sec = 0.0
        upstream.statuses = [529, 400]

        assert (await call(gateway, session))[0] == 529
        assert session.failure is not None
        second = await call(gateway, session, messages=[{"role": "user", "content": "bad"}])

        assert second[0] == 400
        assert upstream.calls == 2
        assert session.failure is None
        assert outcomes(trace) == [("failed", 529, "overloaded"), ("failed", 400, None)]
        # The provider answered the request it refused: that counts, the failure does not.
        assert session.usage.model_calls == 1

    async def test_a_failure_within_the_window_never_reaches_the_client(self, stack) -> None:
        """The last wait is cut to what is left of the window, so a failure that lifts before
        the window ends is ridden out whole."""
        gateway, upstream, session, _ = stack
        gateway.outage_retry_sec = 0.6
        began = asyncio.get_running_loop().time()
        upstream.status = 529

        async def lift() -> None:
            await asyncio.sleep(0.45)
            upstream.status = 200

        lifting = asyncio.create_task(lift())
        status, _ = await call(gateway, session)
        await lifting

        assert status == 200
        assert 0.45 <= asyncio.get_running_loop().time() - began < 0.6 + 0.2

    async def test_a_provider_named_wait_is_honoured_in_milliseconds(self, stack) -> None:
        gateway, upstream, session, _ = stack
        upstream.statuses = [503]
        upstream.error_headers = {"retry-after-ms": "250"}
        began = asyncio.get_running_loop().time()

        assert (await call(gateway, session))[0] == 200
        assert asyncio.get_running_loop().time() - began >= 0.25

    async def test_a_call_nobody_waits_for_any_more_is_not_tried_again(self, stack) -> None:
        """A Judge tries again itself, under its own deadline; a suspended session's turn is
        over."""
        gateway, upstream, session, _ = stack
        upstream.status = 529
        session.purpose = "judge"
        assert (await call(gateway, session))[0] == 529
        assert upstream.calls == 1
        session.purpose = "agent"

        async def end_turn() -> None:
            await asyncio.sleep(0.05)
            session.suspended = True

        began = asyncio.get_running_loop().time()
        ending = asyncio.create_task(end_turn())
        status, _ = await call(gateway, session, messages=[{"role": "user", "content": "two"}])
        await ending
        assert status == 529
        # Given up once the turn ended, well within the window.
        assert asyncio.get_running_loop().time() - began < 1.0

    async def test_a_provider_not_reached_is_tried_again_then_fails_the_call(
        self, tmp_path: Path
    ) -> None:
        upstream = Upstream()
        url = await upstream.start()
        await upstream.stop()  # nothing listens there now
        gateway = Gateway(api_key=REAL_KEY, upstream=url, host="127.0.0.1", outage_retry_sec=0.1)
        await gateway.start()
        trace = EpisodeRecording(tmp_path).transport
        session = gateway.open_session(
            GatewaySession(episode_id="ep-1", model="claude-opus-4-8"), trace
        )
        try:
            status, _ = await call_text(gateway, session)
        finally:
            await gateway.stop()

        assert status == 500
        tried = outcomes(trace)
        assert len(tried) > 1
        assert set(tried) == {("failed", None, "unreachable")}
        assert session.failure is not None and session.failure.kind == "unreachable"
        assert session.usage.model_calls == 0

    async def test_a_stream_the_provider_fails_in_is_failed_and_its_tokens_count(
        self, stack
    ) -> None:
        gateway, upstream, session, trace = stack
        upstream.in_band_errors = 1

        status, chunks = await stream_call(gateway, session)

        assert status == 200
        assert b"overloaded_error" in b"".join(chunk for _, chunk in chunks)
        assert outcomes(trace) == [("failed", 200, "overloaded")]
        assert read_jsonl(trace.path).records[0]["input_tokens"] == upstream.counted_input
        assert session.usage.model_calls == 0
        assert session.usage.input_tokens == upstream.counted_input
        assert session.failure is not None and session.failure.kind == "overloaded"


async def call_text(gateway: Gateway, session: GatewaySession) -> tuple[int, str]:
    async with (
        ClientSession() as client,
        client.post(
            f"{gateway.base_url}/v1/messages",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
            headers={"authorization": f"Bearer {session.token}"},
        ) as response,
    ):
        return response.status, await response.text()


@pytest.mark.parametrize(
    ("error", "headers", "quota"),
    [
        ({"type": "error", "error": {"type": "billing_error", "message": "x"}}, {}, True),
        (
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Your credit balance is too low to access the Anthropic API.",
                },
            },
            {},
            True,
        ),
        (
            {"type": "error", "error": {"type": "rate_limit_error"}},
            {"anthropic-ratelimit-unified-status": "rejected"},
            True,
        ),
        (
            {"type": "error", "error": {"type": "rate_limit_error"}},
            {
                "anthropic-ratelimit-unified-status": "rejected",
                "anthropic-ratelimit-unified-overage-status": "allowed",
            },
            False,
        ),
        ({"type": "error", "error": {"type": "rate_limit_error"}}, {}, False),
        ({"type": "error", "error": {"type": "overloaded_error"}}, {}, False),
    ],
)
def test_anthropic_names_the_accounts_own_limits(
    error: dict, headers: dict[str, str], quota: bool
) -> None:
    assert anthropic.quota(error, headers) is quota


def test_only_a_failed_call_says_how_the_provider_failed_it() -> None:
    call = dict(episode_id="e", call_id="c", model="m", request_digest="sha256:d")
    assert TransportCall(**call, disposition="failed", upstream_error="overloaded")
    with pytest.raises(ValidationError, match="upstream_error is valid only for failed calls"):
        TransportCall(**call, disposition="forwarded", upstream_error="overloaded")
