"""Fetching and staging task data.

Two hops, and the split is what makes pre-baking possible later (ADR 0007):

1. **fetch** — a directory of published data lands in the host store under a key derived
   from where it came from, never from any task's name. A renamed task still finds it,
   and two tasks naming the same data share one copy.
2. **stage** — it is copied into a sandbox at the path the task asked for.

Which stage lists a mount decides when it appears. Gold answers listed under ``verify``
are copied in during scoring only, so while the agent works they are not hidden — they
are absent.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from ale.core.errors import AssetError
from ale.core.sandbox import Identity, Sandbox
from ale.core.store import AssetOrigin, data_key
from ale.core.taskspec import AssetMount
from ale.run.sources import cache_root

__all__ = ["MaterialisedAsset", "fetch_mount", "stage_mounts"]


@dataclass(frozen=True)
class MaterialisedAsset:
    """One mount, and how it got here — both go into provenance."""

    mount: AssetMount
    key: str
    origin: AssetOrigin
    path: Path


def _store_dir() -> Path:
    return cache_root() / "store"


async def fetch_mount(mount: AssetMount) -> MaterialisedAsset:
    """Bring a mount's data into the host store, if it is not already there."""
    key = data_key(mount.repo, mount.revision, mount.path)
    target = _store_dir() / key

    if (target / ".complete").is_file():
        return MaterialisedAsset(mount, key, AssetOrigin.CACHE, target)

    await _download(mount, target)
    (target / ".complete").write_text(key, encoding="utf-8")
    return MaterialisedAsset(mount, key, AssetOrigin.DOWNLOAD, target)


async def _download(mount: AssetMount, target: Path) -> None:
    """Fetch one directory of a dataset at a pinned commit."""
    subpath = mount.path.strip("/")
    staging = target.with_suffix(".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    def _snapshot() -> str:
        from huggingface_hub import snapshot_download

        return snapshot_download(
            repo_id=mount.repo,
            repo_type="dataset",
            revision=mount.revision or None,
            allow_patterns=[f"{subpath}/**"],
            local_dir=str(staging),
        )

    try:
        await asyncio.to_thread(_snapshot)
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise AssetError(f"could not fetch {mount.repo}:{subpath}: {exc}") from exc

    # The snapshot mirrors the repository layout; keep only the directory asked for, and
    # rename last so an interrupted download never looks complete.
    fetched = staging / subpath
    if not fetched.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
        raise AssetError(f"{subpath} is not present in {mount.repo}@{mount.revision}")
    shutil.rmtree(target, ignore_errors=True)
    fetched.rename(target)
    shutil.rmtree(staging, ignore_errors=True)


async def stage_mounts(sandbox: Sandbox, mounts: tuple[AssetMount, ...]) -> list[MaterialisedAsset]:
    """Copy each mount into the sandbox at the destination the task asked for."""
    materialised: list[MaterialisedAsset] = []
    for mount in mounts:
        asset = await fetch_mount(mount)
        await sandbox.exec(["mkdir", "-p", mount.dest])
        # Staged as the agent: a task's data is the agent's to read and often to change,
        # and ownership set on arrival is one less thing anyone has to remember.
        await sandbox.upload_dir(str(asset.path), mount.dest, identity=Identity.AGENT)
        materialised.append(asset)
    return materialised
