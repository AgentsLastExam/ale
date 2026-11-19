from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from huggingface_hub.errors import HfHubHTTPError

from ale.core.errors import AssetError
from ale.run.assets import asset_status, pull_assets

pytestmark = pytest.mark.integration


class FakeApi:
    def __init__(self, *, token: object = None) -> None:
        self.token = token
        self.revision: object = None

    def repo_info(self, *args: object, **kwargs: object) -> object:
        self.revision = kwargs.get("revision")
        return SimpleNamespace(sha="a" * 40)


def test_pull_replaces_only_selected_subtrees_and_records_commit(
    ale_checkout: Path,
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one", "two"))
    preserved = repository / "tasks/two/environment/assets/local.txt"
    preserved.parent.mkdir(parents=True)
    preserved.write_text("keep")

    api = FakeApi()
    tokens: list[object] = []
    monkeypatch.setenv("ALE_HF_TOKEN", "hf-checkout-token")
    monkeypatch.setattr("huggingface_hub.HfApi", lambda *, token: tokens.append(token) or api)

    def snapshot_download(**kwargs: object) -> str:
        assert kwargs["revision"] == "a" * 40
        assert kwargs["token"] == "hf-checkout-token"
        target = Path(str(kwargs["local_dir"])) / "tasks/one/environment/assets"
        target.mkdir(parents=True)
        (target / "remote.txt").write_text("remote")
        return str(target)

    monkeypatch.setattr("huggingface_hub.snapshot_download", snapshot_download)
    (result,) = pull_assets([repository / "tasks/one"], revision="a" * 40)
    assert result.commit == "a" * 40
    assert tokens == ["hf-checkout-token"]
    assert api.revision == "a" * 40
    assert (repository / "tasks/one/environment/assets/remote.txt").read_text() == "remote"
    assert preserved.read_text() == "keep"
    assert all(not item.dirty for item in asset_status([repository / "tasks/one"]))

    task = repository / "tasks/one"
    remote = task / "environment/assets/remote.txt"
    with pytest.raises(AssetError, match=r"already exist.*--force"):
        pull_assets([task])
    remote.write_text("local edit")
    with pytest.raises(AssetError, match=r"already exist.*--force"):
        pull_assets([task])
    assert remote.read_text() == "local edit"
    pull_assets([task], force=True)
    assert remote.read_text() == "remote"
    assert preserved.read_text() == "keep"


def test_pull_rejects_empty_existing_assets_before_contacting_hf(
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))
    (repository / "tasks/one/environment/assets").mkdir()

    def forbidden(**kwargs: object) -> None:
        pytest.fail("existing assets must be rejected before contacting HF")

    monkeypatch.setattr("huggingface_hub.HfApi", forbidden)
    with pytest.raises(AssetError, match=r"already exist.*--force"):
        pull_assets([repository])


def test_pull_preserves_assets_created_during_download(
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))
    local = repository / "tasks/one/environment/assets/local.txt"
    monkeypatch.setattr("huggingface_hub.HfApi", FakeApi)

    def download(**kwargs: object) -> str:
        local.parent.mkdir()
        local.write_text("concurrent edit")
        return str(kwargs["local_dir"])

    monkeypatch.setattr("huggingface_hub.snapshot_download", download)
    with pytest.raises(AssetError, match=r"already exist.*--force"):
        pull_assets([repository])
    assert local.read_text() == "concurrent edit"


def test_pull_rolls_back_every_selected_root_when_replacement_fails(
    ale_checkout: Path,
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))
    task = repository / "tasks/one"
    roots = ("environment/assets", "tests/assets", "solution/assets")
    for relative in roots:
        root = task / relative
        root.mkdir(parents=True)
        (root / "original.txt").write_text(relative)

    monkeypatch.setattr("huggingface_hub.HfApi", FakeApi)

    def snapshot_download(**kwargs: object) -> str:
        downloaded = Path(str(kwargs["local_dir"]))
        for relative in roots:
            root = downloaded / f"tasks/one/{relative}"
            root.mkdir(parents=True)
            (root / "remote.txt").write_text(relative)
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
        pull_assets([task], force=True)

    for relative in roots:
        assert (task / relative / "original.txt").read_text() == relative
    assert not (task / "environment/assets/remote.txt").exists()


def test_pull_reads_public_dataset_anonymously_without_collection(
    ale_checkout: Path,
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))
    monkeypatch.delenv("ALE_HF_TOKEN", raising=False)
    tokens: list[object] = []

    class Public(FakeApi):
        def repo_info(self, *args: object, **kwargs: object) -> object:
            tokens.append(self.token)
            return SimpleNamespace(sha="a" * 40, private=False)

    def snapshot_download(**kwargs: object) -> str:
        tokens.append(kwargs["token"])
        target = Path(str(kwargs["local_dir"])) / "tasks/one/environment/assets"
        target.mkdir(parents=True)
        (target / "remote.txt").write_text("remote")
        return str(target)

    monkeypatch.setattr("huggingface_hub.HfApi", Public)
    monkeypatch.setattr("huggingface_hub.snapshot_download", snapshot_download)
    (result,) = pull_assets([repository])
    assert result.commit == "a" * 40
    assert tokens == [False, False]
    assert (repository / "tasks/one/environment/assets/remote.txt").read_text() == "remote"


def test_pull_reports_an_unreadable_dataset(
    write_task_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))

    class Unreadable(FakeApi):
        def repo_info(self, *args: object, **kwargs: object) -> object:
            request = httpx.Request("GET", "https://huggingface.co/api/datasets/demo")
            response = httpx.Response(401, request=request)
            raise HfHubHTTPError("401 Unauthorized", response=response)

    monkeypatch.setattr("huggingface_hub.HfApi", Unreadable)
    with pytest.raises(AssetError, match="could not read dataset"):
        pull_assets([repository])
