from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ale.run.assets import (
    _mark_clean,
    asset_status,
    observe_task_assets,
    select_asset_repositories,
)
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit


def test_recursive_selection_deduplicates_overlapping_roots(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one", "two"))
    selections = select_asset_repositories(
        [repository / "tasks/one", repository / "tasks", repository]
    )
    assert len(selections) == 1
    assert [task.source.task_relative_path for task in selections[0].tasks] == [  # type: ignore[attr-defined]
        "tasks/one",
        "tasks/two",
    ]


def test_task_local_assets_have_one_commit_and_dirty_observation(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))
    task_path = repository / "tasks/one"
    asset = task_path / "image/assets/data.txt"
    asset.parent.mkdir()
    asset.write_text("one")
    task = load_tasks(task_path)[0]

    initial = observe_task_assets(task)
    assert initial is not None and initial.commit is None and initial.dirty

    _mark_clean(select_asset_repositories([task_path])[0], "a" * 40)
    clean = asset_status([task_path])[0]
    assert clean.commit == "a" * 40 and not clean.dirty

    (task_path / "instruction.md").write_text("Source-only change.\n")
    assert not asset_status([task_path])[0].dirty

    asset.write_text("changed")
    assert asset_status([task_path])[0].dirty


def test_removing_a_clean_asset_root_is_dirty_until_synchronized(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))
    task_path = repository / "tasks/one"
    asset_root = task_path / "verify/assets"
    asset_root.mkdir()
    (asset_root / "expected.json").write_text("{}")
    selection = select_asset_repositories([task_path])[0]
    _mark_clean(selection, "a" * 40)

    shutil.rmtree(asset_root)
    removed = asset_status([task_path])
    assert len(removed) == 1 and removed[0].dirty

    _mark_clean(selection, "b" * 40)
    assert asset_status([task_path]) == ()


def test_old_marker_without_inventory_is_dirty(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))
    task_path = repository / "tasks/one"
    asset = task_path / "image/assets/data.txt"
    asset.parent.mkdir()
    asset.write_text("one")
    state = repository / ".ale-cache/assets.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": repository.name,
                "tasks": {
                    "tasks/one": {
                        "commit": "a" * 40,
                        "clean_marker_ns": 2**63,
                        "dirty": False,
                    }
                },
            }
        )
    )

    observation = asset_status([task_path])[0]
    assert observation.commit == "a" * 40 and observation.dirty


def test_status_does_not_read_asset_bytes(write_task_repo, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))
    asset = repository / "tasks/one/oracle/assets/reference.bin"
    asset.parent.mkdir()
    asset.write_bytes(b"large")

    original = Path.read_bytes

    def forbidden(path: Path) -> bytes:
        if "assets" in path.parts:
            raise AssertionError("asset bytes were hashed")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert asset_status([repository / "tasks/one"])[0].dirty


def test_no_assets_task_has_no_public_asset_record(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))
    assert asset_status([repository / "tasks/one"]) == ()
