from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.errors import AssetError
from ale.run.assets import asset_status, pull_assets

pytestmark = pytest.mark.integration


class FakeApi:
    def __init__(self) -> None:
        self.revision: object = None

    def get_collection(self, slug: str) -> object:
        return SimpleNamespace(
            slug=slug,
            title="assets",
        )

    def repo_info(self, *args: object, **kwargs: object) -> object:
        self.revision = kwargs.get("revision")
        return SimpleNamespace(sha="a" * 40, private=True)


def test_pull_replaces_only_selected_subtrees_and_records_commit(
    ale_checkout: Path,
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one", "two"))
    preserved = repository / "tasks/two/image/assets/local.txt"
    preserved.parent.mkdir(parents=True)
    preserved.write_text("keep")

    api = FakeApi()
    monkeypatch.setattr("huggingface_hub.HfApi", lambda: api)

    def snapshot_download(**kwargs: object) -> str:
        assert kwargs["revision"] == "a" * 40
        target = Path(str(kwargs["local_dir"])) / "tasks/one/image/assets"
        target.mkdir(parents=True)
        (target / "remote.txt").write_text("remote")
        return str(target)

    monkeypatch.setattr("huggingface_hub.snapshot_download", snapshot_download)
    (result,) = pull_assets(
        [repository / "tasks/one"],
        collection="agents-last-exam/assets-123",
        revision="a" * 40,
    )
    assert result.commit == "a" * 40
    assert api.revision == "a" * 40
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
        pull_assets([task], collection="agents-last-exam/assets-123", force=True)

    assert (task / "image/assets/original.txt").read_text() == "image"
    assert (task / "setup/assets/original.txt").read_text() == "setup"
    assert (task / "oracle/assets/original.txt").read_text() == "oracle"
    assert not (task / "image/assets/remote.txt").exists()


def test_pull_rejects_public_dataset(
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))

    class Public(FakeApi):
        def repo_info(self, *args: object, **kwargs: object) -> object:
            return SimpleNamespace(sha="a" * 40, private=False)

    monkeypatch.setattr("huggingface_hub.HfApi", Public)
    with pytest.raises(AssetError, match="must be private"):
        pull_assets([repository], collection="agents-last-exam/assets-123")
