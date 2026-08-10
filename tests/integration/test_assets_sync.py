from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.errors import AssetError
from ale.run.assets import asset_status, pull_assets, push_assets

pytestmark = pytest.mark.integration


class FakeApi:
    def __init__(self) -> None:
        self.operations: list[object] = []
        self.parent_commit: str | None = None

    def get_collection(self, slug: str) -> object:
        return SimpleNamespace(
            slug=slug,
            items=(SimpleNamespace(item_type="dataset", item_id="publisher/ale-tasks-demo"),),
        )

    def repo_info(self, *args: object, **kwargs: object) -> object:
        return SimpleNamespace(sha="a" * 40)

    def whoami(self) -> dict[str, str]:
        return {"name": "publisher"}

    def create_repo(self, *args: object, **kwargs: object) -> None:
        return None

    def add_collection_item(self, *args: object, **kwargs: object) -> None:
        return None

    def list_repo_files(self, *args: object, **kwargs: object) -> list[str]:
        return [
            "tasks/one/image/assets/stale.txt",
            "tasks/two/image/assets/untouched.txt",
        ]

    def create_commit(self, repo_id: str, operations, **kwargs: object) -> object:  # type: ignore[no-untyped-def]
        self.operations = list(operations)
        self.parent_commit = kwargs.get("parent_commit")  # type: ignore[assignment]
        return SimpleNamespace(oid="b" * 40)


def test_pull_replaces_only_selected_subtrees_and_records_commit(
    ale_checkout: Path,
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one", "two"))
    preserved = repository / "tasks/two/image/assets/local.txt"
    preserved.parent.mkdir(parents=True)
    preserved.write_text("keep")

    monkeypatch.setattr("huggingface_hub.HfApi", FakeApi)

    def snapshot_download(**kwargs: object) -> str:
        target = Path(str(kwargs["local_dir"])) / "tasks/one/image/assets"
        target.mkdir(parents=True)
        (target / "remote.txt").write_text("remote")
        return str(target)

    monkeypatch.setattr("huggingface_hub.snapshot_download", snapshot_download)
    (result,) = pull_assets(
        [repository / "tasks/one"],
        collection="publisher/assets-123",
    )
    assert result.commit == "a" * 40
    assert (repository / "tasks/one/image/assets/remote.txt").read_text() == "remote"
    assert preserved.read_text() == "keep"
    assert all(not item.dirty for item in asset_status([repository / "tasks/one"]))


def test_pull_rolls_back_every_selected_root_when_replacement_fails(
    ale_checkout: Path,
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))
    task = repository / "tasks/one"
    for stage in ("image", "setup", "oracle"):
        root = task / stage / "assets"
        root.mkdir(parents=True)
        (root / "original.txt").write_text(stage)

    monkeypatch.setattr("huggingface_hub.HfApi", FakeApi)

    def snapshot_download(**kwargs: object) -> str:
        downloaded = Path(str(kwargs["local_dir"]))
        for stage in ("image", "setup", "oracle"):
            root = downloaded / f"tasks/one/{stage}/assets"
            root.mkdir(parents=True)
            (root / "remote.txt").write_text(stage)
        return str(downloaded)

    monkeypatch.setattr("huggingface_hub.snapshot_download", snapshot_download)
    original_copytree = shutil.copytree
    calls = 0

    def fail_second_copy(source: Path, destination: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replacement failure")
        return str(original_copytree(source, destination))

    monkeypatch.setattr("ale.run.assets.shutil.copytree", fail_second_copy)

    with pytest.raises(AssetError, match="simulated replacement failure"):
        pull_assets([task], collection="publisher/assets-123", force=True)

    assert (task / "image/assets/original.txt").read_text() == "image"
    assert (task / "setup/assets/original.txt").read_text() == "setup"
    assert (task / "oracle/assets/original.txt").read_text() == "oracle"
    assert not (task / "image/assets/remote.txt").exists()


def test_push_adds_local_files_deletes_stale_remote_files_and_guards_parent(
    ale_checkout: Path,
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one", "two"))
    local = repository / "tasks/one/image/assets/current.txt"
    local.parent.mkdir(parents=True)
    local.write_text("current")
    api = FakeApi()
    monkeypatch.setattr("huggingface_hub.HfApi", lambda: api)
    (result,) = push_assets(
        [repository / "tasks/one"],
        collection="publisher/assets-123",
    )
    additions = {getattr(item, "path_in_repo", "") for item in api.operations}
    assert "tasks/one/image/assets/current.txt" in additions
    assert "tasks/one/image/assets/stale.txt" in additions
    assert "tasks/two/image/assets/untouched.txt" not in additions
    assert api.parent_commit == "a" * 40
    assert result.commit == "b" * 40


def test_pull_rejects_missing_or_duplicate_same_named_dataset(
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))

    class Missing(FakeApi):
        def get_collection(self, slug: str) -> object:
            return SimpleNamespace(slug=slug, items=())

    monkeypatch.setattr("huggingface_hub.HfApi", Missing)
    with pytest.raises(AssetError, match="exactly one"):
        pull_assets([repository], collection="publisher/assets-123")
