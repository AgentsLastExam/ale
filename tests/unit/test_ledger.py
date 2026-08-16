"""Run identity and the live SQLite projection."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ale.core.lock import AssetProvenance
from ale.core.result import FailureInfo, ResultRecord
from ale.core.taskspec import StandardTaskSpec
from ale.core.verdict import Status
from ale.run.ledger import Ledger, episode_identity

pytestmark = pytest.mark.unit


def spec(**overrides: object) -> StandardTaskSpec:
    base: dict[str, object] = {
        "name": "demo-hello",
        "instruction": "write hello",
        "image": {"kind": "container"},
    }
    return StandardTaskSpec(**(base | overrides))  # type: ignore[arg-type]


def identity(task: StandardTaskSpec, **overrides: object) -> str:
    args: dict[str, object] = {
        "task_digest": "sha256:" + "1" * 64,
        "image_digest": "sha256:" + "2" * 64,
        "agent": "claude-code@2.1",
        "seed": 0,
        "config_hash": "sha256:aa",
    }
    return episode_identity(task, **(args | overrides))  # type: ignore[arg-type]


def result(
    episode_id: str,
    *,
    status: Status = Status.COMPLETED,
    rewards: dict[str, float] | None = None,
) -> ResultRecord:
    started = datetime.now(UTC)
    return ResultRecord(
        episode_id=episode_id,
        status=status,
        rewards=rewards or ({"reward": 1.0} if status is Status.COMPLETED else None),
        failure=(
            None
            if status is Status.COMPLETED
            else FailureInfo(error_type="RuntimeError", message="no docker")
        ),
        started_at=started,
        finished_at=started + timedelta(milliseconds=1),
    )


class TestEpisodeIdentity:
    def test_the_same_work_has_the_same_identity(self) -> None:
        assert identity(spec()) == identity(spec())

    def test_a_different_task_is_different_work(self) -> None:
        assert identity(spec()) != identity(spec(instruction="write goodbye"))

    def test_a_whole_task_edit_is_different_work(self) -> None:
        assert identity(spec()) != identity(spec(), task_digest="sha256:" + "3" * 64)

    def test_a_different_prepared_image_is_different_work(self) -> None:
        assert identity(spec()) != identity(spec(), image_digest="sha256:" + "3" * 64)

    def test_a_different_variant_is_different_work(self) -> None:
        base = spec(variant="base", params={"n": 3})
        hard = spec(variant="hard", params={"n": 10})
        assert identity(base) != identity(hard)

    def test_a_different_agent_is_different_work(self) -> None:
        assert identity(spec()) != identity(spec(), agent="cua-gui@1")

    def test_a_different_seed_is_different_work(self) -> None:
        assert identity(spec()) != identity(spec(), seed=1)

    def test_a_different_configuration_is_different_work(self) -> None:
        assert identity(spec()) != identity(spec(), config_hash="sha256:bb")

    def test_different_effective_resources_are_different_work(self) -> None:
        assert identity(spec(), resources_digest="sha256:aa") != identity(
            spec(), resources_digest="sha256:bb"
        )


@pytest.mark.parametrize(("commit", "dirty"), [("a" * 40, False), ("a" * 40, True), (None, True)])
def test_consumed_asset_provenance_records_one_task_observation(
    commit: str | None, dirty: bool
) -> None:
    record = AssetProvenance(
        repository="ale-tasks-base",
        task_path="tasks/demo/external_assets",
        commit=commit,
        dirty=dirty,
    )
    assert record.repository == "ale-tasks-base"
    assert "owner/" not in record.repository


class TestLedger:
    def ledger(self, tmp_path: Path) -> Ledger:
        ledger = Ledger(tmp_path / "run")
        ledger.open_run("r1", "sha256:aa")
        return ledger

    def test_projects_live_and_terminal_state(self, tmp_path: Path) -> None:
        ledger = self.ledger(tmp_path)
        task = spec()
        ledger.queue_episode(episode_id="e1", run_id="r1", identity=identity(task), spec=task)
        assert ledger.episodes("r1")[0].status == "queued"

        ledger.mark_running("e1")
        ledger.update_phase("e1", "setup")
        running = ledger.episodes("r1")[0]
        assert running.status == "running"
        assert running.current_phase == "setup"
        assert running.started_at is not None

        ledger.finish_episode("e1", result("e1", rewards={"quality": 1.0, "format": 0.5}))
        finished = ledger.episodes("r1")[0]
        assert finished.status == "completed"
        assert finished.current_phase is None
        assert finished.rewards == {"format": 0.5, "quality": 1.0}
        assert finished.succeeded
        ledger.close()

    def test_a_failed_episode_does_not_count_as_done(self, tmp_path: Path) -> None:
        ledger = self.ledger(tmp_path)
        task = spec()
        key = identity(task)
        ledger.start_episode(episode_id="e1", run_id="r1", identity=key, spec=task)
        ledger.finish_episode("e1", result("e1", status=Status.ENV_ERROR))

        row = ledger.episodes("r1")[0]
        assert not row.succeeded
        assert row.failure_type == "RuntimeError"
        assert key not in ledger.completed_identities()
        ledger.close()

    def test_repeats_of_one_identity_are_counted_separately(self, tmp_path: Path) -> None:
        ledger = self.ledger(tmp_path)
        task = spec()
        key = identity(task)
        for index in range(3):
            episode_id = f"e{index}"
            ledger.start_episode(episode_id=episode_id, run_id="r1", identity=key, spec=task)
            ledger.finish_episode(episode_id, result(episode_id))

        assert len([row for row in ledger.episodes("r1") if row.succeeded]) == 3
        ledger.close()

    def test_schema_is_a_projection_not_an_episode_document(self, tmp_path: Path) -> None:
        ledger = self.ledger(tmp_path)
        mode = ledger._db.execute("PRAGMA journal_mode").fetchone()[0]
        columns = {row[1] for row in ledger._db.execute("PRAGMA table_info(episodes)").fetchall()}
        assert mode == "wal"
        assert {"episode_path", "current_phase", "rewards_json"} <= columns
        assert {"reward", "lock_json", "result_json", "trajectory_json"}.isdisjoint(columns)
        ledger.close()

    def test_reopen_reconciles_result_then_interrupts_stale_work(self, tmp_path: Path) -> None:
        root = tmp_path / "run"
        ledger = self.ledger(tmp_path)
        task = spec()
        ledger.start_episode(episode_id="finished", run_id="r1", identity="done", spec=task)
        ledger.start_episode(episode_id="stale", run_id="r1", identity="stale", spec=task)
        finished = result("finished", rewards={"reward": 1.0})
        path = root / "finished" / "result.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(finished.model_dump(mode="json"), sort_keys=True),
            encoding="utf-8",
        )
        ledger.close()

        reopened = Ledger(root)
        rows = {row.episode_id: row for row in reopened.episodes("r1")}
        assert rows["finished"].status == "completed"
        assert rows["finished"].rewards == {"reward": 1.0}
        assert rows["stale"].status == "interrupted"
        assert rows["stale"].failure_type == "HostInterrupted"
        reopened.close()

    def test_episodes_of_other_runs_are_not_returned(self, tmp_path: Path) -> None:
        ledger = self.ledger(tmp_path)
        ledger.open_run("r2", "sha256:aa")
        task = spec()
        ledger.start_episode(episode_id="e1", run_id="r1", identity=identity(task), spec=task)
        ledger.start_episode(episode_id="e2", run_id="r2", identity=identity(task), spec=task)
        assert [row.episode_id for row in ledger.episodes("r1")] == ["e1"]
        ledger.close()

    def test_migrates_scalar_reward_without_retaining_the_column(self, tmp_path: Path) -> None:
        root = tmp_path / "run"
        root.mkdir()
        db = sqlite3.connect(root / "ledger.db")
        db.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, created_at REAL NOT NULL, config_hash TEXT NOT NULL
            );
            CREATE TABLE episodes (
                episode_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, identity TEXT NOT NULL,
                task_id TEXT NOT NULL, variant TEXT, status TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, reward REAL, lock_json TEXT
            );
            INSERT INTO runs VALUES ('r1', 1, 'sha256:aa');
            INSERT INTO episodes VALUES (
                'e1', 'r1', 'identity', 'demo-hello', NULL, 'completed', 1, 2, 0.75, '{}'
            );
            """
        )
        db.commit()
        db.close()

        ledger = Ledger(root)
        assert ledger.episodes("r1")[0].rewards == {"reward": 0.75}
        columns = {row[1] for row in ledger._db.execute("PRAGMA table_info(episodes)").fetchall()}
        assert "reward" not in columns
        assert "lock_json" not in columns
        ledger.close()


class TestConcurrentEpisodes:
    def test_every_episode_is_recorded_exactly_once(self, tmp_path: Path) -> None:
        ledger = Ledger(tmp_path / "run")
        ledger.open_run("run1", "sha256:cfg")
        task = spec()

        async def record(index: int) -> None:
            episode_id = f"e{index}"
            ledger.start_episode(
                episode_id=episode_id,
                run_id="run1",
                identity=f"identity-{index}",
                spec=task,
            )
            await asyncio.sleep(0)
            ledger.finish_episode(episode_id, result(episode_id))

        async def record_all() -> None:
            await asyncio.gather(*(record(index) for index in range(4)))

        asyncio.run(record_all())
        rows = ledger.episodes("run1")
        ledger.close()

        assert len(rows) == 4
        assert len({row.episode_id for row in rows}) == 4
        assert all(row.succeeded for row in rows)
