"""The Gemini dialect against a local stand-in for generativelanguage.googleapis.com."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from ale.core.config import GatewayConfig
from ale.core.trace import read_jsonl
from ale.run.gateway.gemini import extract_usage, stream_completed, usage_from_stream
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import CallUsage, GatewaySession, Limits
from ale.run.recording import EpisodeRecording
from ale.run.secrets import provider_credentials

pytestmark = pytest.mark.unit

REAL_KEY = "host-gemini-key"
MODEL = "gemini-3.1-pro-preview"
HELPER = "gemini-3.1-flash-lite-preview"
STREAM = f"models/{HELPER}:streamGenerateContent?alt=sse"
GENERATE = f"models/{HELPER}:generateContent"
BODY = {
    "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
    "generationConfig": {"maxOutputTokens": 100},
}


@dataclass
class Seen:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: dict | None


class GeminiUpstream:
    """Answers like the Gemini API and remembers every request that reached it."""

    def __init__(self) -> None:
        self.seen: list[Seen] = []
        self.status = 200
        self.candidates_tokens = 7
        self.stream_gap = 0.0
        self.streamed = b""
        self.runner: web.AppRunner | None = None
        self.url = ""

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    def usage(self, body: dict) -> dict[str, int]:
        ceiling = (body.get("generationConfig") or {}).get("maxOutputTokens", 10**9)
        candidates = min(self.candidates_tokens, ceiling)
        return {
            "promptTokenCount": 120,
            "cachedContentTokenCount": 40,
            "candidatesTokenCount": candidates,
            "thoughtsTokenCount": 5,
            "totalTokenCount": 125 + candidates,
        }

    async def handle(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        body = json.loads(raw) if raw else None
        self.seen.append(
            Seen(request.method, request.path, dict(request.query), dict(request.headers), body)
        )
        if self.status != 200:
            error = {"error": {"code": self.status, "message": "busy", "status": "UNAVAILABLE"}}
            return web.json_response(error, status=self.status)
        assert body is not None
        if request.path.endswith(":countTokens"):
            return web.json_response({"totalTokens": 120})
        final = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "ok"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": self.usage(body),
            "responseId": "resp-1",
        }
        if not request.path.endswith(":streamGenerateContent"):
            return web.json_response(final)
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        thought = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "…", "thought": True}]}}
            ],
            "usageMetadata": {"promptTokenCount": 120, "totalTokenCount": 120},
            "responseId": "resp-1",
        }
        chunks = [f"data: {json.dumps(event)}\r\n\r\n".encode() for event in (thought, final)]
        self.streamed = b"".join(chunks)
        await response.write(chunks[0])
        await asyncio.sleep(self.stream_gap)
        await response.write(chunks[1])
        await response.write_eof()
        return response


@pytest.fixture
async def stack(tmp_path: Path):
    upstream = GeminiUpstream()
    await upstream.start()
    gateway = Gateway(api_key=REAL_KEY, upstream=upstream.url, dialect="gemini", host="127.0.0.1")
    await gateway.start()
    trace = EpisodeRecording(tmp_path).transport
    session = gateway.open_session(GatewaySession(episode_id="ep", model=MODEL), trace)
    try:
        yield gateway, upstream, session, trace
    finally:
        await gateway.stop()
        await upstream.stop()


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    chunks: list[bytes]

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    def json(self) -> dict:
        return json.loads(self.body)


async def send(
    gateway: Gateway,
    target: str,
    body: dict | None = None,
    *,
    token: str | None = None,
    key: str | None = None,
    prefix: str = "",
    method: str = "POST",
) -> Reply:
    url = f"{gateway.base_url}{prefix}/v1beta/{target}"
    if key is not None:
        url += ("&" if "?" in url else "?") + f"key={key}"
    headers = {"x-goog-api-key": token} if token is not None else {}
    async with (
        ClientSession() as client,
        client.request(method, url, json=body, headers=headers) as response,
    ):
        chunks = [chunk async for chunk in response.content.iter_any()]
        return Reply(response.status, dict(response.headers), chunks)


def records(trace) -> list[dict]:
    return [record for record in read_jsonl(trace.path).records if record["kind"] == "call"]


def google_error(reply: Reply, code: int, status: str) -> dict:
    error = reply.json()["error"]
    assert (error["code"], error["status"]) == (code, status)
    assert error["details"][0]["@type"] == "type.googleapis.com/google.rpc.ErrorInfo"
    return error


class TestRouting:
    @pytest.mark.parametrize("prefix", ["", "/gemini/episode"])
    async def test_routes_at_the_root_and_below_a_prefix(self, stack, prefix: str) -> None:
        gateway, upstream, session, _ = stack

        reply = await send(gateway, STREAM, BODY, token=session.token, prefix=prefix)

        assert reply.status == 200
        (seen,) = upstream.seen
        assert seen.path == f"/v1beta/models/{MODEL}:streamGenerateContent"
        assert seen.query == {"alt": "sse"}

    async def test_the_run_model_replaces_every_requested_model(self, stack) -> None:
        """agy's fixed helper model is rewritten like its main model: one run, one model."""
        gateway, upstream, session, trace = stack

        for requested in (HELPER, "gemini-3.8-flash"):
            # Distinct labels keep the second call from being a replay of the first.
            body = {**BODY, "model": f"models/{requested}", "labels": {"call": requested}}
            target = f"models/{requested}:generateContent"
            assert (await send(gateway, target, body, token=session.token)).status == 200

        rewritten = f"/v1beta/models/{MODEL}:generateContent"
        assert [seen.path for seen in upstream.seen] == [rewritten, rewritten]
        assert not any("model" in (seen.body or {}) for seen in upstream.seen)
        assert [record["model"] for record in records(trace)] == [MODEL, MODEL]

    async def test_another_dialects_session_is_not_served(self, stack) -> None:
        _, upstream, _, _ = stack
        other = Gateway(api_key="k", upstream=upstream.url, host="127.0.0.1")
        await other.start()
        session = other.open_session(GatewaySession(episode_id="a", model="claude-opus-4-8"))
        try:
            reply = await send(other, STREAM, BODY, token=session.token)
        finally:
            await other.stop()

        google_error(reply, 404, "NOT_FOUND")
        assert upstream.seen == []


class TestAuthentication:
    async def test_header_and_query_tokens_are_swapped_for_the_host_key(self, stack) -> None:
        gateway, upstream, session, _ = stack

        by_header = await send(gateway, STREAM, BODY, token=session.token)
        by_query = await send(gateway, STREAM, {**BODY, "n": 2}, key=session.token)

        assert (by_header.status, by_query.status) == (200, 200)
        for seen in upstream.seen:
            assert seen.headers["x-goog-api-key"] == REAL_KEY
            assert "key" not in seen.query
            assert session.token not in json.dumps(seen.headers)

    @pytest.mark.parametrize("credential", [{}, {"token": "forged"}, {"key": "forged"}])
    async def test_without_a_live_token_the_request_is_unauthenticated(
        self, stack, credential: dict[str, str]
    ) -> None:
        gateway, upstream, _, trace = stack

        reply = await send(gateway, STREAM, BODY, **credential)

        google_error(reply, 401, "UNAUTHENTICATED")
        assert upstream.seen == []
        assert not trace.path.exists() or records(trace) == []

    async def test_a_query_key_authenticates_only_gemini_routes(self, stack) -> None:
        gateway, _, session, _ = stack
        async with (
            ClientSession() as client,
            client.post(
                f"{gateway.base_url}/v1/messages?key={session.token}", json={"messages": []}
            ) as response,
        ):
            assert response.status == 401


class TestPolicy:
    @pytest.mark.parametrize(
        "tools",
        [
            [{"googleSearch": {}}],
            [{"google_search": {}}],
            [{"functionDeclarations": [{"name": "run"}]}, {"urlContext": {}}],
        ],
    )
    async def test_grounding_is_refused(self, stack, tools: list[dict]) -> None:
        """Google would search the web outside the Task's network policy, billed apart."""
        gateway, upstream, session, trace = stack

        reply = await send(gateway, GENERATE, {**BODY, "tools": tools}, token=session.token)

        error = google_error(reply, 400, "FAILED_PRECONDITION")
        assert "grounding needs a Task with open network" in error["message"]
        assert upstream.seen == []
        (record,) = records(trace)
        assert (record["disposition"], record["refusal_limit"]) == ("refused", "web_tools")
        assert session.refusal is None, "a policy refusal is not a budget"

    async def test_grounding_is_forwarded_for_a_task_with_open_network(self, stack) -> None:
        """An open Task may reach the web anyway, so Google may do it on its behalf."""
        gateway, upstream, session, _ = stack
        session.provider_web_tools = True

        reply = await send(
            gateway, GENERATE, {**BODY, "tools": [{"googleSearch": {}}]}, token=session.token
        )

        assert reply.status == 200
        assert len(upstream.seen) == 1

    @pytest.mark.parametrize(
        ("method", "prefix", "target"),
        [
            ("POST", "", f"models/{HELPER}:embedContent"),
            ("POST", "", "cachedContents"),
            ("GET", "", "models"),
            ("GET", "", f"models/{HELPER}"),
            ("GET", "", GENERATE),
            ("POST", "/upload", "files"),
        ],
    )
    async def test_other_methods_are_refused_not_forwarded(
        self, stack, method: str, prefix: str, target: str
    ) -> None:
        gateway, upstream, session, trace = stack
        body = BODY if method == "POST" else None

        reply = await send(gateway, target, body, token=session.token, prefix=prefix, method=method)

        google_error(reply, 404, "NOT_FOUND")
        assert upstream.seen == []
        (record,) = records(trace)
        assert record["refusal_limit"] == "unsupported_method"

    async def test_token_counting_is_forwarded_on_the_runs_model(self, stack) -> None:
        """It samples nothing and stores nothing with Google, so there is no reason to refuse it."""
        gateway, upstream, session, trace = stack

        reply = await send(gateway, f"models/{HELPER}:countTokens", BODY, token=session.token)

        assert reply.status == 200
        assert reply.json()["totalTokens"] == 120
        (seen,) = upstream.seen
        assert seen.path == f"/v1beta/models/{MODEL}:countTokens"
        (record,) = records(trace)
        assert record["disposition"] == "forwarded"
        assert (record["input_tokens"], record["output_tokens"]) == (0, 0)

    async def test_a_stream_must_be_server_sent_events(self, stack) -> None:
        """A JSON-array stream would carry usage the Gateway does not parse."""
        gateway, upstream, session, _ = stack

        reply = await send(gateway, STREAM.removesuffix("?alt=sse"), BODY, token=session.token)

        google_error(reply, 400, "INVALID_ARGUMENT")
        assert upstream.seen == []


class TestStreaming:
    async def test_a_quiet_stream_passes_through_without_keep_alive_comments(self, stack) -> None:
        """agy fails a turn, without retrying, on ``: keep-alive``."""
        gateway, upstream, session, trace = stack
        gateway.sse_keepalive_sec = 0.05
        upstream.stream_gap = 0.4

        reply = await send(gateway, STREAM, BODY, token=session.token)

        assert reply.status == 200
        assert reply.headers["Content-Type"].startswith("text/event-stream")
        assert reply.body == upstream.streamed
        assert not any(line.startswith(b":") for line in reply.body.splitlines())
        (record,) = records(trace)
        assert record["disposition"] == "forwarded"

    async def test_tool_round_trips_are_forwarded_unchanged(self, stack) -> None:
        gateway, upstream, session, _ = stack
        body = {
            **BODY,
            "systemInstruction": {"parts": [{"text": "be brief"}]},
            "tools": [{"functionDeclarations": [{"name": "run", "parametersJsonSchema": {}}]}],
            "contents": [
                {"role": "user", "parts": [{"text": "list files"}]},
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"id": "c1", "name": "run", "args": {"cmd": "ls"}},
                            "thoughtSignature": "c2lnbmF0dXJl",
                        }
                    ],
                },
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": "c1",
                                "name": "run",
                                "response": {"output": "é"},
                            }
                        }
                    ],
                },
            ],
        }

        assert (await send(gateway, STREAM, body, token=session.token)).status == 200
        assert upstream.seen[0].body == body


class TestAccounting:
    async def test_streamed_usage_comes_from_the_last_usage_metadata(self, stack) -> None:
        gateway, _, session, trace = stack

        await send(gateway, STREAM, BODY, token=session.token)

        (record,) = records(trace)
        assert (record["input_tokens"], record["cache_read_tokens"]) == (120, 40)
        assert record["output_tokens"] == 7 + 5, "thinking tokens are output"
        assert record["stop_reason"] == "STOP"
        assert record["provider_response_id"] == "resp-1"

    async def test_generate_content_passes_through_and_a_retry_is_replayed(self, stack) -> None:
        gateway, upstream, session, trace = stack

        first = await send(gateway, GENERATE, BODY, token=session.token)
        retry = await send(gateway, GENERATE, BODY, token=session.token)

        assert first.status == retry.status == 200
        assert first.headers["Content-Type"].startswith("application/json")
        assert first.json()["responseId"] == "resp-1" and retry.body == first.body
        assert len(upstream.seen) == 1
        kinds = [record["kind"] for record in read_jsonl(trace.path).records]
        assert kinds == ["call", "replay"]
        (record,) = records(trace)
        assert (record["input_tokens"], record["output_tokens"]) == (120, 12)

    async def test_one_body_streamed_and_not_is_two_requests(self, stack) -> None:
        gateway, upstream, session, _ = stack

        await send(gateway, GENERATE, BODY, token=session.token)
        streamed = await send(gateway, STREAM, BODY, token=session.token)

        assert len(upstream.seen) == 2
        assert streamed.body.startswith(b"data: ")


class TestLimits:
    async def test_max_output_tokens_is_forwarded_as_sent_and_never_invented(self, stack) -> None:
        gateway, upstream, session, _ = stack

        await send(gateway, STREAM, BODY, token=session.token)
        await send(gateway, STREAM, {"contents": BODY["contents"]}, token=session.token)

        assert upstream.seen[0].body["generationConfig"] == {"maxOutputTokens": 100}
        assert "generationConfig" not in upstream.seen[1].body

    async def test_an_output_ceiling_refuses_the_next_call_as_a_google_error(self, stack) -> None:
        gateway, upstream, session, trace = stack
        session.limits = Limits(max_output_tokens=10)
        upstream.candidates_tokens = 50

        first = await send(
            gateway,
            STREAM,
            {**BODY, "generationConfig": {"maxOutputTokens": 6}},
            token=session.token,
        )
        second = await send(gateway, STREAM, BODY, token=session.token)

        assert first.status == 200
        error = google_error(second, 400, "FAILED_PRECONDITION")
        assert error["details"][0]["metadata"]["limit"] == "max_output_tokens"
        assert second.headers["x-should-retry"] == "false"
        assert session.refusal is not None and session.refusal.limit == "max_output_tokens"
        assert len(upstream.seen) == 1
        assert [record["disposition"] for record in records(trace)] == ["forwarded", "refused"]


class TestErrors:
    async def test_an_upstream_error_passes_through_and_its_retry_is_forwarded(self, stack) -> None:
        gateway, upstream, session, trace = stack
        upstream.status = 503

        failed = await send(gateway, STREAM, BODY, token=session.token)
        upstream.status = 200
        retried = await send(gateway, STREAM, BODY, token=session.token)

        assert failed.status == 503
        assert failed.json() == {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}}
        assert retried.status == 200
        assert len(upstream.seen) == 2
        dispositions = [(r["disposition"], r.get("upstream_status")) for r in records(trace)]
        assert dispositions == [("failed", 503), ("forwarded", None)]


class TestDialect:
    def test_stream_usage_takes_the_last_report_across_chunk_boundaries(self) -> None:
        stream = (
            b'data: {"usageMetadata":{"promptTokenCount":9,"totalTokenCount":9}}\r\n\r\n'
            b'data: {"candidates":[{"finishReason":"MAX_TOKENS"}],"usageMetadata":'
            b'{"promptTokenCount":9,"toolUsePromptTokenCount":1,"cachedContentTokenCount":4,'
            b'"candidatesTokenCount":3,"thoughtsTokenCount":2,"totalTokenCount":15}}\r\n\r\n'
        )
        assert usage_from_stream([stream[:50], stream[50:111], stream[111:]]) == (
            CallUsage(input_tokens=10, cache_read_tokens=4, output_tokens=5),
            "MAX_TOKENS",
        )

    def test_only_a_finished_candidate_without_an_error_completes_a_stream(self) -> None:
        part = b'data: {"candidates":[{"content":{"parts":[{"text":"a"}]}}]}\r\n\r\n'
        done = b'data: {"candidates":[{"finishReason":"STOP"}]}\r\n\r\n'
        error = b'data: {"error":{"code":500,"status":"INTERNAL"}}\r\n\r\n'
        blocked = b'data: {"promptFeedback":{"blockReason":"SAFETY"}}\r\n\r\n'
        assert stream_completed([part, done])
        assert stream_completed([blocked])
        assert not stream_completed([part])
        assert not stream_completed([part, error, done])

    def test_a_response_without_usage_is_counted_as_zero(self) -> None:
        assert extract_usage({"candidates": []}) == (CallUsage(), None)

    def test_the_gemini_provider_defaults_to_googles_api(self, monkeypatch) -> None:
        monkeypatch.setenv("ALE_GEMINI_API_KEY", "from-env")
        monkeypatch.delenv("ALE_GEMINI_BASE_URL", raising=False)
        config = GatewayConfig(provider="gemini", dialect="gemini")
        assert (config.provider, config.dialect) == ("gemini", "gemini")
        assert provider_credentials("gemini") == (
            "from-env",
            "https://generativelanguage.googleapis.com",
        )
