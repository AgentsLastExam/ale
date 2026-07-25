"""The run ledger.

Two records with different jobs: a SQLite table that answers "what has been done?", and
an append-only event log per episode that answers "what happened, right up to the crash?"

Resume is keyed by a content hash of everything that determines an episode — the task
spec, the agent, the seed, the configuration. Matching by name or directory is what the
previous framework did, and it silently re-ran or silently skipped whenever a path
changed. A content key cannot drift from what it identifies.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ale.core.ids import content_hash
from ale.core.taskspec import TaskSpec
from ale.core.verdict import Status, Verdict

__all__ = ["Ledger", "episode_identity"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    config_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    episode_id    TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    identity      TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    variant       TEXT,
    status        TEXT NOT NULL,
    reward        REAL,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    lock_json     TEXT
);
CREATE INDEX IF NOT EXISTS episodes_identity ON episodes (identity);
"""


def episode_identity(spec: TaskSpec, *, agent: str, seed: int, config_hash: str) -> str:
    """What makes two episodes the same piece of work.

    Everything that would change the result belongs here; nothing that would not.
    """
    return content_hash(
        {
            "spec": spec.spec_hash,
            "agent": agent,
            "seed": seed,
            "config": config_hash,
        }
    )


@dataclass
class EpisodeRow:
    episode_id: str
    identity: str
    status: str
    reward: float | None

    @property
    def succeeded(self) -> bool:
        return self.status == Status.COMPLETED


class Ledger:
    """Durable record of a run, and the basis for resuming it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.root / "ledger.db")
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- runs ---

    def open_run(self, run_id: str, config_hash: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO runs (run_id, created_at, config_hash) VALUES (?, ?, ?)",
            (run_id, time.time(), config_hash),
        )
        self._db.commit()

    # --- episodes ---

    def completed_identities(self) -> set[str]:
        """Work that resume may skip.

        Only successes count: a failed episode is worth retrying, and treating it as
        done would quietly bake a transient error into a result set.
        """
        rows = self._db.execute(
            "SELECT identity FROM episodes WHERE status = ?", (Status.COMPLETED.value,)
        ).fetchall()
        return {row["identity"] for row in rows}

    def start_episode(self, *, episode_id: str, run_id: str, identity: str, spec: TaskSpec) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO episodes "
            "(episode_id, run_id, identity, task_id, variant, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                episode_id,
                run_id,
                identity,
                str(spec.id),
                spec.variant,
                "running",
                time.time(),
            ),
        )
        self._db.commit()

    def finish_episode(
        self, episode_id: str, verdict: Verdict, lock: dict[str, Any] | None = None
    ) -> None:
        self._db.execute(
            "UPDATE episodes SET status = ?, reward = ?, finished_at = ?, lock_json = ? "
            "WHERE episode_id = ?",
            (
                verdict.status.value,
                verdict.primary_reward,
                time.time(),
                json.dumps(lock) if lock else None,
                episode_id,
            ),
        )
        self._db.commit()

    def episodes(self, run_id: str | None = None) -> list[EpisodeRow]:
        query = "SELECT episode_id, identity, status, reward FROM episodes"
        params: tuple[Any, ...] = ()
        if run_id:
            query += " WHERE run_id = ?"
            params = (run_id,)
        return [
            EpisodeRow(
                episode_id=row["episode_id"],
                identity=row["identity"],
                status=row["status"],
                reward=row["reward"],
            )
            for row in self._db.execute(query, params).fetchall()
        ]

    # --- events ---

    def event(self, episode_id: str, kind: str, **data: Any) -> None:
        """Append one lifecycle event, flushed immediately.

        The database records outcomes; this records the path to them, so a killed
        process still explains how far it got.
        """
        path = self.root / episode_id / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.time(), "kind": kind, **data}) + "\n")
            handle.flush()
