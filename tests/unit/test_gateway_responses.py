from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from ale.core.trace import read_jsonl
from ale.run.gateway.openai_responses import usage_from_stream
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, Limits
from ale.run.recording import EpisodeRecording

pytestmark = pytest.mark.unit


class ResponsesUpstream:
    def __init__(self) -> None:
        self.calls = 0
        self.count_calls = 0
        self.key = ""
        self.model = ""
        self.max_output_tokens = 0
        self.runner: web.AppRunner | None = None
        self.url = ""

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/responses", self.responses)
        app.router.add_post("/v1/responses/input_tokens", self.count)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def responses(self, request: web.Request) -> web.Response:
        self.calls += 1
        self.key = request.headers.get("authorization", "")
        payload = await request.json()
        self.model = payload["model"]
        self.max_output_tokens = payload["max_output_tokens"]
        return web.json_response(
            {
                "id": "resp_1",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 6, "output_tokens": 4},
            }
        )

    async def count(self, request: web.Request) -> web.Response:
        self.count_calls += 1
        return web.json_response({"input_tokens": 6})


async def test_responses_dialect_routes_auth_model_and_exact_limits() -> None:
    upstream = ResponsesUpstream()
    await upstream.start()
    gateway = Gateway(
        api_key="real-key",
        upstream=upstream.url,
        dialect="openai-responses",
        host="127.0.0.1",
    )
    await gateway.start()
    session = gateway.open_session(
        GatewaySession(
            episode_id="episode",
            model="grok-4.5",
            limits=Limits(max_total_tokens=10),
        )
    )
    try:
        async with (
            ClientSession() as client,
            client.post(
                f"{gateway.base_url}/v1/responses",
                json={
                    "model": "wrong",
                    "input": "hello",
                    "max_output_tokens": 100,
                },
                headers={"authorization": f"Bearer {session.token}"},
            ) as response,
        ):
            assert response.status == 200
            assert json.loads(await response.text())["id"] == "resp_1"
    finally:
        await gateway.stop()
        await upstream.stop()

    assert upstream.key == "Bearer real-key"
    assert upstream.model == "grok-4.5"
    assert upstream.max_output_tokens == 4
    assert upstream.count_calls == 1
    assert session.usage.input_tokens == 6
    assert session.usage.output_tokens == 4


def test_responses_stream_usage_may_cross_network_chunk_boundaries() -> None:
    event = (
        b'data: {"type":"response.completed","response":{"id":"resp_1",'
        b'"status":"completed","usage":{"input_tokens":6,"output_tokens":4}}}\n\n'
    )

    assert usage_from_stream([event[:37], event[37:91], event[91:]]) == (6, 4, "completed")


def test_responses_stream_accounts_incomplete_terminal_events() -> None:
    event = (
        b'data: {"type":"response.incomplete","response":{"id":"resp_1",'
        b'"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},'
        b'"usage":{"input_tokens":6,"output_tokens":4}}}\n\n'
    )

    assert usage_from_stream([event]) == (6, 4, "max_output_tokens")


async def test_disconnected_downstream_does_not_cancel_upstream_accounting(
    tmp_path: Path, monkeypatch
) -> None:
    event = (
        b'data: {"type":"response.completed","response":{"id":"resp_1",'
        b'"status":"completed","usage":{"input_tokens":6,"output_tokens":4}}}\n\n'
    )

    class Content:
        async def iter_any(self):
            yield event[:49]
            await asyncio.sleep(0)
            yield event[49:]

    class Upstream:
        status = 200
        content = Content()

    class DisconnectedResponse:
        def __init__(self, **kwargs) -> None:
            self.status = kwargs["status"]

        async def prepare(self, request) -> None:
            return None

        async def write(self, chunk: bytes) -> None:
            raise ConnectionResetError

        async def write_eof(self) -> None:
            raise AssertionError("a disconnected downstream has no writable EOF")

    monkeypatch.setattr(
        "ale.run.gateway.server.web.StreamResponse",
        DisconnectedResponse,
    )
    recording = EpisodeRecording(tmp_path)
    gateway = Gateway(api_key="key", dialect="openai-responses")
    session = gateway.open_session(
        GatewaySession(episode_id="episode", model="grok-4.5"),
        recording.transport,
    )
    session.begin("request")
    reservation = await session.reserve(
        input_tokens=0,
        requested_output_tokens=100,
        pricing=(2.0, 6.0),
    )

    response = await gateway._stream(
        object(),  # type: ignore[arg-type]
        session,
        Upstream(),
        "request",
        {"model": "grok-4.5", "stream": True, "max_output_tokens": 100},
        "call_1",
        reservation,
        asyncio.get_running_loop().time(),
    )

    assert response.status == 200
    assert session.usage.input_tokens == 6
    assert session.usage.output_tokens == 4
    (record,) = read_jsonl(recording.transport.path).records
    assert record["disposition"] == "forwarded"
