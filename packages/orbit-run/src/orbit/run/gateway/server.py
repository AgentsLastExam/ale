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
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout, web

from orbit.core.episode import EventSink
from orbit.core.errors import ConfigError
from orbit.core.trace import TransportCall, TransportReplay, UpstreamError
from orbit.run.gateway import anthropic, gemini, openai_chat_completions, openai_responses
from orbit.run.gateway.session import (
    CallUsage,
    GatewayReservation,
    GatewaySession,
    LimitReached,
    SessionRegistry,
    Upstream,
    UpstreamFailure,
)
from orbit.run.recording import Redactor
from orbit.run.subscription import SubscriptionCredential

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

#: How long the Gateway sends a call again while the model provider fails an agent
#: session's calls, overloaded, limiting their rate, failing or not reached, counted from the
#: first call it failed until it answers one. A minute absorbs a burst of rate limits or an
#: overloaded moment, which some CLIs do not retry at all; a longer failure reaches the
#: client as the provider sent it, so that its program gives up soon rather than waiting out
#: an outage.
OUTAGE_RETRY_SEC = 60.0
#: The waits between the Gateway's tries of one call, where the provider names none.
_RETRY_WAITS = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)

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
        outage_retry_sec: float = OUTAGE_RETRY_SEC,
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
        self.outage_retry_sec = outage_retry_sec
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
                text=json.dumps({"type": "error", "error": {"type": "orbit_wrong_dialect"}}),
                content_type="application/json",
            )
        started = asyncio.get_running_loop().time()
        body = await request.read()
        payload = self._authoritative(json.loads(body or b"{}"), session)
        if (refused := self._refused_web_tools(session, payload)) is not None:
            return refused
        return await self._call(
            request, session, payload, started=started, streaming=bool(payload.get("stream"))
        )

    async def _gemini(self, request: web.Request) -> web.StreamResponse:
        """Serve a Gemini API request, refusing what the Gateway cannot account or allow."""
        started = asyncio.get_running_loop().time()
        token = request.headers.get("x-goog-api-key") or request.query.get("key", "")
        session = self.sessions.resolve(token)
        if session is None and self._refuse_suspended(token, request):
            return _google_error(
                403,
                "PERMISSION_DENIED",
                "this Orbit session is suspended",
                "ORBIT_SESSION_SUSPENDED",
            )
        if session is None:
            return _google_error(
                401, "UNAUTHENTICATED", "unknown Orbit episode token", "ORBIT_UNKNOWN_SESSION"
            )
        if self._dialect_name(session) != "gemini":
            return _google_error(
                404,
                "NOT_FOUND",
                f"this episode's Gateway dialect is {self._dialect_name(session)}, not gemini",
                "ORBIT_WRONG_DIALECT",
            )
        try:
            payload = json.loads(await request.read() or b"{}")
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            return _google_error(
                400, "INVALID_ARGUMENT", "request body is not a JSON object", "ORBIT_INVALID_BODY"
            )
        payload = self._authoritative(payload, session)

        target = request.match_info["target"]
        refusal = gemini.policy_refusal(request.method, target, request.query.get("alt"))
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
        if (refused := self._refused_web_tools(session, payload, path)) is not None:
            return refused
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
            upstream, failure = await self._post(request, payload, session, path)
            try:
                if not 200 <= upstream.status < 300:
                    # The provider's failure, or its refusal of the request, streamed or not,
                    # as it sent it. A failed call is still recorded: that is the difference
                    # between "the agent stalled" and "the provider was returning 529s for
                    # eleven minutes". It is never replayed: the client's retry of the same
                    # request is forwarded. A refusal counts as a call; a failure does not.
                    raw = await upstream.read()
                    self._retain_payload(session, call_id, None, raw)
                    self._record(
                        session,
                        call_id=call_id,
                        request_digest=_request_digest(payload, path),
                        disposition="failed",
                        upstream_status=upstream.status,
                        upstream_error=failure,
                        latency_ms=_elapsed_ms(started),
                        response_digest=GatewaySession.digest(raw) if raw else None,
                        provider_response_id=_response_id(_json_object(raw)),
                    )
                    await session.commit(reservation, answered=failure is None)
                    committed = True
                    session.finish(
                        key,
                        {
                            "status": upstream.status,
                            "body": raw,
                            "streaming": False,
                            "call_id": call_id,
                        },
                        replayable=False,
                    )
                    return web.Response(
                        body=raw,
                        status=upstream.status,
                        headers={
                            "content-type": upstream.headers.get("content-type", "application/json")
                        },
                    )
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
                parsed = _json_object(raw)
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
                session.finish(key, cached)
                return web.Response(
                    body=raw,
                    status=upstream.status,
                    content_type="application/json",
                )
            finally:
                upstream.release()
        except BaseException as error:
            if not committed:
                # Not reached, or cut off in its stream: the provider's failure, which the
                # session records; any other is the client's, such as a cancellation.
                unreached = isinstance(error, (ClientError, TimeoutError))
                if unreached:
                    session.upstream_failed("unreachable", None, asyncio.get_running_loop().time())
                self._record(
                    session,
                    call_id=call_id,
                    request_digest=_request_digest(payload, path),
                    disposition="failed",
                    upstream_error="unreachable" if unreached else None,
                    latency_ms=_elapsed_ms(started),
                )
                await session.commit(reservation, answered=False)
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
        chunks: list[bytes] = []
        downstream_open = True
        try:
            await response.prepare(request)
        except ConnectionResetError:
            # The client went away, not the provider: its answer is still read and counted.
            downstream_open = False
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
            replayable=self._dialect(session).stream_completed(chunks),
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

    def _refused_web_tools(
        self, session: GatewaySession, payload: dict[str, Any], path: str | None = None
    ) -> web.Response | None:
        """Refuse a request for provider-side network tools unless the Task's network is open.

        The tools reach the network from the provider's side, outside the Sandbox's network
        policy. The request is refused rather than forwarded without them: the Harness
        turns them off, so one only arrives when a CLI changed its defaults or an agent
        wrote it, and either should show in the trace rather than be quietly rewritten.
        """
        if session.provider_web_tools:
            return None
        named = self._dialect(session).web_tools(payload)
        if not named:
            return None
        self._record(
            session,
            call_id=f"call_{uuid.uuid4().hex}",
            request_digest=_request_digest(payload, path),
            refused="web_tools",
        )
        message = (
            "provider-side network tools need a Task with open network; the Orbit Gateway "
            f"refuses {', '.join(named)}"
        )
        return web.json_response(
            self._dialect(session).policy_body("web_tools", message),
            status=400,
            # A policy does not lift by waiting; SDKs that honor this stop retrying.
            headers={"x-should-retry": "false"},
        )

    def _authenticate(self, request: web.Request) -> GatewaySession:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() or request.headers.get("x-api-key", "")
        session = self.sessions.resolve(token)
        if session is None and self._refuse_suspended(token, request):
            raise web.HTTPForbidden(
                text=json.dumps({"type": "error", "error": {"type": "orbit_session_suspended"}}),
                content_type="application/json",
            )
        if session is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"type": "error", "error": {"type": "orbit_unknown_session"}}),
                content_type="application/json",
            )
        return session

    def _refuse_suspended(self, token: str, request: web.Request) -> bool:
        """Whether ``token`` is a known session's, suspended for now, whose refused call its
        trace then records as refused, for ``session_suspended``."""
        session = self.sessions.known(token)
        if session is None:
            return False
        self._record(
            session,
            call_id=f"call_{uuid.uuid4().hex}",
            request_digest=_request_digest({}, f"{request.method} {request.path}"),
            refused="session_suspended",
        )
        return True

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
        """Record and count a call the provider took: answered, or failed inside the stream
        it began, whose tokens count but whose call does not."""
        self._retain_exact_tokens(session, call_id, payload, parsed)
        dialect = self._dialect(session)
        failure: UpstreamError | None = None
        if streamed is not None:
            usage, stop_reason = dialect.usage_from_stream(streamed)
            if not dialect.stream_completed(streamed):
                failure = self._stream_failure(session, streamed)
        else:
            usage, stop_reason = dialect.extract_usage(parsed)
        if failure is None:
            session.upstream_answered()
        else:
            reached = None if failure == "unreachable" else status
            session.upstream_failed(failure, reached, asyncio.get_running_loop().time())
        self._record(
            session,
            call_id=call_id,
            request_digest=_request_digest(payload, path),
            disposition="forwarded" if failure is None else "failed",
            response_digest=GatewaySession.digest(response_body) if response_body else None,
            provider_response_id=provider_response_id,
            usage=usage,
            stop_reason=stop_reason,
            upstream_status=None if failure is None else status,
            upstream_error=failure,
            latency_ms=latency_ms,
        )
        await session.commit(reservation, usage, answered=failure is None)

    def _stream_failure(self, session: GatewaySession, chunks: list[bytes]) -> UpstreamError:
        """How the provider failed a stream it began: by the error it sent in it, or, cut off
        before its end, as not reached."""
        dialect = self._dialect(session)
        error = dialect.stream_error(chunks)
        if error is None:
            return "unreachable"
        kind = _failure_kind(200, error, dialect.quota(error, {}))
        assert kind is not None, "a provider's error inside a 2xx stream is its failure"
        return kind

    async def _post(
        self,
        request: web.Request,
        payload: dict[str, Any],
        session: GatewaySession,
        path: str | None = None,
    ) -> tuple[ClientResponse, UpstreamError | None]:
        """Send one call upstream, and again while the model provider fails the session's
        calls, for up to ``outage_retry_sec`` from its first failure and before anything
        reached the client. Return the provider's answer, or its last failure, read, and how
        it failed; raise where it was not reached."""
        loop = asyncio.get_running_loop()
        refreshed = False
        tries = 0
        while True:
            started = loop.time()
            try:
                upstream = await self._send(request, payload, session, path)
            except (ClientError, TimeoutError):
                failed = session.upstream_failed("unreachable", None, loop.time())
                wait = self._retry_wait(session, request, failed, {}, tries)
                if wait is None:
                    raise
            else:
                if 200 <= upstream.status < 300:
                    return upstream, None
                if (
                    upstream.status == 401
                    and not refreshed
                    and session.upstream is None
                    and self.subscription is not None
                ):
                    # The Gateway's own credential, which only it can refresh.
                    refreshed = True
                    if await self.subscription.refresh():
                        upstream.release()
                        continue
                error = _json_object(await upstream.read())
                kind = _failure_kind(
                    upstream.status, error, self._dialect(session).quota(error, upstream.headers)
                )
                if kind is None:
                    # The request's own failure, such as an invalid one: the provider answered.
                    session.upstream_answered()
                    return upstream, None
                failed = session.upstream_failed(kind, upstream.status, loop.time())
                wait = self._retry_wait(session, request, failed, upstream.headers, tries)
                if wait is None:
                    return upstream, kind
                upstream.release()
            self._record(
                session,
                call_id=f"call_{uuid.uuid4().hex}",
                request_digest=_request_digest(payload, path),
                disposition="failed",
                upstream_status=failed.status,
                upstream_error=failed.kind,
                latency_ms=_elapsed_ms(started),
            )
            tries += 1
            await asyncio.sleep(wait)

    async def _send(
        self,
        request: web.Request,
        payload: dict[str, Any],
        session: GatewaySession,
        path: str | None,
    ) -> ClientResponse:
        assert self._client is not None
        if session.upstream is not None:
            return await self._client.post(
                session.upstream.endpoint,
                json=payload,
                headers=self._own_headers(request, session.upstream),
            )
        return await self._client.post(
            f"{self.upstream}{path or self.upstream_path or REQUEST_PATHS[self.dialect]}",
            json=payload,
            headers=await self._upstream_headers(request),
        )

    def _retry_wait(
        self,
        session: GatewaySession,
        request: web.Request,
        failure: UpstreamFailure,
        headers: Mapping[str, str],
        tries: int,
    ) -> float | None:
        """How long to wait before sending a call again, ``tries`` sends after its first:
        the next backoff, or longer where the provider names longer, cut to what is left of
        the failure's ``outage_retry_sec``. ``None`` gives the failure to the client: a
        quota, which does not lift by waiting; a wait the provider names past that window;
        the window spent; a Judge's call, which its Judge tries again itself, under its own
        deadline; and a call nobody waits for any more, its client gone or its turn over."""
        left = failure.since + self.outage_retry_sec - asyncio.get_running_loop().time()
        named = _retry_after(headers)
        if (
            failure.kind == "quota"
            or left <= 0
            or (named is not None and named > left)
            or session.purpose == "judge"
            or session.suspended
            or request.transport is None
            or request.transport.is_closing()
        ):
            return None
        return min(max(_RETRY_WAITS[min(tries, len(_RETRY_WAITS) - 1)], named or 0.0), left)

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
        upstream_error: UpstreamError | None = None,
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
                upstream_error=upstream_error,
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
                _redacted(
                    session,
                    json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2).encode()
                    + b"\n",
                ),
            )
        if response is not None:
            suffix = "sse" if streaming else "json"
            _atomic_write(root / f"{call_id}.response.{suffix}", _redacted(session, response))

    def _retain_exact_tokens(
        self,
        session: GatewaySession,
        call_id: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        root = session.exact_token_dir
        evidence = response.get("orbit_token_data")
        if root is None or not isinstance(evidence, dict):
            return
        retained = dict(evidence)
        if "temperature" not in retained and isinstance(request.get("temperature"), int | float):
            retained["temperature"] = request["temperature"]
        root.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            root / f"{call_id}.json",
            _redacted(
                session,
                json.dumps(
                    retained, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
                + b"\n",
            ),
        )


def _redacted(session: GatewaySession, data: bytes) -> bytes:
    """``data`` without the Task's secrets; undecodable bytes pass through unchanged."""
    if not session.redacted:
        return data
    text = data.decode("utf-8", errors="surrogateescape")
    redactor = Redactor(session.redacted, patterns=False)
    return redactor(text).encode("utf-8", errors="surrogateescape")


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


def _failure_kind(status: int, error: dict[str, Any], quota: bool) -> UpstreamError | None:
    """How the provider failed a call that got ``status``, a 2xx one in its stream, with
    ``error``; ``None`` for a request it refused as the client's fault, such as an invalid one.
    """
    if quota:
        return "quota"
    if 300 <= status < 500 and status != 429:
        return None
    text = json.dumps(error).lower()
    if status == 529 or "overload" in text or "capacity" in text:
        return "overloaded"
    if status == 429 or "rate_limit" in text:
        return "rate_limited"
    return "server_error"


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """The wait a failure names in ``retry-after-ms`` or ``retry-after`` (seconds), if any;
    a date is left to the backoff."""
    for name, seconds in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            return max(float(value) * seconds, 0.0)
        except ValueError:
            continue
    return None


def _json_object(raw: bytes) -> dict[str, Any]:
    """A body's JSON object, or an empty one for anything else, such as a proxy's HTML page."""
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


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
