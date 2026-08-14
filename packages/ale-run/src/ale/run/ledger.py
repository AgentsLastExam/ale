"""Run-level live status and terminal-result projection."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ale.core.ids import content_hash
from ale.core.result import ResultRecord
from ale.core.taskspec import BaseTaskSpec
from ale.core.verdict import Status

__all__ = ["EpisodeRow", "Ledger", "episode_identity"]

_EPISODE_COLUMNS = (
    "episode_id",
    "run_id",
    "identity",
    "task_id",
    "variant",
    "episode_path",
    "status",
    "current_phase",
    "started_at",
    "updated_at",
    "finished_at",
    "rewards_json",
    "failure_type",
    "failure_message",
)

_RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    config_hash TEXT NOT NULL
);
"""

_EPISODE_SCHEMA = """
CREATE TABLE episodes (
    episode_id      TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    identity        TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    variant         TEXT,
    episode_path    TEXT NOT NULL,
    status          TEXT NOT NULL,
    current_phase   TEXT,
    started_at      REAL,
    updated_at      REAL NOT NULL,
    finished_at     REAL,
    rewards_json    TEXT,
    failure_type    TEXT,
    failure_message TEXT
);
CREATE INDEX episodes_run_identity
ON episodes (run_id, identity, status);
"""


def episode_identity(
    spec: BaseTaskSpec,
    *,
    task_digest: str,
    image_digest: str,
    agent: str,
    seed: int,
    config_hash: str,
    resources_digest: str = "",
) -> str:
    return content_hash(
        {
            "spec": spec.spec_hash,
            "task": task_digest,
            "image": image_digest,
            "agent": agent,
            "seed": seed,
            "config": config_hash,
            "resources": resources_digest,
        }
    )


@dataclass(frozen=True)
class EpisodeRow:
    episode_id: str
    identity: str
    status: str
    episode_path: str
    current_phase: str | None
    started_at: float | None
    updated_at: float
    finished_at: float | None
    rewards: dict[str, float] | None
    failure_type: str | None
    failure_message: str | None

    @property
    def succeeded(self) -> bool:
        return self.status == Status.COMPLETED.value


class Ledger:
    """One SQLite projection per run; episode files remain authoritative."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.root / "ledger.db", timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()
        self._reconcile_terminal_rows()
        self._interrupt_stale_rows()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _ensure_schema(self) -> None:
        with self._lock:
            self._db.executescript(_RUN_SCHEMA)
            exists = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='episodes'"
            ).fetchone()
            if not exists:
                self._db.executescript(_EPISODE_SCHEMA)
                self._db.commit()
                return
            columns = tuple(
                row["name"] for row in self._db.execute("PRAGMA table_info(episodes)").fetchall()
            )
            if columns == _EPISODE_COLUMNS:
                return
            legacy = self._db.execute("SELECT * FROM episodes").fetchall()
            self._db.executescript(
                "DROP INDEX IF EXISTS episodes_identity;"
                "DROP INDEX IF EXISTS episodes_run_identity;"
                "ALTER TABLE episodes RENAME TO episodes_legacy;" + _EPISODE_SCHEMA
            )
            now = time.time()
            for row in legacy:
                names = set(row.keys())
                reward = row["reward"] if "reward" in names else None
                rewards = {"reward": reward} if reward is not None else None
                started = row["started_at"] if "started_at" in names else None
                finished = row["finished_at"] if "finished_at" in names else None
                self._db.execute(
                    "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["episode_id"],
                        row["run_id"],
                        row["identity"],
                        row["task_id"],
                        row["variant"] if "variant" in names else None,
                        row["episode_id"],
                        row["status"],
                        None,
                        started,
                        finished or started or now,
                        finished,
                        json.dumps(rewards) if rewards else None,
                        None,
                        None,
                    ),
                )
            self._db.execute("DROP TABLE episodes_legacy")
            self._db.commit()

    def open_run(self, run_id: str, config_hash: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO runs (run_id, created_at, config_hash) VALUES (?, ?, ?)",
                (run_id, time.time(), config_hash),
            )
            self._db.commit()

    def completed_identities(self) -> set[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT identity FROM episodes WHERE status = ?",
                (Status.COMPLETED.value,),
            ).fetchall()
        return {row["identity"] for row in rows}

    def queue_episode(
        self,
        *,
        episode_id: str,
        run_id: str,
        identity: str,
        spec: BaseTaskSpec,
        episode_path: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO episodes "
                "(episode_id, run_id, identity, task_id, variant, episode_path, "
                "status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode_id,
                    run_id,
                    identity,
                    str(spec.id),
                    spec.variant,
                    episode_path or episode_id,
                    "queued",
                    now,
                ),
            )
            self._db.commit()

    def mark_running(self, episode_id: str) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "UPDATE episodes SET status='running', current_phase=NULL, "
                "started_at=?, updated_at=? WHERE episode_id=?",
                (now, now, episode_id),
            )
            self._db.commit()

    def start_episode(
        self,
        *,
        episode_id: str,
        run_id: str,
        identity: str,
        spec: BaseTaskSpec,
        episode_path: str | None = None,
    ) -> None:
        self.queue_episode(
            episode_id=episode_id,
            run_id=run_id,
            identity=identity,
            spec=spec,
            episode_path=episode_path,
        )
        self.mark_running(episode_id)

    def update_phase(self, episode_id: str, phase: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE episodes SET current_phase=?, updated_at=? WHERE episode_id=?",
                (phase, time.time(), episode_id),
            )
            self._db.commit()

    def finish_episode(self, episode_id: str, result: ResultRecord) -> None:
        failure = result.failure
        with self._lock:
            self._db.execute(
                "UPDATE episodes SET status=?, current_phase=NULL, updated_at=?, "
                "finished_at=?, rewards_json=?, failure_type=?, failure_message=? "
                "WHERE episode_id=?",
                (
                    result.status.value,
                    time.time(),
                    result.finished_at.timestamp(),
                    json.dumps(result.rewards, sort_keys=True)
                    if result.rewards is not None
                    else None,
                    failure.error_type if failure else None,
                    failure.message[:1000] if failure else None,
                    episode_id,
                ),
            )
            self._db.commit()

    def episodes(self, run_id: str | None = None) -> list[EpisodeRow]:
        query = (
            "SELECT episode_id, identity, status, episode_path, current_phase, "
            "started_at, updated_at, finished_at, rewards_json, failure_type, "
            "failure_message FROM episodes"
        )
        params: tuple[Any, ...] = ()
        if run_id:
            query += " WHERE run_id = ?"
            params = (run_id,)
        query += " ORDER BY rowid"
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [
            EpisodeRow(
                episode_id=row["episode_id"],
                identity=row["identity"],
                status=row["status"],
                episode_path=row["episode_path"],
                current_phase=row["current_phase"],
                started_at=row["started_at"],
                updated_at=row["updated_at"],
                finished_at=row["finished_at"],
                rewards=json.loads(row["rewards_json"]) if row["rewards_json"] else None,
                failure_type=row["failure_type"],
                failure_message=row["failure_message"],
            )
            for row in rows
        ]

    def _reconcile_terminal_rows(self) -> None:
        rows = self._db.execute("SELECT episode_id, episode_path, status FROM episodes").fetchall()
        for row in rows:
            result_path = self.root / row["episode_path"] / "result.json"
            if not result_path.is_file():
                continue
            try:
                result = ResultRecord.model_validate_json(result_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            self.finish_episode(row["episode_id"], result)

    def _interrupt_stale_rows(self) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "UPDATE episodes SET status='interrupted', current_phase=NULL, "
                "updated_at=?, finished_at=?, failure_type='HostInterrupted', "
                "failure_message='run process ended before a terminal result' "
                "WHERE status IN ('queued', 'running')",
                (now, now),
            )
            self._db.commit()
