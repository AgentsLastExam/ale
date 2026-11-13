"""The control plane's ledger on disk: one SQLite file, written through on every change.

The control plane works on its records in memory and writes each one back as it
changes, so a restart resumes from the last state it acknowledged. Records are stored
whole, as JSON, beside the few columns queries select on.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ale.run.fleet.placement import Holds
from ale.run.fleet.protocol import (
    AcquireRequest,
    EpisodeReport,
    ImageHolding,
    NodeDeclaration,
    RunPlan,
    SandboxDemand,
    SandboxStatus,
)

__all__ = ["Event", "NodeRecord", "RunRecord", "SandboxRecord", "SnapshotRecord", "Store"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (name TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sandboxes (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL,
    updated_at REAL NOT NULL, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sandboxes_by_status ON sandboxes (status, updated_at);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL,
    node TEXT NOT NULL DEFAULT '', sandbox TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
"""

_RECORD = ConfigDict(extra="forbid", validate_assignment=True)


class NodeRecord(BaseModel):
    model_config = _RECORD

    declaration: NodeDeclaration
    registered_at: float
    last_seen: float
    online: bool = True
    draining: bool = False
    images: tuple[ImageHolding, ...] = ()
    disk_free_gb: int = 0


class SandboxRecord(BaseModel):
    model_config = _RECORD

    sandbox_id: str
    run_id: str
    status: SandboxStatus = SandboxStatus.QUEUED
    demand: SandboxDemand
    episode_id: str = ""
    role: str = ""
    reason: str = ""
    node: str | None = None
    provider: str | None = None
    """The Node's Provider for the profile it was placed on."""
    holds: Holds | None = None
    refused: tuple[str, ...] = ()
    """Nodes that refused it before creating anything; it is not placed there again."""
    released: bool = False
    """Its Run released it, so any copy a Node still holds is to be destroyed."""
    roles: tuple[Literal["solver", "verifier"], ...] = ()
    """What a retained Sandbox was kept for."""
    continuation: AcquireRequest | None = None
    """The later Run's request to continue in a retained Sandbox, granted and pending until
    its Node's roster shows that Run has it."""
    lease_expires_at: float | None = None
    create_sec: float | None = None
    """Its Node's Provider's time from the create request to a ready Sandbox."""
    queue_sec: float | None = None
    image_sec: float | None = None
    created_at: float
    seen_at: float
    """When its Run last asked about it; a queued or placed Sandbox nobody asks about lapses."""
    placed_at: float | None = None
    ready_at: float | None = None
    ended_at: float | None = None


class SnapshotRecord(BaseModel):
    """A snapshot one Node keeps until an operator destroys it; it outlives every Run."""

    model_config = _RECORD

    snapshot_id: str
    """The Sandbox it was taken from, whose name it keeps."""
    node: str
    provider: str
    demand: SandboxDemand
    episode_id: str = ""
    roles: tuple[Literal["solver", "verifier"], ...] = ()
    reason: str = ""
    created_at: float
    released: bool = False
    """An operator destroyed it: its Node is to delete it."""


class RunRecord(BaseModel):
    model_config = _RECORD

    run_id: str
    created_at: float
    last_served_at: float = 0.0
    """When placement last gave this Run a Sandbox; the queue serves the longest-waiting first."""
    unreachable: tuple[str, ...] = ()
    """Nodes this Run could not reach; none of its Sandboxes is placed there again."""
    plan: RunPlan | None = None
    assignments: dict[str, tuple[str, ...]] = {}
    """The Nodes the plan chose for each image."""
    episodes: dict[str, EpisodeReport] = {}


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    at: float
    kind: str
    node: str = ""
    sandbox: str = ""
    detail: str = ""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def nodes(self) -> list[NodeRecord]:
        rows = self._db.execute("SELECT body FROM nodes").fetchall()
        return [NodeRecord.model_validate_json(body) for (body,) in rows]

    def sandboxes(self, *, since: float) -> list[SandboxRecord]:
        """Every Sandbox still open, and those that ended after ``since``."""
        rows = self._db.execute(
            "SELECT body FROM sandboxes WHERE status NOT IN (?, ?) OR updated_at >= ?",
            (SandboxStatus.RELEASED.value, SandboxStatus.FAILED.value, since),
        ).fetchall()
        return [SandboxRecord.model_validate_json(body) for (body,) in rows]

    def snapshots(self) -> list[SnapshotRecord]:
        rows = self._db.execute("SELECT body FROM snapshots").fetchall()
        return [SnapshotRecord.model_validate_json(body) for (body,) in rows]

    def put_snapshot(self, record: SnapshotRecord) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO snapshots (id, body) VALUES (?, ?)",
            (record.snapshot_id, record.model_dump_json()),
        )

    def delete_snapshot(self, snapshot_id: str) -> None:
        self._db.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))

    def runs(self) -> list[RunRecord]:
        rows = self._db.execute("SELECT body FROM runs").fetchall()
        return [RunRecord.model_validate_json(body) for (body,) in rows]

    def put_node(self, record: NodeRecord) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO nodes (name, body) VALUES (?, ?)",
            (record.declaration.name, record.model_dump_json()),
        )

    def put_sandbox(self, record: SandboxRecord, *, now: float) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO sandboxes (id, run_id, status, updated_at, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (record.sandbox_id, record.run_id, record.status.value, now, record.model_dump_json()),
        )

    def put_run(self, record: RunRecord) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO runs (id, body) VALUES (?, ?)",
            (record.run_id, record.model_dump_json()),
        )

    def add_event(self, event: Event) -> None:
        self._db.execute(
            "INSERT INTO events (at, kind, node, sandbox, detail) VALUES (?, ?, ?, ?, ?)",
            (event.at, event.kind, event.node, event.sandbox, event.detail),
        )

    def events(self, limit: int = 200) -> list[Event]:
        rows = self._db.execute(
            "SELECT at, kind, node, sandbox, detail FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            Event(at=at, kind=kind, node=node, sandbox=sandbox, detail=detail)
            for at, kind, node, sandbox, detail in rows
        ]
