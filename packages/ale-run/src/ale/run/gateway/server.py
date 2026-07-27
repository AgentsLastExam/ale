"""The model gateway.

A standalone host-side proxy. Its only interface to a sandbox is a reachable URL and a
per-episode bearer token, so it knows nothing about containers, virtual machines or any
provider — wiring the route is the provider's job.

What it buys, none of which an agent can opt out of:

* real credentials stay on the host; a sandbox holds a token that is worthless elsewhere
  and is revoked when the episode ends;
* ceilings are enforced by refusing the next call, so budgets bind agents that were
  never written to respect them;
* every call is recorded, so "did this agent call the model?" is answered from evidence
  rather than from the agent's own report;
* the model is imposed rather than requested, so a run measures what it says it does.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from ale.core.trace import TraceLayer, TraceWriter, TransportRecord
from ale.run.gateway.anthropic import (
    estimate_cost,
    extract_usage,
    refusal_body,
    usage_from_stream,
)
from ale.run.gateway.session import GatewaySession, LimitReached, SessionRegistry

__all__ = ["Gateway"]

UPSTREAM_DEFAULT = "https://api.anthropic.com"
_REQUEST_TIMEOUT = ClientTimeout(total=1800)

#: Hop-by-hop and auth headers never forwarded upstream: the gateway supplies its own.
_STRIP = frozenset(
    {"host", "content-length", "authorization", "x-api-key", "connection", "accept-encoding"}
)


class Gateway:
    """Serves model traffic for many episodes at once."""

    def __init__(
        self,
        *,
        api_key: str,
        upstream: str = UPSTREAM_DEFAULT,
        host: str = "0.0.0.0",
        port: int = 0,
    ) -> None:
        self.api_key = api_key
        self.upstream = upstream.rstrip("/")
        self.host = host
        self.port = port
        self.sessions = SessionRegistry()
        self._traces: dict[str, TraceWriter] = {}
        self._app = self._build_app()
        self._runner: web.AppRunner | None = None
        self._client: ClientSession | None = None

    # --- lifecycle ---

    async def start(self) -> str:
        """Start serving and return the base URL."""
        self._client = ClientSession(timeout=_REQUEST_TIMEOUT)
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        sockets = getattr(site._server, "sockets", None)
        if sockets:
            self.port = sockets[0].getsockname()[1]
        return self.base_url

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def open_session(
        self, session: GatewaySession, trace: TraceWriter | None = None
    ) -> GatewaySession:
        """Register an episode. Its token is valid until :meth:`close_session`."""
        if trace is not None:
            self._traces[session.token] = trace
        return self.sessions.open(session)

    def close_session(self, session: GatewaySession) -> None:
        self.sessions.close(session)
        self._traces.pop(session.token, None)

    # --- routes ---

    def _build_app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/healthz", self._healthz)
        app.router.add_post("/v1/messages", self._messages)
        return app

    async def _healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "dialects": ["anthropic"]})

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        session = self._authenticate(request)
        started = asyncio.get_running_loop().time()
        body = await request.read()

        try:
            session.check()
        except LimitReached as limit:
            self._record(session, request_digest=GatewaySession.digest(body), refused=limit.limit)
            return web.json_response(refusal_body(limit.limit, limit.value), status=429)

        payload = self._authoritative(json.loads(body or b"{}"), session)
        key = GatewaySession.digest(json.dumps(payload, sort_keys=True).encode())

        if (cached := session.cached(key)) is not None:
            return self._replayed(cached)  # a retry must not sample twice
        if (pending := session.inflight(key)) is not None:
            return self._replayed(await pending)  # …nor while the first is in flight

        session.begin(key)
        try:
            response = await self._forward(request, session, payload, key, started)
        except Exception as exc:
            session.finish(key, None, error=exc)
            raise
        return response

    # --- forwarding ---

    async def _forward(
        self,
        request: web.Request,
        session: GatewaySession,
        payload: dict[str, Any],
        key: str,
        started: float,
    ) -> web.StreamResponse:
        assert self._client is not None
        headers = {
            name: value for name, value in request.headers.items() if name.lower() not in _STRIP
        }
        headers["x-api-key"] = self.api_key
        headers.setdefault("anthropic-version", "2023-06-01")

        streaming = bool(payload.get("stream"))
        async with self._client.post(
            f"{self.upstream}/v1/messages", json=payload, headers=headers
        ) as upstream:
            if streaming:
                return await self._stream(request, session, upstream, key, payload, started)

            raw = await upstream.read()
            parsed = json.loads(raw or b"{}") if upstream.status == 200 else {}
            self._account(
                session,
                parsed,
                payload,
                streamed=None,
                status=upstream.status,
                latency_ms=_elapsed_ms(started),
            )
            cached = {"status": upstream.status, "body": raw, "streaming": False}
            session.finish(key, cached)
            return web.Response(body=raw, status=upstream.status, content_type="application/json")

    async def _stream(
        self,
        request: web.Request,
        session: GatewaySession,
        upstream: Any,
        key: str,
        payload: dict[str, Any],
        started: float,
    ) -> web.StreamResponse:
        """Pass the event stream straight through, counting usage as it goes."""
        response = web.StreamResponse(
            status=upstream.status, headers={"content-type": "text/event-stream"}
        )
        await response.prepare(request)

        chunks: list[bytes] = []
        async for chunk in _iter_chunks(upstream):
            chunks.append(chunk)
            await response.write(chunk)
        await response.write_eof()

        # Measured to the end of the stream: a streamed call's cost in wall-clock time is
        # how long the agent waited for all of it, not how quickly the first byte arrived.
        self._account(
            session,
            {},
            payload,
            streamed=chunks,
            status=upstream.status,
            latency_ms=_elapsed_ms(started),
        )
        session.finish(
            key, {"status": upstream.status, "body": b"".join(chunks), "streaming": True}
        )
        return response

    def _replayed(self, cached: dict[str, Any]) -> web.Response:
        content_type = "text/event-stream" if cached["streaming"] else "application/json"
        return web.Response(body=cached["body"], status=cached["status"], content_type=content_type)

    # --- policy and accounting ---

    def _authenticate(self, request: web.Request) -> GatewaySession:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() or request.headers.get("x-api-key", "")
        session = self.sessions.resolve(token)
        if session is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"type": "error", "error": {"type": "ale_unknown_session"}}),
                content_type="application/json",
            )
        return session

    def _authoritative(self, payload: dict[str, Any], session: GatewaySession) -> dict[str, Any]:
        """Impose the run's model rather than trusting the agent's request."""
        return {**payload, "model": session.model}

    def _account(
        self,
        session: GatewaySession,
        parsed: dict[str, Any],
        payload: dict[str, Any],
        *,
        streamed: list[bytes] | None,
        status: int,
        latency_ms: int = 0,
    ) -> None:
        request_digest = GatewaySession.digest(json.dumps(payload, sort_keys=True).encode())
        if status != 200:
            # A failed call is still a call. Recording it costs one line and is the
            # difference between "the agent stalled" and "the provider was returning
            # 529s for eleven minutes" — the same evidence, opposite conclusions.
            self._record(
                session,
                request_digest=request_digest,
                upstream_status=status,
                latency_ms=latency_ms,
            )
            return
        if streamed is not None:
            input_tokens, output_tokens, stop_reason = usage_from_stream(streamed)
        else:
            input_tokens, output_tokens, stop_reason = extract_usage(parsed)
        cost = estimate_cost(session.model, input_tokens, output_tokens)
        session.record(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)
        self._record(
            session,
            request_digest=request_digest,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            stop_reason=stop_reason,
            latency_ms=latency_ms,
        )

    def _record(
        self,
        session: GatewaySession,
        *,
        request_digest: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        stop_reason: str | None = None,
        refused: str | None = None,
        upstream_status: int | None = None,
        latency_ms: int = 0,
    ) -> None:
        trace = self._traces.get(session.token)
        if trace is None:
            return
        trace.write_transport(
            TransportRecord(
                seq=trace.next_seq(TraceLayer.TRANSPORT),
                episode_id=session.episode_id,
                model=session.model,
                request_digest=f"sha256:{request_digest}",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                stop_reason=stop_reason,
                refused=refused is not None,
                refusal_limit=refused,
                upstream_status=upstream_status,
                latency_ms=latency_ms,
            )
        )


async def _iter_chunks(upstream: Any) -> AsyncIterator[bytes]:
    async for chunk in upstream.content.iter_any():
        yield chunk


def _elapsed_ms(started: float) -> int:
    """Milliseconds since ``started``, on the loop's own clock."""
    return int((asyncio.get_running_loop().time() - started) * 1000)
