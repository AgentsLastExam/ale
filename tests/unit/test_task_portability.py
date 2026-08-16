from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ale.run.scaffold import scaffold_task
from ale.run.tasksets import load_tasks

pytestmark = pytest.mark.unit


def test_task_does_not_depend_on_siblings_or_parent_manifest(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    task = scaffold_task(collection / "tasks" / "demo")
    sibling = scaffold_task(collection / "tasks" / "sibling")
    (collection / "domain.yaml").write_text("name: ignored\n")
    before = load_tasks(task)[0]
    shutil.rmtree(sibling)
    moved = tmp_path / "standalone"
    shutil.copytree(task, moved)
    after = load_tasks(moved)[0]
    assert before.spec == after.spec
    assert before.task_digest == after.task_digest
    assert after.source.repository_name is None
    assert after.source.task_relative_path is None
