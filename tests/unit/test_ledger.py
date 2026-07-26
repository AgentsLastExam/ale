"""What makes two episodes the same piece of work.

Resume rests entirely on this: get identity wrong in one direction and completed work is
repeated, wrong in the other and different work is silently skipped. The second is worse,
because it looks like success.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale.core.ids import TaskId
from ale.core.taskspec import ImageRef, TaskSpec
from ale.core.verdict import Status, Verdict
from ale.run.ledger import Ledger, episode_identity

pytestmark = pytest.mark.unit


def spec(**overrides: object) -> TaskSpec:
    base: dict[str, object] = {
        "id": TaskId("demo-hello"),
        "domain": "demo",
        "instruction": "write hello",
        "image": ImageRef(name="sandbox-base-cli"),
    }
    return TaskSpec(**(base | overrides))  # type: ignore[arg-type]


def identity(task: TaskSpec, **overrides: object) -> str:
    args: dict[str, object] = {"agent": "claude-code@2.1", "seed": 0, "config_hash": "sha256:aa"}
    return episode_identity(task, **(args | overrides))  # type: ignore[arg-type]


class TestEpisodeIdentity:
    def test_the_same_work_has_the_same_identity(self) -> None:
        assert identity(spec()) == identity(spec())

    def test_a_different_task_is_different_work(self) -> None:
        assert identity(spec()) != identity(spec(instruction="write goodbye"))

    def test_a_different_variant_is_different_work(self) -> None:
        """Variants share an id and must never be mistaken for one another."""
        base = spec(variant="base", params={"n": 3})
        hard = spec(variant="hard", params={"n": 10})
        assert identity(base) != identity(hard)

    def test_a_different_agent_is_different_work(self) -> None:
        assert identity(spec()) != identity(spec(), agent="cua-gui@1")

    def test_a_different_seed_is_different_work(self) -> None:
        """Otherwise asking for five samples would be answered by one."""
        assert identity(spec()) != identity(spec(), seed=1)

    def test_a_different_configuration_is_different_work(self) -> None:
        assert identity(spec()) != identity(spec(), config_hash="sha256:bb")


class TestLedger:
    def ledger(self, tmp_path: Path) -> Ledger:
        ledger = Ledger(tmp_path / "run")
        ledger.open_run("r1", "sha256:aa")
        return ledger

    def test_a_finished_episode_is_recorded_with_its_reward(self, tmp_path: Path) -> None:
        ledger = self.ledger(tmp_path)
        task = spec()
        ledger.start_episode(episode_id="e1", run_id="r1", identity=identity(task), spec=task)
        ledger.finish_episode("e1", Verdict.completed({"reward": 1.0}))

        (row,) = ledger.episodes("r1")
        assert (row.status, row.reward, row.succeeded) == ("completed", 1.0, True)
        ledger.close()

    def test_a_failed_episode_does_not_count_as_done(self, tmp_path: Path) -> None:
        """Resume must re-run it; treating a failure as complete loses the work."""
        ledger = self.ledger(tmp_path)
        task = spec()
        ledger.start_episode(episode_id="e1", run_id="r1", identity=identity(task), spec=task)
        ledger.finish_episode("e1", Verdict.failed(Status.ENV_ERROR, RuntimeError("no docker")))

        (row,) = ledger.episodes("r1")
        assert not row.succeeded
        assert identity(task) not in ledger.completed_identities()
        ledger.close()

    def test_an_unfinished_episode_does_not_count_as_done(self, tmp_path: Path) -> None:
        """A killed process leaves a started row; that work still has to be redone."""
        ledger = self.ledger(tmp_path)
        task = spec()
        ledger.start_episode(episode_id="e1", run_id="r1", identity=identity(task), spec=task)

        assert not ledger.episodes("r1")[0].succeeded
        ledger.close()

    def test_repeats_of_one_identity_are_counted_separately(self, tmp_path: Path) -> None:
        """Asking for three samples of one task is three rows, not one."""
        ledger = self.ledger(tmp_path)
        task = spec()
        key = identity(task)
        for index in range(3):
            ledger.start_episode(episode_id=f"e{index}", run_id="r1", identity=key, spec=task)
            ledger.finish_episode(f"e{index}", Verdict.completed({"reward": 1.0}))

        assert len([row for row in ledger.episodes("r1") if row.succeeded]) == 3
        ledger.close()

    def test_the_lock_is_stored_with_the_episode(self, tmp_path: Path) -> None:
        ledger = self.ledger(tmp_path)
        task = spec()
        ledger.start_episode(episode_id="e1", run_id="r1", identity=identity(task), spec=task)
        ledger.finish_episode("e1", Verdict.completed({"reward": 1.0}), {"seed": 7})
        ledger.close()

        # Reopening proves it survived the process, which is the whole point of a ledger.
        reopened = Ledger(tmp_path / "run")
        assert reopened.episodes("r1")[0].episode_id == "e1"
        reopened.close()

    def test_events_are_appended_and_flushed(self, tmp_path: Path) -> None:
        """A killed process still explains how far it got."""
        ledger = self.ledger(tmp_path)
        ledger.event("e1", "provisioned", image="sandbox-base-cli")
        ledger.event("e1", "finished", status="completed")

        lines = (tmp_path / "run" / "e1" / "events.jsonl").read_text().splitlines()
        assert [json.loads(line)["kind"] for line in lines] == ["provisioned", "finished"]
        ledger.close()

    def test_episodes_of_other_runs_are_not_returned(self, tmp_path: Path) -> None:
        ledger = self.ledger(tmp_path)
        ledger.open_run("r2", "sha256:aa")
        task = spec()
        ledger.start_episode(episode_id="e1", run_id="r1", identity=identity(task), spec=task)
        ledger.start_episode(episode_id="e2", run_id="r2", identity=identity(task), spec=task)

        assert [row.episode_id for row in ledger.episodes("r1")] == ["e1"]
        ledger.close()
