from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from ale.core.trace import read_jsonl
from ale.run.gateway.openai_chat_completions import usage_from_stream
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, Limits
from ale.run.recording import EpisodeRecording

pytestmark = pytest.mark.unit


class ChatUpstream:
    def __init__(self) -> None:
        self.calls = 0
        self.key = ""
        self.payload: dict = {}
        self.runner: web.AppRunner | None = None
        self.url = ""

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.completions)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def completions(self, request: web.Request) -> web.StreamResponse:
        self.calls += 1
        self.key = request.headers.get("authorization", "")
        self.payload = await request.json()
        if not self.payload.get("stream"):
            return web.json_response(
                {
                    "id": "chatcmpl_1",
                    "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10},
                }
            )
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        await response.write(
            b'data: {"id":"chatcmpl_2","choices":[{"delta":{"content":"ok"},'
            b'"finish_reason":"stop"}],"usage":null}\n\n'
        )
        await response.write(
            b'data: {"id":"chatcmpl_2","choices":[],"usage":'
            b'{"prompt_tokens":6,"completion_tokens":4,"total_tokens":10}}\n\n'
        )
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response


async def test_chat_dialect_routes_auth_model_and_output_limit(tmp_path: Path) -> None:
    upstream = ChatUpstream()
    await upstream.start()
    recording = EpisodeRecording(tmp_path)
    gateway = Gateway(
        api_key="real-key",
        upstream=upstream.url,
        dialect="openai-chat-completions",
        host="127.0.0.1",
    )
    await gateway.start()
    session = gateway.open_session(
        GatewaySession(
            episode_id="episode",
            model="gpt-5-mini",
            limits=Limits(max_output_tokens=4),
        ),
        recording.transport,
    )
    try:
        async with (
            ClientSession() as client,
            client.post(
                f"{gateway.base_url}/v1/chat/completions",
                json={
                    "model": "wrong",
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 100,
                },
                headers={"authorization": f"Bearer {session.token}"},
            ) as response,
        ):
            assert response.status == 200
            assert json.loads(await response.text())["id"] == "chatcmpl_1"
    finally:
        await gateway.stop()
        await upstream.stop()

    assert upstream.key == "Bearer real-key"
    assert upstream.payload["model"] == "gpt-5-mini"
    assert upstream.payload["max_completion_tokens"] == 100
    assert "max_tokens" not in upstream.payload
    assert session.usage.input_tokens == 6
    assert session.usage.output_tokens == 4
    (record,) = read_jsonl(recording.transport.path).records
    assert record["provider_response_id"] == "chatcmpl_1"


async def test_chat_stream_forces_usage_and_accounts_it(tmp_path: Path) -> None:
    upstream = ChatUpstream()
    await upstream.start()
    recording = EpisodeRecording(tmp_path)
    gateway = Gateway(
        api_key="real-key",
        upstream=upstream.url,
        dialect="openai-chat-completions",
        host="127.0.0.1",
    )
    await gateway.start()
    session = gateway.open_session(
        GatewaySession(episode_id="episode", model="gpt-5-mini"),
        recording.transport,
    )
    try:
        async with (
            ClientSession() as client,
            client.post(
                f"{gateway.base_url}/v1/chat/completions",
                json={
                    "model": "wrong",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
                headers={"authorization": f"Bearer {session.token}"},
            ) as response,
        ):
            assert response.status == 200
            assert b"chatcmpl_2" in await response.read()
    finally:
        await gateway.stop()
        await upstream.stop()

    assert upstream.payload["stream_options"]["include_usage"] is True
    assert session.usage.input_tokens == 6
    assert session.usage.output_tokens == 4
    (record,) = read_jsonl(recording.transport.path).records
    assert record["provider_response_id"] == "chatcmpl_2"


def test_chat_stream_usage_may_cross_network_chunk_boundaries() -> None:
    event = (
        b'data: {"id":"chatcmpl_1","choices":[{"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":6,"completion_tokens":4}}\n\ndata: [DONE]\n\n'
    )

    assert usage_from_stream([event[:31], event[31:79], event[79:]]) == (6, 4, "stop")


@pytest.mark.parametrize(
    ("limits", "expected_limit"),
    [
        (Limits(max_input_tokens=5), "max_input_tokens"),
        (Limits(max_total_tokens=9), "max_total_tokens"),
        (Limits(max_cost_usd=0.000009), "max_cost_usd"),
    ],
)
async def test_chat_usage_limits_stop_the_call_after_the_threshold(
    tmp_path: Path,
    limits: Limits,
    expected_limit: str,
) -> None:
    upstream = ChatUpstream()
    await upstream.start()
    gateway = Gateway(
        api_key="real-key",
        upstream=upstream.url,
        dialect="openai-chat-completions",
        host="127.0.0.1",
    )
    await gateway.start()
    session = gateway.open_session(
        GatewaySession(episode_id="episode", model="gpt-5-mini", limits=limits)
    )
    try:
        async with ClientSession() as client:
            first = await client.post(
                f"{gateway.base_url}/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "first"}],
                    "max_completion_tokens": 100,
                },
                headers={"authorization": f"Bearer {session.token}"},
            )
            assert first.status == 200
            await first.read()
            second = await client.post(
                f"{gateway.base_url}/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "second"}],
                    "max_completion_tokens": 100,
                },
                headers={"authorization": f"Bearer {session.token}"},
            )
            body = json.loads(await second.text())
            assert second.status == 429
    finally:
        await gateway.stop()
        await upstream.stop()

    assert body["error"]["ale"]["limit"] == expected_limit
    assert upstream.calls == 1
