"""RESTful HTTP server for receiving Task bundles from upstream Brewers."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aiohttp import web
from pydantic import BaseModel, ValidationError

from ale.core.errors import ConfigError
from ale.core.intake import (
    HEADER_DISPATCH_ID,
    HEADER_IDEMPOTENCY_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    INTAKE_PROTOCOL_VERSION,
    IntakeDispatchRequest,
    verify_dispatch_signature,
)
from ale.run import __version__
from ale.run.intake.service import (
    IntakeConflictError,
    IntakeNotFoundError,
    IntakeServerSettings,
    IntakeService,
    IntakeValidationError,
)

__all__ = ["intake_app", "serve_intake"]

_MAX_METADATA_BYTES = 4 * 1024 * 1024
_SERVICE_KEY = web.AppKey("intake_service", IntakeService)


def _json_response(payload: BaseModel | dict[str, Any], status: int = 200) -> web.Response:
    text = (
        payload.model_dump_json()
        if isinstance(payload, BaseModel)
        else json.dumps(payload, ensure_ascii=False)
    )
    return web.Response(text=text, status=status, content_type="application/json")


def _error_response(status: int, detail: str) -> web.Response:
    return _json_response({"detail": detail}, status=status)


def _bearer_credential(request: web.Request) -> str:
    header = (request.headers.get("Authorization") or "").strip()
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _secret_match(provided: str, expected: str) -> bool:
    return bool(provided and expected) and hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    )


class _IntakeApi:
    def __init__(self, service: IntakeService) -> None:
        self.service = service
        self.settings = service.settings

    def _authorize_bearer_query(self, request: web.Request) -> None:
        provided = _bearer_credential(request)
        token_ok = bool(self.settings.token) and _secret_match(provided, self.settings.token)
        secret_ok = bool(self.settings.secret) and _secret_match(provided, self.settings.secret)
        if not (token_ok or secret_ok):
            raise web.HTTPUnauthorized(
                text=json.dumps({"detail": "valid Bearer token is required"}),
                content_type="application/json",
            )

    def _check_submit_bearer(self, request: web.Request) -> None:
        if not self.settings.token:
            return
        provided = _bearer_credential(request)
        if not _secret_match(provided, self.settings.token):
            raise web.HTTPUnauthorized(
                text=json.dumps({"detail": "missing or invalid Bearer token"}),
                content_type="application/json",
            )

    def _check_submit_hmac(
        self, request: web.Request, metadata_bytes: bytes, payload_idem_key: str | None
    ) -> None:
        if not self.settings.secret:
            return
        idem_header = (request.headers.get(HEADER_IDEMPOTENCY_KEY) or "").strip()
        effective_idem = idem_header or (payload_idem_key or "")
        ok, reason = verify_dispatch_signature(
            secret=self.settings.secret,
            signature_header=request.headers.get(HEADER_SIGNATURE, ""),
            timestamp_header=request.headers.get(HEADER_TIMESTAMP, ""),
            idempotency_key=effective_idem,
            metadata_bytes=metadata_bytes,
            max_skew_sec=self.settings.max_skew_sec,
        )
        if not ok:
            raise web.HTTPUnauthorized(
                text=json.dumps({"detail": reason}),
                content_type="application/json",
            )

    async def health(self, _request: web.Request) -> web.Response:
        return _json_response(
            {
                "status": "ok",
                "service": "ale-intake",
                "protocol": INTAKE_PROTOCOL_VERSION,
                "version": __version__,
            }
        )

    async def submit(self, request: web.Request) -> web.Response:
        self._check_submit_bearer(request)

        bundle_bytes: bytes | None = None
        bundle_filename: str | None = None
        content_type = (request.content_type or "").lower()

        if content_type.startswith("multipart/"):
            reader = await request.multipart()
            metadata_bytes: bytes | None = None
            while (part := await reader.next()) is not None:
                if part.name == "metadata":
                    chunks = bytearray()
                    while chunk := await part.read_chunk(65_536):
                        chunks.extend(chunk)
                        if len(chunks) > _MAX_METADATA_BYTES:
                            return _error_response(422, "metadata part exceeds 4 MiB limit")
                    metadata_bytes = bytes(chunks)
                elif part.name == "bundle":
                    bundle_filename = Path(part.filename).name if part.filename else None
                    buf = bytearray()
                    while chunk := await part.read_chunk(65_536):
                        buf.extend(chunk)
                        if len(buf) > self.settings.max_bundle_bytes:
                            return _error_response(413, "uploaded bundle exceeds size limit")
                    bundle_bytes = bytes(buf)
                else:
                    await part.release()
            if metadata_bytes is None:
                return _error_response(422, "multipart intake request requires a 'metadata' part")
        else:
            metadata_bytes = await request.read()

        raw_idem: str | None = None
        try:
            decoded_meta = json.loads(metadata_bytes.decode("utf-8"))
            if isinstance(decoded_meta, dict) and isinstance(
                decoded_meta.get("idempotency_key"), str
            ):
                raw_idem = decoded_meta["idempotency_key"]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

        self._check_submit_hmac(request, metadata_bytes, raw_idem)

        try:
            dispatch = IntakeDispatchRequest.model_validate_json(metadata_bytes)
        except ValidationError as exc:
            return _error_response(422, f"invalid intake payload: {exc}")

        header_idem = (request.headers.get(HEADER_IDEMPOTENCY_KEY) or "").strip()
        if header_idem and header_idem != dispatch.idempotency_key:
            return _error_response(
                422, f"{HEADER_IDEMPOTENCY_KEY} header disagrees with payload idempotency_key"
            )
        header_dispatch = (request.headers.get(HEADER_DISPATCH_ID) or "").strip()
        if header_dispatch and header_dispatch != dispatch.dispatch_id:
            return _error_response(
                422, f"{HEADER_DISPATCH_ID} header disagrees with payload dispatch_id"
            )

        try:
            receipt, created = await self.service.submit(
                dispatch,
                bundle_bytes=bundle_bytes,
                bundle_filename=bundle_filename,
            )
        except IntakeConflictError as exc:
            return _error_response(409, str(exc))
        except IntakeValidationError as exc:
            return _error_response(422, str(exc))

        return _json_response(receipt, status=202 if created else 200)

    async def list_tasks(self, request: web.Request) -> web.Response:
        self._authorize_bearer_query(request)
        status_filter = (request.query.get("status") or "").strip() or None
        try:
            limit = max(1, min(500, int(request.query.get("limit", "100"))))
        except ValueError:
            return _error_response(422, "limit must be an integer")
        tasks = self.service.list_tasks(status=status_filter, limit=limit)
        return _json_response({"tasks": [item.model_dump(mode="json") for item in tasks]})

    async def get_task(self, request: web.Request) -> web.Response:
        self._authorize_bearer_query(request)
        ref = request.match_info["ref"]
        try:
            view = self.service.get_task(ref)
        except IntakeNotFoundError as exc:
            return _error_response(404, str(exc))
        return _json_response(view)

    async def retry_task(self, request: web.Request) -> web.Response:
        self._authorize_bearer_query(request)
        ref = request.match_info["ref"]
        try:
            view = await self.service.retry(ref)
        except IntakeNotFoundError as exc:
            return _error_response(404, str(exc))
        except IntakeConflictError as exc:
            return _error_response(409, str(exc))
        return _json_response(view, status=202)


def intake_app(settings: IntakeServerSettings) -> web.Application:
    """Create the aiohttp application for the ALE runtime task intake server."""
    if not (settings.secret.strip() or settings.token.strip()):
        raise ConfigError(
            "at least one of ALE_INTAKE_SECRET or ALE_INTAKE_TOKEN must be configured"
        )
    service = IntakeService(settings)
    api = _IntakeApi(service)
    max_body = max(64 * 1024 * 1024, settings.max_bundle_bytes + 8 * 1024 * 1024)
    app = web.Application(client_max_size=max_body)
    app[_SERVICE_KEY] = service
    app.add_routes(
        [
            web.get("/healthz", api.health),
            web.get("/v1/health", api.health),
            web.post("/v1/intake/tasks", api.submit),
            web.get("/v1/intake/tasks", api.list_tasks),
            web.get("/v1/intake/tasks/{ref}", api.get_task),
            web.post("/v1/intake/tasks/{ref}/retry", api.retry_task),
        ]
    )

    async def lifecycle(_app: web.Application) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.close()

    app.cleanup_ctx.append(lifecycle)
    return app


async def serve_intake(settings: IntakeServerSettings) -> None:
    """Serve the intake HTTP API until cancelled."""
    app = intake_app(settings)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, settings.host, settings.port).start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
