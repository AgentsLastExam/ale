"""Small durable recording primitives shared by episode artifacts."""

from __future__ import annotations

import codecs
import contextvars
import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from ale.core.blob import BlobRef, BlobText, InlineText, InlineTextOrBlob, media_class_for
from ale.core.environment import EventSink
from ale.core.lock import RunLock
from ale.core.result import ResultRecord
from ale.core.sandbox import ExecResult
from ale.core.trace import (
    CommandFinished,
    CommandStarted,
    ExecutionEvent,
    ExecutionLog,
    PartialOutputRecovered,
    TransportEvent,
)
from ale.core.trajectory import AtifTrajectory

__all__ = [
    "INLINE_TEXT_LIMIT",
    "BlobStore",
    "CommandRecorder",
    "EpisodeRecording",
    "JsonlWriter",
    "atomic_write_json",
    "execution_logging",
    "install_execution_handler",
]

INLINE_TEXT_LIMIT = 16 * 1024

_EXTENSIONS = {
    "application/json": "json",
    "application/pdf": "pdf",
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "text/plain": "txt",
}

_LOG_CONTEXT: contextvars.ContextVar[tuple[EventSink, str, str | None, str, Redactor] | None] = (
    contextvars.ContextVar("ale_execution_context", default=None)
)
_HANDLER_LOCK = threading.Lock()


def atomic_write_json(path: Path, value: BaseModel | dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value
    )
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class JsonlWriter:
    """Serialized complete-line appends with monotonic sequence assignment."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.Lock()
        self._next_seq = self._existing_next_seq()

    def _existing_next_seq(self) -> int:
        if not self.path.exists():
            return 0
        last = -1
        with self.path.open("rb") as handle:
            for raw in handle:
                if not raw.endswith(b"\n"):
                    break
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    break
                if isinstance(record, dict) and isinstance(record.get("seq"), int):
                    last = max(last, record["seq"])
        return last + 1

    def append(self, payload: dict[str, Any], *, durable: bool = False) -> int:
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            record = {**payload, "seq": seq}
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                if durable:
                    os.fsync(handle.fileno())
            return seq


class EventRecorder:
    """Validate a typed event, then let the file writer own its sequence."""

    def __init__(self, path: Path, kind: str) -> None:
        self._writer = JsonlWriter(path)
        self.kind = kind

    @property
    def path(self) -> Path:
        return self._writer.path

    def append(
        self,
        event: TransportEvent | ExecutionEvent | BaseModel | dict[str, Any],
        *,
        durable: bool = False,
    ) -> int:
        payload = (
            event.model_dump(mode="json", exclude_none=True)
            if isinstance(event, BaseModel)
            else dict(event)
        )
        payload.pop("seq", None)
        return self._writer.append(payload, durable=durable)


class BlobStore:
    """Content-addressed immutable payload storage within one episode."""

    def __init__(self, episode_dir: Path) -> None:
        self.episode_dir = episode_dir

    def put(self, data: bytes, *, media_type: str, complete: bool = True) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        media_class = media_class_for(media_type)
        extension = _safe_extension(media_type)
        name = f"sha256-{digest}" + (f".{extension}" if extension else "")
        relative = Path("blobs") / media_class.value / name
        target = self.episode_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name(f".{target.name}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, target)
        return BlobRef(
            path=relative.as_posix(),
            media_type=media_type,
            size_bytes=len(data),
            sha256=f"sha256:{digest}",
            complete=complete,
        )

    def text(self, text: str, *, complete: bool = True) -> InlineText | BlobRef:
        data = text.encode("utf-8")
        if len(data) <= INLINE_TEXT_LIMIT:
            return InlineText(inline=text, size_bytes=len(data), complete=complete)
        return self.put(data, media_type="text/plain; charset=utf-8", complete=complete)

    def partial_path(self, execution_id: str, stream: str) -> Path:
        if not execution_id or not execution_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("unsafe execution ID")
        if stream not in {"stdout", "stderr"}:
            raise ValueError("stream must be stdout or stderr")
        path = self.episode_dir / "blobs" / ".partial" / f"{execution_id}.{stream}.partial"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


class Redactor:
    """Remove framework-managed credentials before execution text reaches disk."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self.secrets = tuple(sorted((value for value in secrets if value), key=len, reverse=True))

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(
            r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
            "Bearer [REDACTED]",
            text,
        )
        return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "sk-[REDACTED]", text)


class _RedactedStream:
    def __init__(self, path: Path, redactor: Redactor) -> None:
        self.path = path
        self.redactor = redactor
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending = ""
        self.tail_chars = max([512, *(len(secret) + 1 for secret in redactor.secrets)])

    def write(self, data: bytes) -> None:
        combined = self.pending + self.decoder.decode(data)
        if len(combined) <= self.tail_chars:
            self.pending = combined
            return
        ready, self.pending = combined[: -self.tail_chars], combined[-self.tail_chars :]
        self._append(self.redactor(ready))

    def finish(self) -> None:
        self._append(self.redactor(self.pending + self.decoder.decode(b"", final=True)))
        self.pending = ""

    def _append(self, text: str) -> None:
        if not text:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()


class CommandRecorder:
    """Stream one framework/task command into a durable terminal execution record."""

    def __init__(
        self,
        *,
        execution: EventSink,
        blobs: BlobStore,
        episode_id: str,
        phase: str,
        component: str,
        execution_id: str,
        actor: Literal["framework", "task"],
        argv: list[str],
        cwd: str | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.execution = execution
        self.blobs = blobs
        self.episode_id = episode_id
        self.phase = phase
        self.component = component
        self.execution_id = execution_id
        self.redactor = Redactor(secrets)
        self.streams = {
            name: _RedactedStream(blobs.partial_path(execution_id, name), self.redactor)
            for name in ("stdout", "stderr")
        }
        self.execution.append(
            CommandStarted(
                episode_id=episode_id,
                phase=phase,
                component=component,
                execution_id=execution_id,
                actor=actor,
                argv=argv,
                cwd=cwd,
            ),
            durable=True,
        )

    async def write(self, stream: Literal["stdout", "stderr"], data: bytes) -> None:
        self.streams[stream].write(data)

    def finish(
        self,
        result: ExecResult,
        *,
        outcome: Literal["succeeded", "failed", "timed_out", "cancelled"] | None = None,
    ) -> None:
        for stream in self.streams.values():
            stream.finish()
        actual = outcome or (
            "timed_out" if result.timed_out else "succeeded" if result.exit_code == 0 else "failed"
        )
        complete = actual not in {"timed_out", "cancelled"}
        stdout = self._finalize("stdout", result.stdout, complete=complete)
        stderr = self._finalize("stderr", result.stderr, complete=complete)
        self.execution.append(
            CommandFinished(
                episode_id=self.episode_id,
                phase=self.phase,
                component=self.component,
                level="info" if actual == "succeeded" else "error",
                execution_id=self.execution_id,
                outcome=actual,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                stdout=stdout,
                stderr=stderr,
                truncated=result.truncated,
            ),
            durable=True,
        )

    def _finalize(
        self, stream: Literal["stdout", "stderr"], preview: str, *, complete: bool
    ) -> InlineTextOrBlob:
        path = self.blobs.partial_path(self.execution_id, stream)
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            path.unlink()
        else:
            text = self.redactor(preview)
        retained = self.blobs.text(text, complete=complete)
        return BlobText(blob=retained) if isinstance(retained, BlobRef) else retained


class _ExecutionHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        bound = _LOG_CONTEXT.get()
        if bound is None:
            return
        sink, episode_id, phase, component, redactor = bound
        sink.append(
            ExecutionLog(
                episode_id=episode_id,
                phase=phase,
                component=component,
                level=_level(record.levelno),
                message=redactor(record.getMessage()),
                data=getattr(record, "ale_data", {}),
            )
        )


def install_execution_handler() -> None:
    logger = logging.getLogger("ale.execution")
    with _HANDLER_LOCK:
        if any(isinstance(handler, _ExecutionHandler) for handler in logger.handlers):
            return
        logger.addHandler(_ExecutionHandler())
        logger.setLevel(logging.DEBUG)
        logger.propagate = False


@contextmanager
def execution_logging(
    sink: EventSink,
    *,
    episode_id: str,
    phase: str | None,
    component: str,
    secrets: tuple[str, ...] = (),
):
    install_execution_handler()
    token = _LOG_CONTEXT.set((sink, episode_id, phase, component, Redactor(secrets)))
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


class EpisodeRecording:
    """Own the canonical artifact paths and writers for one episode."""

    def __init__(self, episode_dir: Path) -> None:
        self.directory = episode_dir
        self.directory.mkdir(parents=True, exist_ok=True)
        self.transport = EventRecorder(self.directory / "trace.transport.jsonl", "transport")
        self.execution = EventRecorder(self.directory / "trace.execution.jsonl", "execution")
        self.blobs = BlobStore(self.directory)
        self._cleanup_temps()
        self._recover_partials()

    @property
    def trajectory_path(self) -> Path:
        return self.directory / "trajectory.json"

    @property
    def result_path(self) -> Path:
        return self.directory / "result.json"

    @property
    def lock_path(self) -> Path:
        return self.directory / "lock.json"

    def write_trajectory(self, trajectory: AtifTrajectory) -> None:
        atomic_write_json(self.trajectory_path, trajectory)

    def write_result(self, result: ResultRecord) -> None:
        atomic_write_json(self.result_path, result)

    def write_lock(self, lock: RunLock) -> None:
        atomic_write_json(self.lock_path, lock)

    def verify_blob_references(self) -> None:
        """Fail finalization if canonical records point at missing or changed payloads."""
        documents: list[Any] = []
        if self.trajectory_path.is_file():
            documents.append(json.loads(self.trajectory_path.read_text()))
        for path in (self.transport.path, self.execution.path):
            if path.is_file():
                with path.open(encoding="utf-8") as handle:
                    documents.extend(json.loads(line) for line in handle if line.endswith("\n"))
        for document in documents:
            for reference in _blob_references(document):
                target = self.directory / reference["path"]
                if not target.is_file():
                    raise ValueError(f"missing blob reference {reference['path']}")
                data = target.read_bytes()
                if "size_bytes" in reference and len(data) != reference["size_bytes"]:
                    raise ValueError(f"blob size mismatch for {reference['path']}")
                if "sha256" in reference:
                    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
                    if digest != reference["sha256"]:
                        raise ValueError(f"blob digest mismatch for {reference['path']}")

    def _recover_partials(self) -> None:
        root = self.directory / "blobs" / ".partial"
        if not root.is_dir():
            return
        for path in sorted(root.glob("*.partial")):
            name = path.name.removesuffix(".partial")
            execution_id, separator, stream = name.rpartition(".")
            if not separator or stream not in {"stdout", "stderr"}:
                continue
            ref = self.blobs.put(
                path.read_bytes(),
                media_type="text/plain; charset=utf-8",
                complete=False,
            )
            path.unlink()
            self.execution.append(
                PartialOutputRecovered(
                    episode_id=self.directory.name,
                    phase=None,
                    component="recording-recovery",
                    level="warning",
                    execution_id=execution_id,
                    stream=stream,
                    output=BlobText(blob=ref),
                ),
                durable=True,
            )

    def _cleanup_temps(self) -> None:
        root = self.directory / "blobs"
        if not root.is_dir():
            return
        for path in root.rglob("*.tmp"):
            path.unlink()


def _safe_extension(media_type: str) -> str | None:
    normalized = media_type.partition(";")[0].strip().lower()
    if normalized in _EXTENSIONS:
        return _EXTENSIONS[normalized]
    guessed = mimetypes.guess_extension(normalized, strict=True)
    if guessed:
        suffix = guessed.removeprefix(".").lower()
        if suffix.isalnum() and len(suffix) <= 9:
            return suffix
    return None


def _level(value: int) -> Literal["debug", "info", "warning", "error"]:
    if value >= logging.ERROR:
        return "error"
    if value >= logging.WARNING:
        return "warning"
    if value >= logging.INFO:
        return "info"
    return "debug"


def _blob_references(value: Any):
    if isinstance(value, dict):
        path = value.get("path")
        if isinstance(path, str) and path.startswith("blobs/"):
            yield value
        for child in value.values():
            yield from _blob_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _blob_references(child)
