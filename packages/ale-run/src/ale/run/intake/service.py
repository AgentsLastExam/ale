"""Asynchronous task intake orchestration, bundle fetching, execution, and webhooks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp

from ale.core.config import RunConfig, load_run_config
from ale.core.errors import AleError, TaskDefinitionError
from ale.core.intake import (
    DEFAULT_INTAKE_PORT,
    DEFAULT_MAX_SKEW_SEC,
    HEADER_SIGNATURE,
    IntakeDispatchRequest,
    IntakeExecutionMode,
    IntakeReceipt,
    IntakeStatus,
    IntakeTaskMetadata,
    IntakeTaskView,
    IntakeWebhookPayload,
    sign_webhook,
)
from ale.run.intake.bundle import (
    MAX_ARCHIVE_UNCOMPRESSED_BYTES,
    stage_task_for_ale,
    unpack_archive,
    verify_unpacked_bundle,
)
from ale.run.intake.store import IntakeRecord, Store

__all__ = [
    "IntakeConflictError",
    "IntakeExecutionError",
    "IntakeNotFoundError",
    "IntakeServerSettings",
    "IntakeService",
    "IntakeValidationError",
]

_log = logging.getLogger(__name__)
_DOWNLOAD_CHUNK = 65_536
_SLUG_PART = re.compile(r"[^a-z0-9_-]+")
_BLOCKED_METADATA_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fe80::/10"),
)


class IntakeNotFoundError(AleError):
    """Requested intake record does not exist."""


class IntakeConflictError(AleError):
    """Idempotency key or state transition conflicts with existing record."""


class IntakeValidationError(AleError):
    """Inbound payload, bundle digest, or URL failed boundary validation."""


class IntakeExecutionError(AleError):
    """Runtime execution failed while keeping structured diagnostics."""

    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload


@dataclass(frozen=True)
class IntakeServerSettings:
    """Configuration for the RESTful task intake server."""

    state_dir: Path
    runs_dir: Path
    host: str = "0.0.0.0"
    port: int = DEFAULT_INTAKE_PORT
    secret: str = ""
    token: str = ""
    webhook_url: str = ""
    webhook_secret: str = ""
    allowed_origins: tuple[str, ...] = ()
    execution_mode: IntakeExecutionMode = IntakeExecutionMode.VALIDATE
    concurrency: int = 1
    max_skew_sec: int = DEFAULT_MAX_SKEW_SEC
    max_bundle_bytes: int = MAX_ARCHIVE_UNCOMPRESSED_BYTES
    run_settings: RunConfig | None = None
    executor: Callable[[Path, IntakeExecutionMode, Path, str], Awaitable[dict[str, Any]]] | None = (
        None
    )

    @property
    def effective_webhook_secret(self) -> str:
        return self.webhook_secret or self.secret or self.token


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_filename(raw_name: str, fallback: str) -> str:
    name = Path(raw_name.replace("\\", "/")).name.strip()
    if not name or name in {".", ".."}:
        return fallback
    return name


def _slug(raw: str, fallback: str) -> str:
    cleaned = _SLUG_PART.sub("_", raw.strip().lower()).strip("_-")
    if not cleaned:
        return fallback
    if not cleaned[0].isalnum():
        cleaned = f"t_{cleaned}"
    return cleaned[:100]


def _default_org(task: IntakeTaskMetadata) -> str:
    repo = task.task_repo.lower()
    if "openrsi-x" in repo:
        return "openrsi-x"
    if "openrsi-index" in repo:
        return "openrsi-index"
    if task.domain:
        head = _slug(task.domain.split("/")[0], "")
        if head:
            return head
    return "openrsi"


def _initial_task_id(task: IntakeTaskMetadata) -> str:
    if "/" in task.task_name:
        org, _, name = task.task_name.partition("/")
        return f"{_slug(org, _default_org(task))}/{_slug(name, 'task')}"
    return f"{_default_org(task)}/{_slug(task.task_name, 'task')}"


def _validate_remote_url(raw_url: str, allowed_origins: tuple[str, ...]) -> str:
    url = raw_url.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise IntakeValidationError(f"URL must be an absolute http(s) URL: {raw_url!r}")
    origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    if allowed_origins:
        normalized_allowed = {
            entry.rstrip("/").lower() for entry in allowed_origins if entry.strip()
        }
        if origin not in normalized_allowed:
            raise IntakeValidationError(f"URL origin {origin!r} is not in allowed_origins")
    try:
        addr = ipaddress.ip_address(parts.hostname)
    except ValueError:
        addr = None
    if addr is not None and any(
        addr.version == net.version and addr in net for net in _BLOCKED_METADATA_NETS
    ):
        raise IntakeValidationError(f"URL targets disallowed host address: {parts.hostname}")
    return url


def _resolve_webhook_url(
    request: IntakeDispatchRequest,
    settings: IntakeServerSettings,
) -> str | None:
    configured = settings.webhook_url.strip()
    if configured and configured.lower() != "auto":
        return _validate_remote_url(configured, settings.allowed_origins)
    if request.status_webhook_url and request.status_webhook_url.strip():
        return _validate_remote_url(request.status_webhook_url, settings.allowed_origins)
    if configured.lower() == "auto" and request.artifact.download_url:
        parts = urlsplit(request.artifact.download_url.strip())
        if "/api/v1/outbox/bundles/" in parts.path and parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}/api/v1/webhooks/ale-status"
    return None


def _default_run_settings(mode: IntakeExecutionMode) -> RunConfig:
    if mode is IntakeExecutionMode.RUN:
        return load_run_config(overrides=["agent.name=nop"])
    return load_run_config(overrides=["agent.name=oracle"])


async def _default_execute(
    task_dir: Path,
    mode: IntakeExecutionMode,
    runs_dir: Path,
    run_id: str,
    run_settings: RunConfig | None,
) -> dict[str, Any]:
    # Lazy imports keep intake modules cross-platform when only bundle/HTTP layers run on Windows.
    from ale.core.validation import ValidationObservation
    from ale.run.cli.tasks import _prepare_only, _run_one, _validate
    from ale.run.lint import lint_repository
    from ale.run.tasksets.manifest import load_tasks

    findings = lint_repository(task_dir)
    if findings:
        detail = "; ".join(str(item) for item in findings)[:2000]
        raise TaskDefinitionError(f"ale lint failed: {detail}")
    tasks = load_tasks(task_dir)
    loaded_ids = [str(item.spec.name) for item in tasks]

    if mode is IntakeExecutionMode.LINT:
        return {"mode": mode.value, "lint_ok": True, "tasks": loaded_ids}

    effective_settings = run_settings or _default_run_settings(mode)
    if mode is IntakeExecutionMode.PREPARE:
        exit_code = await _prepare_only(str(task_dir), effective_settings)
        if exit_code != 0:
            raise IntakeExecutionError(
                f"ale prepare failed with exit code {exit_code}",
                payload={"mode": mode.value, "lint_ok": True, "exit_code": exit_code},
            )
        return {"mode": mode.value, "lint_ok": True, "tasks": loaded_ids, "exit_code": exit_code}

    if mode is IntakeExecutionMode.VALIDATE:
        runs_dir.mkdir(parents=True, exist_ok=True)
        exit_code = await _validate(str(task_dir), effective_settings, runs_dir, run_id=run_id)
        obs_path = runs_dir / run_id / "validation.json"
        validation_payload: dict[str, Any] | None = None
        if obs_path.is_file():
            obs = ValidationObservation.model_validate_json(obs_path.read_text(encoding="utf-8"))
            validation_payload = obs.model_dump(mode="json")
        payload = {
            "mode": mode.value,
            "lint_ok": True,
            "run_id": run_id,
            "exit_code": exit_code,
            "validation": validation_payload,
        }
        if exit_code != 0:
            raise IntakeExecutionError(
                f"ale validate failed with exit code {exit_code}",
                payload=payload,
            )
        return payload

    runs_dir.mkdir(parents=True, exist_ok=True)
    exit_code = await _run_one(str(task_dir), effective_settings, runs_dir, run_id=run_id)
    payload = {
        "mode": mode.value,
        "lint_ok": True,
        "run_id": run_id,
        "tasks": loaded_ids,
        "exit_code": exit_code,
    }
    if exit_code != 0:
        raise IntakeExecutionError(
            f"ale run failed with exit code {exit_code}",
            payload=payload,
        )
    return payload


class IntakeService:
    """Owns intake submission, idempotency, background processing, and Brewer webhooks."""

    def __init__(self, settings: IntakeServerSettings) -> None:
        self.settings = settings
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.runs_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.settings.state_dir / "intake.db")
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._http: aiohttp.ClientSession | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._http = aiohttp.ClientSession()
        for record in self.store.incomplete_records():
            await self._queue.put(record.id)
        worker_count = max(1, self.settings.concurrency)
        for index in range(worker_count):
            self._workers.append(
                asyncio.create_task(self._worker_loop(), name=f"ale-intake-{index}")
            )

    async def close(self) -> None:
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._workers.clear()
        if self._http is not None:
            await self._http.close()
            self._http = None
        self.store.close()
        self._started = False

    def list_tasks(self, *, status: str | None = None, limit: int = 100) -> list[IntakeTaskView]:
        return [record.to_view() for record in self.store.list_records(status=status, limit=limit)]

    def get_task(self, ref: str) -> IntakeTaskView:
        record = self.store.get(ref)
        if record is None:
            raise IntakeNotFoundError(f"unknown intake task {ref!r}")
        return record.to_view()

    async def submit(
        self,
        request: IntakeDispatchRequest,
        *,
        bundle_bytes: bytes | None = None,
        bundle_filename: str | None = None,
    ) -> tuple[IntakeReceipt, bool]:
        existing = self.store.get_by_idempotency_key(request.idempotency_key)
        if existing is not None:
            if existing.artifact_sha256 != request.artifact.sha256:
                raise IntakeConflictError(
                    f"idempotency_key {request.idempotency_key!r} is already bound to a different "
                    "artifact digest"
                )
            return existing.to_receipt(created=False), False

        mode = request.execution_mode or self.settings.execution_mode
        webhook_url = _resolve_webhook_url(request, self.settings)
        intake_id = uuid.uuid4().hex
        task_root = self.settings.state_dir / "tasks" / intake_id
        bundle_path: str | None = None

        if bundle_bytes is not None:
            if len(bundle_bytes) > self.settings.max_bundle_bytes:
                raise IntakeValidationError("uploaded bundle exceeds configured size limit")
            actual_sha = hashlib.sha256(bundle_bytes).hexdigest()
            if actual_sha != request.artifact.sha256:
                raise IntakeValidationError(
                    f"uploaded bundle sha256 {actual_sha} does not match expected "
                    f"{request.artifact.sha256}"
                )
            safe_name = _safe_filename(bundle_filename or request.artifact.name, "bundle.zip")
            dest = task_root / "bundle" / safe_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.tmp-{uuid.uuid4().hex}")
            tmp.write_bytes(bundle_bytes)
            tmp.replace(dest)
            bundle_path = str(dest)
        else:
            if not request.artifact.download_url.strip():
                raise IntakeValidationError(
                    "artifact.download_url is required when no multipart bundle is uploaded"
                )
            _validate_remote_url(request.artifact.download_url, self.settings.allowed_origins)

        now_iso = _now_iso()
        record = IntakeRecord(
            id=intake_id,
            dispatch_id=request.dispatch_id,
            idempotency_key=request.idempotency_key,
            ale_task_id=_initial_task_id(request.task),
            status=IntakeStatus.ACKNOWLEDGED,
            execution_mode=mode,
            artifact_sha256=request.artifact.sha256,
            bundle_path=bundle_path,
            detail="accepted for processing",
            request=request,
            received_at=now_iso,
            updated_at=now_iso,
            updated_epoch=time.time(),
            webhook_url=webhook_url,
        )
        self.store.put(record)
        await self._queue.put(record.id)
        return record.to_receipt(created=True), True

    async def retry(self, ref: str) -> IntakeTaskView:
        record = self.store.get(ref)
        if record is None:
            raise IntakeNotFoundError(f"unknown intake task {ref!r}")
        if record.status is not IntakeStatus.FAILED:
            raise IntakeConflictError(
                f"intake task {record.id!r} cannot be retried from status {record.status.value!r}"
            )
        record.status = IntakeStatus.ACKNOWLEDGED
        record.detail = "re-queued for retry"
        record.completed_at = None
        record.updated_at = _now_iso()
        record.updated_epoch = time.time()
        self.store.put(record)
        await self._queue.put(record.id)
        return record.to_view()

    async def _worker_loop(self) -> None:
        while True:
            intake_id = await self._queue.get()
            try:
                await self._process(intake_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("unhandled intake worker error for %s", intake_id)
            finally:
                self._queue.task_done()

    async def _download_bundle(self, record: IntakeRecord, dest: Path) -> Path:
        assert self._http is not None
        url = _validate_remote_url(
            record.request.artifact.download_url, self.settings.allowed_origins
        )
        headers: dict[str, str] = {}
        query = parse_qs(urlsplit(url).query)
        if self.settings.token and "signature" not in query:
            headers["Authorization"] = f"Bearer {self.settings.token}"

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.tmp-{uuid.uuid4().hex}")
        hasher = hashlib.sha256()
        total = 0
        try:
            async with self._http.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as response:
                if response.status != 200:
                    body = (await response.text(errors="replace"))[:400]
                    raise IntakeValidationError(
                        f"bundle download failed with HTTP {response.status}: {body}"
                    )
                with tmp.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(_DOWNLOAD_CHUNK):
                        total += len(chunk)
                        if total > self.settings.max_bundle_bytes:
                            raise IntakeValidationError("downloaded bundle exceeds size limit")
                        hasher.update(chunk)
                        handle.write(chunk)
            digest = hasher.hexdigest()
            if digest != record.artifact_sha256:
                raise IntakeValidationError(
                    f"downloaded bundle sha256 {digest} does not match expected "
                    f"{record.artifact_sha256}"
                )
            tmp.replace(dest)
            return dest
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    async def _emit_webhook(self, record: IntakeRecord) -> None:
        if not record.webhook_url or self._http is None:
            return
        payload = IntakeWebhookPayload(
            dispatch_id=record.dispatch_id,
            idempotency_key=record.idempotency_key,
            status=record.status,
            ale_task_id=record.ale_task_id,
            detail=record.detail[:4000],
            response_body=record.to_view().model_dump(mode="json"),
        )
        raw_bytes = payload.model_dump_json().encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = self.settings.effective_webhook_secret
        if secret:
            headers[HEADER_SIGNATURE] = sign_webhook(secret, raw_bytes)
        try:
            async with self._http.post(
                record.webhook_url,
                data=raw_bytes,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if 200 <= response.status < 300:
                    record.webhook_delivered_at = _now_iso()
                    record.webhook_last_error = ""
                else:
                    text = (await response.text(errors="replace"))[:300]
                    record.webhook_last_error = f"HTTP {response.status}: {text}"
        except (aiohttp.ClientError, TimeoutError) as exc:
            record.webhook_last_error = f"{type(exc).__name__}: {exc}"
        record.updated_at = _now_iso()
        record.updated_epoch = time.time()
        self.store.put(record)

    async def _process(self, intake_id: str) -> None:
        record = self.store.get(intake_id)
        if record is None or record.status.terminal:
            return

        task_root = self.settings.state_dir / "tasks" / record.id
        try:
            bundle_path = Path(record.bundle_path) if record.bundle_path else None
            if bundle_path is None or not bundle_path.is_file():
                safe_name = _safe_filename(record.request.artifact.name, "bundle.zip")
                downloaded = await self._download_bundle(record, task_root / "bundle" / safe_name)
                record.bundle_path = str(downloaded)
                bundle_path = downloaded

            unpacked_root = task_root / "unpacked"
            unpack_archive(bundle_path, unpacked_root)
            unpacked = verify_unpacked_bundle(unpacked_root)
            staged_root = task_root / "staged"
            staged = stage_task_for_ale(
                unpacked.task_dir,
                staged_root,
                fallback_org=_default_org(record.request.task),
            )

            now_iso = _now_iso()
            record.ale_task_id = staged.task_id
            record.unpacked_dir = str(unpacked.bundle_root)
            record.staged_dir = str(staged.path)
            record.sealed = unpacked.sealed
            record.adapted = staged.adapted
            record.adaptation_notices = staged.notices
            record.status = IntakeStatus.RUNNING
            record.attempt_count += 1
            record.started_at = record.started_at or now_iso
            record.updated_at = now_iso
            record.updated_epoch = time.time()
            record.detail = f"running mode={record.execution_mode.value}"
            self.store.put(record)
            await self._emit_webhook(record)

            run_id = f"intake-{record.id[:12]}-a{record.attempt_count}"
            if self.settings.executor is not None:
                exec_result = await self.settings.executor(
                    staged.path, record.execution_mode, self.settings.runs_dir, run_id
                )
            else:
                exec_result = await _default_execute(
                    staged.path,
                    record.execution_mode,
                    self.settings.runs_dir,
                    run_id,
                    self.settings.run_settings,
                )

            done_iso = _now_iso()
            record.status = IntakeStatus.COMPLETED
            record.detail = "completed"
            record.completed_at = done_iso
            record.updated_at = done_iso
            record.updated_epoch = time.time()
            record.result = {
                "task_id": staged.task_id,
                "sealed": unpacked.sealed,
                "task_digest": unpacked.task_digest,
                "adapted": staged.adapted,
                "adaptation_notices": list(staged.notices),
                **exec_result,
            }
            self.store.put(record)
            await self._emit_webhook(record)
        except Exception as exc:
            _log.warning("intake task %s failed: %s", record.id, exc)
            fail_iso = _now_iso()
            record.status = IntakeStatus.FAILED
            record.detail = str(exc)[:4000]
            record.completed_at = fail_iso
            record.updated_at = fail_iso
            record.updated_epoch = time.time()
            extra = exc.payload if isinstance(exc, IntakeExecutionError) and exc.payload else {}
            record.result = {
                "error": type(exc).__name__,
                "detail": str(exc)[:4000],
                "sealed": record.sealed,
                "adapted": record.adapted,
                "adaptation_notices": list(record.adaptation_notices),
                **extra,
            }
            self.store.put(record)
            await self._emit_webhook(record)
