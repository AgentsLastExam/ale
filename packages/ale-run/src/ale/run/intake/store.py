"""SQLite WAL ledger for incoming runtime intake dispatches."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ale.core.intake import (
    IntakeDispatchRequest,
    IntakeExecutionMode,
    IntakeReceipt,
    IntakeStatus,
    IntakeTaskView,
)

__all__ = ["IntakeRecord", "Store"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_tasks (
    id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    updated_at REAL NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS intake_tasks_by_dispatch ON intake_tasks (dispatch_id);
CREATE INDEX IF NOT EXISTS intake_tasks_by_status ON intake_tasks (status, updated_at);
"""


class IntakeRecord(BaseModel):
    """Persisted state of one accepted dispatch in the runtime intake ledger."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    dispatch_id: str
    idempotency_key: str
    ale_task_id: str
    status: IntakeStatus = IntakeStatus.ACKNOWLEDGED
    execution_mode: IntakeExecutionMode = IntakeExecutionMode.VALIDATE
    attempt_count: int = 0
    artifact_sha256: str
    bundle_path: str | None = None
    unpacked_dir: str | None = None
    staged_dir: str | None = None
    sealed: bool = False
    adapted: bool = False
    adaptation_notices: tuple[str, ...] = ()
    detail: str = ""
    result: dict[str, Any] | None = None
    request: IntakeDispatchRequest
    received_at: str
    updated_at: str
    updated_epoch: float
    started_at: str | None = None
    completed_at: str | None = None
    webhook_url: str | None = None
    webhook_delivered_at: str | None = None
    webhook_last_error: str = ""

    def to_receipt(self, *, created: bool) -> IntakeReceipt:
        return IntakeReceipt(
            id=self.id,
            ale_task_id=self.ale_task_id,
            task_id=self.ale_task_id,
            runtime="ale",
            dispatch_id=self.dispatch_id,
            idempotency_key=self.idempotency_key,
            status=self.status,
            created=created,
            execution_mode=self.execution_mode,
            status_url=f"/v1/intake/tasks/{self.id}",
            received_at=self.received_at,
        )

    def to_view(self) -> IntakeTaskView:
        return IntakeTaskView(
            id=self.id,
            ale_task_id=self.ale_task_id,
            task_id=self.ale_task_id,
            runtime="ale",
            dispatch_id=self.dispatch_id,
            idempotency_key=self.idempotency_key,
            status=self.status,
            execution_mode=self.execution_mode,
            attempt_count=self.attempt_count,
            artifact_sha256=self.artifact_sha256,
            detail=self.detail,
            result=self.result,
            received_at=self.received_at,
            updated_at=self.updated_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
            webhook_delivered_at=self.webhook_delivered_at,
            webhook_last_error=self.webhook_last_error,
        )


class Store:
    """Durable SQLite ledger for intake task state across server restarts."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def get(self, ref: str) -> IntakeRecord | None:
        """Look up a record by intake ``id``, ``dispatch_id``, or ``idempotency_key``."""
        row = self._db.execute(
            """
            SELECT body FROM intake_tasks
            WHERE id = ? OR dispatch_id = ? OR idempotency_key = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (ref, ref, ref),
        ).fetchone()
        return IntakeRecord.model_validate_json(row[0]) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> IntakeRecord | None:
        row = self._db.execute(
            "SELECT body FROM intake_tasks WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return IntakeRecord.model_validate_json(row[0]) if row is not None else None

    def put(self, record: IntakeRecord) -> None:
        self._db.execute(
            """
            INSERT OR REPLACE INTO intake_tasks
                (id, dispatch_id, idempotency_key, status, updated_at, body)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.dispatch_id,
                record.idempotency_key,
                record.status.value,
                record.updated_epoch,
                record.model_dump_json(),
            ),
        )

    def list_records(self, *, status: str | None = None, limit: int = 100) -> list[IntakeRecord]:
        if status:
            rows = self._db.execute(
                "SELECT body FROM intake_tasks WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT body FROM intake_tasks ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [IntakeRecord.model_validate_json(body) for (body,) in rows]

    def incomplete_records(self) -> list[IntakeRecord]:
        rows = self._db.execute(
            "SELECT body FROM intake_tasks WHERE status IN (?, ?) ORDER BY updated_at ASC",
            (IntakeStatus.ACKNOWLEDGED.value, IntakeStatus.RUNNING.value),
        ).fetchall()
        return [IntakeRecord.model_validate_json(body) for (body,) in rows]
