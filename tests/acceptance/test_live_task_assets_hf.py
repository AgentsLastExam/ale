from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ale.run.assets import (
    asset_status,
    pull_assets,
    push_assets,
    select_asset_repositories,
)


def _task_path() -> Path:
    configured = os.environ.get("ALE_LIVE_HF_TASK")
    if not configured:
        pytest.skip("set ALE_LIVE_HF_TASK to opt into the live HF asset round trip")
    return Path(configured).resolve()


def _clean_copy(task: Path, tmp_path: Path) -> Path:
    (selection,) = select_asset_repositories([task])
    relative = task.relative_to(selection.repository_root)
    clone = tmp_path / selection.repository_name
    shutil.copytree(selection.repository_root, clone, symlinks=True)
    shutil.rmtree(clone / ".ale-cache", ignore_errors=True)
    copied = clone / relative
    for stage in ("image", "setup", "verify", "oracle"):
        shutil.rmtree(copied / stage / "assets", ignore_errors=True)
    return copied


@pytest.mark.needs_hf_write
def test_authenticated_push_and_clean_public_pull_round_trip(tmp_path: Path) -> None:
    task = _task_path()
    collection = os.environ.get("ALE_ASSETS_COLLECTION")
    if not collection:
        pytest.skip("set ALE_ASSETS_COLLECTION to the public assets collection slug")
    pushed = push_assets([task], collection=collection)
    assert pushed and pushed[0].commit

    clean_task = _clean_copy(task, tmp_path)
    pulled = pull_assets([clean_task], collection=collection)
    assert pulled[0].commit == pushed[0].commit
    assert all(not item.dirty for item in asset_status([clean_task]))
    assert (clean_task / "image/assets/public.txt").is_file()
    assert (clean_task / "setup/assets/service-response.txt").is_file()
    assert (clean_task / "verify/assets/expected.json").is_file()
    assert (clean_task / "oracle/assets/result.json").is_file()


@pytest.mark.needs_hf_public
def test_public_collection_pull_needs_no_write_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task_path()
    collection = os.environ.get("ALE_ASSETS_COLLECTION")
    if not collection:
        pytest.skip("set ALE_ASSETS_COLLECTION to the public assets collection slug")
    clean_task = _clean_copy(task, tmp_path)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    from huggingface_hub import HfApi, snapshot_download

    monkeypatch.setattr("huggingface_hub.HfApi", lambda: HfApi(token=False))
    monkeypatch.setattr(
        "huggingface_hub.snapshot_download",
        lambda **kwargs: snapshot_download(token=False, **kwargs),
    )
    pulled = pull_assets([clean_task], collection=collection)
    assert pulled and pulled[0].commit
