"""A separate viewer observes committed run state while work is active."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from ale.core.ids import TaskId
from ale.core.result import ResultRecord
from ale.core.taskspec import ImageRef, TaskSpec
from ale.core.verdict import Status
from ale.run.ledger import Ledger

pytestmark = pytest.mark.integration


def task() -> TaskSpec:
    return TaskSpec(
        id=TaskId("demo-live"),
        domain="demo",
        instruction="work",
        image=ImageRef(name="sandbox-base-cli"),
    )


def view(path) -> tuple[str, str | None, float | None]:
    db = sqlite3.connect(path)
    try:
        return db.execute(
            "SELECT status, current_phase, started_at FROM episodes WHERE episode_id='e1'"
        ).fetchone()
    finally:
        db.close()


def test_viewer_observes_queued_running_phase_and_terminal_state(tmp_path) -> None:
    root = tmp_path / "run"
    ledger = Ledger(root)
    ledger.open_run("run", "sha256:cfg")
    ledger.queue_episode(
        episode_id="e1",
        run_id="run",
        identity="identity",
        spec=task(),
    )
    assert view(root / "ledger.db") == ("queued", None, None)

    ledger.mark_running("e1")
    ledger.update_phase("e1", "setup")
    status, phase, started_at = view(root / "ledger.db")
    assert status == "running" and phase == "setup" and started_at is not None

    now = datetime.now(UTC)
    ledger.finish_episode(
        "e1",
        ResultRecord(
            episode_id="e1",
            status=Status.COMPLETED,
            rewards={"correctness": 1.0, "format": 1.0},
            started_at=now,
            finished_at=now,
        ),
    )
    assert view(root / "ledger.db")[0:2] == ("completed", None)
    ledger.close()
