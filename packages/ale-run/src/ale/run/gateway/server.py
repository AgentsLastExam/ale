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
from ale.run.gateway import anthropic, openai_chat_completions, openai_responses
from ale.run.gateway.session import (
    GatewayReservation,
    GatewaySession,
    LimitReached,
    SessionRegistry,
)

__all__ = ["Gateway"]

UPSTREAM_DEFAULT = "https://api.anthropic.com"
_REQUEST_TIMEOUT = ClientTimeout(total=1800)

#: Hop-by-hop and auth headers never forwarded upstream: the gateway supplies its own.
_STRIP = frozenset(
    {"host", "content-length", "authorization", "x-api-key", "connection", "accept-encoding"}
)
_DIALECTS = {
    "anthropic": anthropic,
    "openai-chat-completions": openai_chat_completions,
    "openai-responses": openai_responses,
}
_REQUEST_PATHS = {
    "anthropic": "/v1/messages",
    "openai-chat-completions": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
}
_COUNT_PATHS = {
    "anthropic": "/v1/messages/count_tokens",
    "openai-responses": "/v1/responses/input_tokens",
}
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
        api_key: str,
        upstream: str = UPSTREAM_DEFAULT,
        dialect: str = "anthropic",
        host: str = "0.0.0.0",
        port: int = 0,
    ) -> None:
        self.api_key = api_key
        self.upstream = upstream.rstrip("/")
        if dialect not in _DIALECTS:
            raise ConfigError(f"unsupported gateway dialect {dialect!r}")
        self.dialect = dialect
        self.host = host
        self.port = port
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
        if self.dialect == "openai-chat-completions" and any(
            value is not None
            for value in (
                session.limits.max_input_tokens,
                session.limits.max_total_tokens,
                session.limits.max_cost_usd,
            )
        ):
            raise ConfigError(
                "openai-chat-completions has no provider-independent exact input-token "
                "endpoint; finite input, total-token, and cost limits are unsupported"
            )
        if (
            session.limits.max_cost_usd is not None
            and self._dialect.pricing_for(session.model) is None
        ):
            raise ConfigError(
                f"finite max_cost_usd requires known pricing for model {session.model!r}"
            )
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
        app.router.add_post(self._request_path, self._messages)
        return app

    async def _healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "dialects": [self.dialect]})

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        session = self._authenticate(request)
        started = asyncio.get_running_loop().time()
        body = await request.read()
        payload = self._authoritative(json.loads(body or b"{}"), session)
        key = GatewaySession.digest(json.dumps(payload, sort_keys=True).encode())
        request_digest = f"sha256:{key}"

        if (cached := session.cached(key)) is not None:
            self._record_replay(
                session,
                call_id=cached["call_id"],
                request_digest=request_digest,
                reason="completed-cache",
            )
            return self._replayed(cached)  # a retry must not sample twice
        if (pending := session.inflight(key)) is not None:
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
            response = await self._forward(request, session, payload, key, call_id, started)
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
    ) -> web.StreamResponse:
        assert self._client is not None
        headers = self._upstream_headers(request)
        input_tokens = (
            await self._count_input_tokens(payload, headers) if _needs_exact_input(session) else 0
        )
        requested_output = self._requested_output(payload)
        try:
            reservation = await session.reserve(
                input_tokens=input_tokens,
                requested_output_tokens=requested_output,
                pricing=self._dialect.pricing_for(session.model),
            )
        except LimitReached as limit:
            self._record(
                session,
                call_id=call_id,
                request_digest=GatewaySession.digest(json.dumps(payload, sort_keys=True).encode()),
                refused=limit.limit,
            )
            raw = json.dumps(
                self._dialect.refusal_body(limit.limit, limit.value, limit.observed)
            ).encode()
            self._retain_payload(session, call_id, payload, raw)
            cached = {
                "status": 429,
                "body": raw,
                "streaming": False,
                "call_id": call_id,
            }
            session.finish(key, cached)
            return self._replayed(cached)
        payload = self._with_output_limit(payload, reservation.output_tokens)
        self._retain_payload(session, call_id, payload, None)

        streaming = bool(payload.get("stream"))
        committed = False
        try:
            async with self._client.post(
                f"{self.upstream}{self._request_path}", json=payload, headers=headers
            ) as upstream:
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
                    provider_response_id=parsed.get("id")
                    if isinstance(parsed.get("id"), str)
                    else None,
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
        except BaseException:
            if not committed:
                self._record(
                    session,
                    call_id=call_id,
                    request_digest=GatewaySession.digest(
                        json.dumps(payload, sort_keys=True).encode()
                    ),
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
    ) -> web.StreamResponse:
        """Pass the event stream straight through, counting usage as it goes."""
        response = web.StreamResponse(
            status=upstream.status, headers={"content-type": "text/event-stream"}
        )
        await response.prepare(request)

        chunks: list[bytes] = []
        downstream_open = True
        async for chunk in _iter_chunks(upstream):
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
        authoritative = {**payload, "model": session.model}
        if self.dialect == "openai-chat-completions" and authoritative.get("stream"):
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
    ) -> None:
        request_digest = GatewaySession.digest(json.dumps(payload, sort_keys=True).encode())
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
            input_tokens, output_tokens, stop_reason = self._dialect.usage_from_stream(streamed)
        else:
            input_tokens, output_tokens, stop_reason = self._dialect.extract_usage(parsed)
        cost = self._dialect.estimate_cost(session.model, input_tokens, output_tokens)
        self._record(
            session,
            call_id=call_id,
            request_digest=request_digest,
            disposition="forwarded",
            response_digest=GatewaySession.digest(response_body) if response_body else None,
            provider_response_id=provider_response_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            stop_reason=stop_reason,
            latency_ms=latency_ms,
        )
        await session.commit(
            reservation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )

    def _upstream_headers(self, request: web.Request) -> dict[str, str]:
        headers = {
            name: value for name, value in request.headers.items() if name.lower() not in _STRIP
        }
        if self.dialect == "anthropic":
            headers["x-api-key"] = self.api_key
            headers.setdefault("anthropic-version", "2023-06-01")
        else:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _count_input_tokens(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> int:
        assert self._client is not None
        count_payload = {
            key: value
            for key, value in payload.items()
            if key not in {self._max_output_field, "stream"}
        }
        async with self._client.post(
            f"{self.upstream}{self._count_path}",
            json=count_payload,
            headers=headers,
        ) as response:
            raw = await response.read()
            if response.status != 200:
                raise web.HTTPBadGateway(
                    text=json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "ale_token_count_failed",
                                "upstream_status": response.status,
                            },
                        }
                    ),
                    content_type="application/json",
                )
            try:
                return int(json.loads(raw or b"{}")["input_tokens"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise web.HTTPBadGateway(
                    text=json.dumps(
                        {
                            "type": "error",
                            "error": {"type": "ale_token_count_invalid"},
                        }
                    ),
                    content_type="application/json",
                ) from exc

    @property
    def _dialect(self):  # type: ignore[no-untyped-def]
        return _DIALECTS[self.dialect]

    @property
    def _request_path(self) -> str:
        return _REQUEST_PATHS[self.dialect]

    @property
    def _count_path(self) -> str:
        try:
            return _COUNT_PATHS[self.dialect]
        except KeyError as exc:
            raise ConfigError(f"{self.dialect} does not support exact input-token counts") from exc

    @property
    def _max_output_field(self) -> str:
        return _MAX_OUTPUT_FIELDS[self.dialect]

    def _requested_output(self, payload: dict[str, Any]) -> int:
        value = payload.get(self._max_output_field)
        if value is None and self.dialect == "openai-chat-completions":
            value = payload.get("max_tokens")
        return int(value or 4096)

    def _with_output_limit(
        self,
        payload: dict[str, Any],
        output_tokens: int,
    ) -> dict[str, Any]:
        limited = dict(payload)
        if self.dialect == "openai-chat-completions":
            limited.pop("max_tokens", None)
        limited[self._max_output_field] = output_tokens
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
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
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
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                stop_reason=stop_reason,
                disposition=disposition or ("refused" if refused is not None else "forwarded"),  # type: ignore[arg-type]
                refusal_limit=refused,
                upstream_status=upstream_status,
                latency_ms=latency_ms,
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


async def _iter_chunks(upstream: Any) -> AsyncIterator[bytes]:
    async for chunk in upstream.content.iter_any():
        yield chunk


def _needs_exact_input(session: GatewaySession) -> bool:
    limits = session.limits
    return any(
        value is not None
        for value in (
            limits.max_input_tokens,
            limits.max_total_tokens,
            limits.max_cost_usd,
        )
    )


def _elapsed_ms(started: float) -> int:
    """Milliseconds since ``started``, on the loop's own clock."""
    return int((asyncio.get_running_loop().time() - started) * 1000)


def _stream_response_id(chunks: list[bytes]) -> str | None:
    for line in b"".join(chunks).decode("utf-8", "ignore").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line.removeprefix("data:").strip())
        except json.JSONDecodeError:
            continue
        message = event.get("message")
        if isinstance(message, dict) and isinstance(message.get("id"), str):
            return message["id"]
        response = event.get("response")
        if isinstance(response, dict) and isinstance(response.get("id"), str):
            return response["id"]
        if isinstance(event.get("id"), str):
            return event["id"]
    return None


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
