"""Host death leaves an interrupted attempt and a distinct retry."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from ale.core.taskspec import ImageSpec, TaskSpec
from ale.run.ledger import Ledger

pytestmark = pytest.mark.integration


def task() -> TaskSpec:
    return TaskSpec(
        name="demo-crash",
        instruction="work",
        image=ImageSpec(kind="container", ref="ghcr.io/example/fixture:1"),
    )


def test_reopen_marks_stale_attempt_interrupted_and_preserves_retry(tmp_path) -> None:
    root = tmp_path / "run"
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from ale.core.taskspec import ImageSpec, TaskSpec
        from ale.run.ledger import Ledger

        task = TaskSpec(
            name="demo-crash",
            instruction="work",
            image=ImageSpec(kind="container", ref="ghcr.io/example/fixture:1"),
        )
        ledger = Ledger(Path({str(root)!r}))
        ledger.open_run("run", "sha256:cfg")
        ledger.start_episode(
            episode_id="attempt-1",
            run_id="run",
            identity="same-work",
            spec=task,
        )
        ledger.update_phase("attempt-1", "agent")
        os._exit(17)
        """
    )
    completed = subprocess.run([sys.executable, "-c", script], check=False)
    assert completed.returncode == 17

    ledger = Ledger(root)
    interrupted = ledger.episodes("run")[0]
    assert interrupted.episode_id == "attempt-1"
    assert interrupted.status == "interrupted"
    assert interrupted.failure_type == "HostInterrupted"
    assert "same-work" not in ledger.completed_identities()

    ledger.queue_episode(
        episode_id="attempt-2",
        run_id="run",
        identity="same-work",
        spec=task(),
    )
    rows = ledger.episodes("run")
    assert [row.episode_id for row in rows] == ["attempt-1", "attempt-2"]
    assert [row.status for row in rows] == ["interrupted", "queued"]
    ledger.close()
