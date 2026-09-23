import os
from pathlib import Path

import pytest

from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "relative",
    [
        "instruction.md",
        "task.yaml",
        "image/Dockerfile",
        "image/assets/input",
        "setup/assets/input",
        "oracle/run.sh",
        "other.txt",
    ],
)
def test_non_verifier_change_requires_new_solver(tmp_path: Path, relative: str):
    root = scaffold_task(tmp_path / "task")
    before = load_tasks(root)[0].non_verifier_digest
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(target.read_text() + "\n# changed\n" if target.exists() else "new")
    assert load_tasks(root)[0].non_verifier_digest != before


@pytest.mark.parametrize("relative", ["verify/run.sh", "verify/assets/input"])
def test_only_verifier_changes_preserve_solver_identity(tmp_path: Path, relative: str):
    root = scaffold_task(tmp_path / "task")
    before = load_tasks(root)[0].non_verifier_digest
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("new verifier material")
    assert load_tasks(root)[0].non_verifier_digest == before


def test_timestamps_do_not_change_content_identity(tmp_path: Path):
    root = scaffold_task(tmp_path / "task")
    before = load_tasks(root)[0].non_verifier_digest
    os.utime(root / "image/Dockerfile", (1, 1))
    assert load_tasks(root)[0].non_verifier_digest == before
    (root / "instruction.md").chmod(0o755)
    assert load_tasks(root)[0].non_verifier_digest != before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["outside", "missing_record"])
async def test_reverify_refuses_changed_or_unrecorded_inputs_before_attaching(
    tmp_path, monkeypatch, change
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tests.unit.test_runtime_contracts import make_lock

    from ale.core.config import RunConfig
    from ale.run.cli import tasks as task_cli

    root = scaffold_task(tmp_path / "task")
    task = load_tasks(root)[0]
    lock = make_lock()
    lock = lock.model_copy(
        update={
            "task": lock.task.model_copy(
                update={
                    "name": task.spec.name,
                    "variant": task.spec.variant,
                    "non_verifier_digest": task.non_verifier_digest
                    if change == "outside"
                    else None,
                }
            )
        }
    )
    if change == "outside":
        instruction = root / "instruction.md"
        instruction.write_text(instruction.read_text() + "\nDifferent solver task")
    monkeypatch.setattr(
        task_cli,
        "_retained_source_episodes",
        lambda _: ((tmp_path / "source", SimpleNamespace(episode_id="old"), lock, "docker:old"),),
    )
    attach = AsyncMock(side_effect=AssertionError("must reject before attachment"))
    monkeypatch.setattr(task_cli, "_attach_retained", attach)
    code = await task_cli._reverify(str(root), tmp_path / "source", RunConfig(), tmp_path / "runs")
    assert code == 2
    attach.assert_not_called()
