from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ale.run.assets import (
    ASSET_ROOTS,
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
    shutil.copytree(
        selection.repository_root,
        clone,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", ".ale-cache", "assets"),
    )
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    copied = clone / relative
    for root in ASSET_ROOTS:
        shutil.rmtree(copied / root, ignore_errors=True)
    return copied


@pytest.mark.needs_hf_write
def test_authenticated_push_and_pinned_private_pull_round_trip(tmp_path: Path) -> None:
    task = _task_path()
    collection = os.environ.get("ALE_ASSETS_COLLECTION")
    pushed = push_assets([task], collection=collection)
    assert pushed and pushed[0].commit

    clean_task = _clean_copy(task, tmp_path)
    pulled = pull_assets([clean_task], collection=collection, revision=pushed[0].commit)
    assert pulled[0].commit == pushed[0].commit
    assert all(not item.dirty for item in asset_status([clean_task]))
    assert (clean_task / "environment/assets/public.txt").is_file()
    assert (clean_task / "environment/assets/service-response.txt").is_file()
    assert (clean_task / "tests/assets/expected.json").is_file()
    assert (clean_task / "solution/assets/result.json").is_file()


@pytest.mark.needs_hf_write
def test_private_dataset_requires_a_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task_path()
    collection = os.environ.get("ALE_ASSETS_COLLECTION")
    clean_task = _clean_copy(task, tmp_path)
    # Without ALE_HF_TOKEN every Hub call is anonymous; no other login stands in for it.
    monkeypatch.delenv("ALE_HF_TOKEN", raising=False)
    from huggingface_hub.errors import HfHubHTTPError

    with pytest.raises(HfHubHTTPError):
        pull_assets([clean_task], collection=collection)
