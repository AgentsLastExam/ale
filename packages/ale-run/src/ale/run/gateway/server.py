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
import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from ale.core.environment import EventSink
from ale.core.errors import ConfigError
from ale.core.trace import TransportCall, TransportReplay
from ale.run.gateway import anthropic, gemini, openai_chat_completions, openai_responses
from ale.run.gateway.session import (
    CallUsage,
    GatewayReservation,
    GatewaySession,
    LimitReached,
    SessionRegistry,
    Upstream,
)
from ale.run.subscription import SubscriptionCredential

__all__ = ["REQUEST_PATHS", "Gateway"]

UPSTREAM_DEFAULT = "https://api.anthropic.com"
_REQUEST_TIMEOUT = ClientTimeout(total=1800)

#: Seconds of upstream silence after which a streaming client is sent an SSE comment.
#:
#: A provider that buffers part of a response — the Messages API holds back a tool_use
#: input until the whole parameter is generated unless the tool asks for eager input
#: streaming — can leave a stream with no bytes for as long as the model takes to write
#: it. Every HTTP client has an idle timer that gives up before that: Bun's fetch, which
#: Claude Code runs on, aborts after 300 s of silence; Node's undici uses the same 300 s
#: for its body timeout. The agent then re-issues the request while the first upstream
#: call keeps running and being billed. A comment line is the event-stream standard's
#: heartbeat: every SSE parser discards it, so the client's timer is fed without the
#: recorded response changing. Well under every known default; ``None`` disables it.
SSE_KEEPALIVE_SEC = 15.0
_SSE_KEEPALIVE = b": keep-alive\n\n"

#: Hop-by-hop and auth headers never forwarded upstream: the gateway supplies its own.
_STRIP = frozenset(
    {
        "host",
        "content-length",
        "authorization",
        "x-api-key",
        "x-goog-api-key",
        "connection",
        "accept-encoding",
    }
)
_DIALECTS = {
    "anthropic": anthropic,
    "gemini": gemini,
    "openai-chat-completions": openai_chat_completions,
    "openai-responses": openai_responses,
}
#: Fixed request paths. Gemini's carry the model and method instead; see ``_GEMINI_ROUTE``.
REQUEST_PATHS = {
    "anthropic": "/v1/messages",
    "openai-chat-completions": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
}
#: Gemini's ``{base}/v1beta/models/{model}:{method}``, at the root or below any prefix: the
#: client keeps a path in its base URL, and the Gateway forwards none of it.
_GEMINI_ROUTE = "/{prefix:(?:.*/)?}v1beta/{target:.*}"
_MAX_OUTPUT_FIELDS = {
    "anthropic": "max_tokens",
    "openai-chat-completions": "max_completion_tokens",
    "openai-responses": "max_output_tokens",
}


class Gateway:
    """Serves model traffic for many episodes at once."""

    def __init__(
        self,
        *,
        api_key: str = "",
        upstream: str = UPSTREAM_DEFAULT,
        dialect: str = "anthropic",
        bearer_auth: bool = False,
        subscription: SubscriptionCredential | None = None,
        host: str = "0.0.0.0",
        port: int = 0,
        sse_keepalive_sec: float | None = SSE_KEEPALIVE_SEC,
    ) -> None:
        if subscription is not None:
            upstream = subscription.upstream
            dialect = subscription.dialect
        self.api_key = api_key
        self.upstream = upstream.rstrip("/")
        if dialect not in _DIALECTS:
            raise ConfigError(f"unsupported gateway dialect {dialect!r}")
        self.dialect = dialect
        self.bearer_auth = bearer_auth
        self.subscription = subscription
        self.upstream_path = subscription.upstream_path if subscription is not None else None
        self.host = host
        self.port = port
        self.sse_keepalive_sec = sse_keepalive_sec
        self.sessions = SessionRegistry()
        self._traces: dict[str, EventSink] = {}
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
        self, session: GatewaySession, trace: EventSink | None = None
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
        # First, so every `v1beta` path is Gemini's, its model listing included.
        app.router.add_route("*", _GEMINI_ROUTE, self._gemini)
        # `/models`, `/v1/models`, and a CLI's own catalog path such as `/muse-code/models`.
        app.router.add_get(r"/{prefix:(?:[\w.-]+/)*}models", self._models)
        for path in REQUEST_PATHS.values():
            app.router.add_post(path, self._messages)
        return app

    async def _healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "dialects": [self.dialect]})

    async def _models(self, request: web.Request) -> web.Response:
        """The model catalog a session sees: exactly the model its run imposes.

        Some CLIs fetch a catalog before their first call and stop without one. Listing
        only the run's model keeps the agent's view consistent with what calls receive.
        """
        session = self._authenticate(request)
        return web.json_response(
            {"object": "list", "data": [{"id": session.model, "object": "model"}]}
        )

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        session = self._authenticate(request)
        if request.path != REQUEST_PATHS.get(self._dialect_name(session)):
            raise web.HTTPNotFound(
                text=json.dumps({"type": "error", "error": {"type": "ale_wrong_dialect"}}),
                content_type="application/json",
            )
        started = asyncio.get_running_loop().time()
        body = await request.read()
        payload = self._authoritative(json.loads(body or b"{}"), session)
        return await self._call(
            request, session, payload, started=started, streaming=bool(payload.get("stream"))
        )

    async def _gemini(self, request: web.Request) -> web.StreamResponse:
        """Serve a Gemini API request, refusing what the Gateway cannot account or allow."""
        started = asyncio.get_running_loop().time()
        token = request.headers.get("x-goog-api-key") or request.query.get("key", "")
        session = self.sessions.resolve(token)
        if session is None:
            return _google_error(
                401, "UNAUTHENTICATED", "unknown ALE episode token", "ALE_UNKNOWN_SESSION"
            )
        if self._dialect_name(session) != "gemini":
            return _google_error(
                404,
                "NOT_FOUND",
                f"this episode's Gateway dialect is {self._dialect_name(session)}, not gemini",
                "ALE_WRONG_DIALECT",
            )
        try:
            payload = json.loads(await request.read() or b"{}")
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            return _google_error(
                400, "INVALID_ARGUMENT", "request body is not a JSON object", "ALE_INVALID_BODY"
            )
        payload = self._authoritative(payload, session)

        target = request.match_info["target"]
        refusal = gemini.policy_refusal(
            request.method,
            target,
            request.query.get("alt"),
            payload,
            web_tools=session.provider_web_tools,
        )
        if refusal is not None:
            self._record(
                session,
                call_id=f"call_{uuid.uuid4().hex}",
                request_digest=_request_digest(payload, f"{request.method} {request.path}"),
                refused=refusal.policy,
            )
            return web.json_response(refusal.body(), status=refusal.code)
        method = target.rpartition(":")[2]
        streaming = gemini.METHODS[method]
        # The run's model replaces whichever one the client named, helper models included.
        path = f"/v1beta/models/{session.model}:{method}" + ("?alt=sse" if streaming else "")
        return await self._call(
            request, session, payload, started=started, streaming=streaming, path=path
        )

    async def _call(
        self,
        request: web.Request,
        session: GatewaySession,
        payload: dict[str, Any],
        *,
        started: float,
        streaming: bool,
        path: str | None = None,
    ) -> web.StreamResponse:
        """Replay, coalesce or forward one authorized request.

        ``path`` is the upstream path when it varies per call, as Gemini's does; ``None``
        selects the Gateway's fixed path for the session's dialect.
        """
        key = _request_digest(payload, path)
        request_digest = f"sha256:{key}"

        replays = session.purpose == "agent"
        if replays and (cached := session.cached(key)) is not None:
            self._record_replay(
                session,
                call_id=cached["call_id"],
                request_digest=request_digest,
                reason="completed-cache",
            )
            return self._replayed(cached)  # a retry must not sample twice
        if replays and (pending := session.inflight(key)) is not None:
            cached = await pending
            self._record_replay(
                session,
                call_id=cached["call_id"],
                request_digest=request_digest,
                reason="inflight-coalesced",
            )
            return self._replayed(cached)  # …nor while the first is in flight

        session.begin(key)
        call_id = f"call_{uuid.uuid4().hex}"
        try:
            response = await self._forward(
                request, session, payload, key, call_id, started, streaming=streaming, path=path
            )
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
        call_id: str,
        started: float,
        *,
        streaming: bool,
        path: str | None = None,
    ) -> web.StreamResponse:
        assert self._client is not None
        try:
            reservation = await session.reserve()
        except LimitReached as limit:
            if session.refusal is None:
                session.refusal = limit
            self._record(
                session,
                call_id=call_id,
                request_digest=_request_digest(payload, path),
                refused=limit.limit,
            )
            raw = json.dumps(
                self._dialect(session).refusal_body(limit.limit, limit.value, limit.observed)
            ).encode()
            self._retain_payload(session, call_id, payload, raw)
            cached = {
                "status": (
                    gemini.REFUSAL_STATUS if self._dialect_name(session) == "gemini" else 429
                ),
                "body": raw,
                "streaming": False,
                "call_id": call_id,
                # A limit does not lift by waiting; SDKs that honor this stop retrying.
                "headers": {"x-should-retry": "false"},
            }
            session.finish(key, cached)
            return self._replayed(cached)
        payload = self._normalized_output_limit(payload, session)
        self._retain_payload(session, call_id, payload, None)

        committed = False
        try:
            upstream = await self._post(request, payload, session, path)
            try:
                if streaming:
                    response = await self._stream(
                        request,
                        session,
                        upstream,
                        key,
                        payload,
                        call_id,
                        reservation,
                        started,
                        path=path,
                    )
                    committed = True
                    return response

                raw = await upstream.read()
                self._retain_payload(session, call_id, None, raw)
                parsed = json.loads(raw or b"{}") if upstream.status == 200 else {}
                await self._account(
                    session,
                    parsed,
                    payload,
                    call_id=call_id,
                    reservation=reservation,
                    streamed=None,
                    status=upstream.status,
                    latency_ms=_elapsed_ms(started),
                    response_body=raw,
                    provider_response_id=_response_id(parsed),
                    path=path,
                )
                committed = True
                cached = {
                    "status": upstream.status,
                    "body": raw,
                    "streaming": False,
                    "call_id": call_id,
                }
                session.finish(key, cached, replayable=_replayable(upstream.status))
                return web.Response(
                    body=raw,
                    status=upstream.status,
                    content_type="application/json",
                )
            finally:
                upstream.release()
        except BaseException:
            if not committed:
                self._record(
                    session,
                    call_id=call_id,
                    request_digest=_request_digest(payload, path),
                    disposition="failed",
                    latency_ms=_elapsed_ms(started),
                )
                await session.commit(reservation)
            raise

    async def _stream(
        self,
        request: web.Request,
        session: GatewaySession,
        upstream: Any,
        key: str,
        payload: dict[str, Any],
        call_id: str,
        reservation: GatewayReservation,
        started: float,
        *,
        path: str | None = None,
    ) -> web.StreamResponse:
        """Pass the event stream straight through, counting usage as it goes."""
        # The upstream's own type: one that answers a streamed request with plain JSON
        # must not reach the client labelled as an event stream.
        response = web.StreamResponse(
            status=upstream.status,
            headers={"content-type": upstream.headers.get("content-type", "text/event-stream")},
        )
        await response.prepare(request)

        chunks: list[bytes] = []
        downstream_open = True
        # Antigravity CLI rejects an SSE comment as an invalid chunk and fails the turn
        # without retrying, so a Gemini stream carries the provider's bytes and nothing else.
        keepalive = None if self._dialect_name(session) == "gemini" else self.sse_keepalive_sec
        async for chunk in _iter_chunks(upstream, keepalive_sec=keepalive):
            if chunk is None:
                # Upstream has gone quiet. The heartbeat goes to the connection only: the
                # recorded body, the usage it is counted from and the bytes a coalesced
                # retry is replayed are the provider's, untouched.
                if downstream_open:
                    try:
                        await response.write(_SSE_KEEPALIVE)
                    except ConnectionResetError:
                        downstream_open = False
                continue
            chunks.append(chunk)
            if downstream_open:
                try:
                    await response.write(chunk)
                except ConnectionResetError:
                    downstream_open = False
        if downstream_open:
            with contextlib.suppress(ConnectionResetError):
                await response.write_eof()

        # Measured to the end of the stream: a streamed call's cost in wall-clock time is
        # how long the agent waited for all of it, not how quickly the first byte arrived.
        await self._account(
            session,
            {},
            payload,
            call_id=call_id,
            reservation=reservation,
            streamed=chunks,
            status=upstream.status,
            latency_ms=_elapsed_ms(started),
            response_body=b"".join(chunks),
            provider_response_id=_stream_response_id(chunks),
            path=path,
        )
        self._retain_payload(session, call_id, None, b"".join(chunks), streaming=True)
        session.finish(
            key,
            {
                "status": upstream.status,
                "body": b"".join(chunks),
                "streaming": True,
                "call_id": call_id,
            },
            replayable=_replayable(upstream.status)
            and self._dialect(session).stream_completed(chunks),
        )
        return response

    def _replayed(self, cached: dict[str, Any]) -> web.Response:
        content_type = "text/event-stream" if cached["streaming"] else "application/json"
        return web.Response(
            body=cached["body"],
            status=cached["status"],
            content_type=content_type,
            headers=cached.get("headers"),
        )

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
        if self._dialect_name(session) == "gemini":
            # The model is imposed in the path; a body field could only contradict it.
            return {name: value for name, value in payload.items() if name != "model"}
        authoritative = {**payload, "model": session.model}
        if self._dialect_name(session) == "openai-chat-completions" and authoritative.get("stream"):
            authoritative["stream_options"] = {
                **(authoritative.get("stream_options") or {}),
                "include_usage": True,
            }
        return authoritative

    async def _account(
        self,
        session: GatewaySession,
        parsed: dict[str, Any],
        payload: dict[str, Any],
        *,
        call_id: str,
        reservation: GatewayReservation,
        streamed: list[bytes] | None,
        status: int,
        latency_ms: int = 0,
        response_body: bytes = b"",
        provider_response_id: str | None = None,
        path: str | None = None,
    ) -> None:
        request_digest = _request_digest(payload, path)
        if status != 200:
            # A failed call is still a call. Recording it costs one line and is the
            # difference between "the agent stalled" and "the provider was returning
            # 529s for eleven minutes" — the same evidence, opposite conclusions.
            self._record(
                session,
                call_id=call_id,
                request_digest=request_digest,
                disposition="failed",
                upstream_status=status,
                latency_ms=latency_ms,
                response_digest=GatewaySession.digest(response_body) if response_body else None,
                provider_response_id=provider_response_id,
            )
            await session.commit(reservation)
            return
        self._retain_exact_tokens(session, call_id, payload, parsed)
        if streamed is not None:
            usage, stop_reason = self._dialect(session).usage_from_stream(streamed)
        else:
            usage, stop_reason = self._dialect(session).extract_usage(parsed)
        self._record(
            session,
            call_id=call_id,
            request_digest=request_digest,
            disposition="forwarded",
            response_digest=GatewaySession.digest(response_body) if response_body else None,
            provider_response_id=provider_response_id,
            usage=usage,
            stop_reason=stop_reason,
            latency_ms=latency_ms,
        )
        await session.commit(reservation, usage)

    async def _post(  # type: ignore[no-untyped-def]
        self,
        request: web.Request,
        payload: dict[str, Any],
        session: GatewaySession,
        path: str | None = None,
    ):
        assert self._client is not None
        if session.upstream is not None:
            return await self._client.post(
                session.upstream.endpoint,
                json=payload,
                headers=self._own_headers(request, session.upstream),
            )
        for attempt in range(2):
            upstream = await self._client.post(
                f"{self.upstream}{path or self.upstream_path or REQUEST_PATHS[self.dialect]}",
                json=payload,
                headers=await self._upstream_headers(request),
            )
            if attempt or self.subscription is None:
                return upstream
            delay = 0.0
            retry = upstream.status == 401 and await self.subscription.refresh()
            if upstream.status == 429:
                try:
                    delay = max(0.0, min(float(upstream.headers.get("retry-after", "1")), 5.0))
                except ValueError:
                    delay = 1.0
                retry = True
            if not retry:
                return upstream
            upstream.release()
            if delay:
                await asyncio.sleep(delay)
        raise AssertionError("subscription retry loop did not return")

    async def _upstream_headers(self, request: web.Request) -> dict[str, str]:
        headers = {
            name: value for name, value in request.headers.items() if name.lower() not in _STRIP
        }
        if self.subscription is not None:
            headers.update(await self.subscription.headers())
        elif self.dialect == "gemini":
            headers["x-goog-api-key"] = self.api_key
        elif self.dialect == "anthropic" and not self.bearer_auth:
            headers["x-api-key"] = self.api_key
            headers.setdefault("anthropic-version", "2023-06-01")
        else:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def _own_headers(self, request: web.Request, upstream: Upstream) -> dict[str, str]:
        headers = {
            name: value for name, value in request.headers.items() if name.lower() not in _STRIP
        }
        if upstream.dialect == "anthropic":
            headers["x-api-key"] = upstream.api_key
            headers.setdefault("anthropic-version", "2023-06-01")
        else:
            headers["authorization"] = f"Bearer {upstream.api_key}"
        return headers

    def _dialect_name(self, session: GatewaySession) -> str:
        return session.upstream.dialect if session.upstream is not None else self.dialect

    def _dialect(self, session: GatewaySession):  # type: ignore[no-untyped-def]
        return _DIALECTS[self._dialect_name(session)]

    def _normalized_output_limit(
        self, payload: dict[str, Any], session: GatewaySession
    ) -> dict[str, Any]:
        """Forward the client's own output ceiling under the dialect's current field.

        An absent ceiling stays absent: limits refuse the next call rather than truncate
        this one, so inventing a value here would silently cut reasoning and output.
        """
        limited = dict(payload)
        dialect = self._dialect_name(session)
        if (
            session.upstream is None
            and self.subscription is not None
            and not self.subscription.supports_output_limit
        ):
            # The ChatGPT Codex backend rejects the public Responses API field.
            limited.pop("max_output_tokens", None)
            return limited
        if dialect == "openai-chat-completions" and "max_tokens" in limited:
            legacy = limited.pop("max_tokens")
            limited.setdefault(_MAX_OUTPUT_FIELDS[dialect], legacy)
        return limited

    def _record(
        self,
        session: GatewaySession,
        *,
        call_id: str,
        request_digest: str,
        disposition: str | None = None,
        response_digest: str | None = None,
        provider_response_id: str | None = None,
        usage: CallUsage | None = None,
        stop_reason: str | None = None,
        refused: str | None = None,
        upstream_status: int | None = None,
        latency_ms: int = 0,
    ) -> None:
        trace = self._traces.get(session.token)
        if trace is None:
            return
        trace.append(
            TransportCall(
                episode_id=session.episode_id,
                call_id=call_id,
                model=session.model,
                request_digest=f"sha256:{request_digest}",
                response_digest=(f"sha256:{response_digest}" if response_digest else None),
                provider_response_id=provider_response_id,
                input_tokens=usage.input_tokens if usage else 0,
                cache_read_tokens=usage.cache_read_tokens if usage else 0,
                cache_write_tokens=usage.cache_write_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                stop_reason=stop_reason,
                disposition=disposition or ("refused" if refused is not None else "forwarded"),  # type: ignore[arg-type]
                refusal_limit=refused,
                upstream_status=upstream_status,
                latency_ms=latency_ms,
                purpose=session.purpose,
            )
        )

    def _record_replay(
        self,
        session: GatewaySession,
        *,
        call_id: str,
        request_digest: str,
        reason: str,
    ) -> None:
        trace = self._traces.get(session.token)
        if trace is None:
            return
        trace.append(
            TransportReplay(
                episode_id=session.episode_id,
                call_id=call_id,
                request_digest=request_digest,
                reason=reason,  # type: ignore[arg-type]
            )
        )

    def _retain_payload(
        self,
        session: GatewaySession,
        call_id: str,
        request: dict[str, Any] | None,
        response: bytes | None,
        *,
        streaming: bool = False,
    ) -> None:
        root = session.payload_dir
        if root is None:
            return
        root.mkdir(parents=True, exist_ok=True)
        if request is not None:
            _atomic_write(
                root / f"{call_id}.request.json",
                json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n",
            )
        if response is not None:
            suffix = "sse" if streaming else "json"
            _atomic_write(root / f"{call_id}.response.{suffix}", response)

    def _retain_exact_tokens(
        self,
        session: GatewaySession,
        call_id: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        root = session.exact_token_dir
        evidence = response.get("ale_token_data")
        if root is None or not isinstance(evidence, dict):
            return
        retained = dict(evidence)
        if "temperature" not in retained and isinstance(request.get("temperature"), int | float):
            retained["temperature"] = request["temperature"]
        root.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            root / f"{call_id}.json",
            json.dumps(retained, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            + b"\n",
        )


async def _iter_chunks(
    upstream: Any, *, keepalive_sec: float | None = None
) -> AsyncIterator[bytes | None]:
    """Yield upstream bytes as they arrive; ``None`` marks ``keepalive_sec`` of silence.

    The upstream iterator is driven by its own task and never cancelled mid-read: a wait
    cut short by the keep-alive timer only interrupts ``Queue.get``, which asyncio
    guarantees loses nothing.
    """
    if keepalive_sec is None:
        async for chunk in upstream.content.iter_any():
            yield chunk
        return

    queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for chunk in upstream.content.iter_any():
                await queue.put(chunk)
        except BaseException as exc:  # handed to the consumer, which re-raises it
            await queue.put(exc)
        else:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), keepalive_sec)
            except TimeoutError:
                yield None
                continue
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


def _replayable(status: int) -> bool:
    """Only a successful provider response is replayed to a retry of the same request.

    Replaying an error (a 5xx, or a 429 from the provider) would answer every retry the
    client makes with the first failure and never reach the provider again.
    """
    return 200 <= status < 300


def _elapsed_ms(started: float) -> int:
    """Milliseconds since ``started``, on the loop's own clock."""
    return int((asyncio.get_running_loop().time() - started) * 1000)


def _request_digest(payload: dict[str, Any], path: str | None = None) -> str:
    """Digest of the authoritative request: its body, plus its path when that varies.

    A Gemini path names the method, so the same body sent to ``generateContent`` and to
    ``streamGenerateContent`` is two requests, and neither may be replayed as the other.
    """
    body = json.dumps(payload, sort_keys=True).encode()
    return GatewaySession.digest(body if path is None else path.encode() + b"\n" + body)


def _response_id(event: dict[str, Any]) -> str | None:
    """The provider's ID in a response body or stream event (Gemini's is ``responseId``)."""
    for scope in (event.get("message"), event.get("response"), event):
        if not isinstance(scope, dict):
            continue
        for name in ("id", "responseId"):
            if isinstance(scope.get(name), str):
                return scope[name]
    return None


def _stream_response_id(chunks: list[bytes]) -> str | None:
    for line in b"".join(chunks).decode("utf-8", "ignore").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line.removeprefix("data:").strip())
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and (found := _response_id(event)) is not None:
            return found
    return None


def _google_error(code: int, status: str, message: str, reason: str) -> web.Response:
    return web.json_response(gemini.error_body(code, status, message, reason), status=code)


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
