"""Materialising task data.

Data travels in two hops, and the split is the point (ADR 0007):

1. **fetch** — a component lands in the host store, addressed by a key derived from
   where the data came from rather than from any task's name, so a renamed task still
   finds it and two tasks sharing a component share one copy.
2. **stage** — the component is copied into a sandbox, at the path the task asked for.

Staging is where visibility is enforced. A component the domain's asset lock marks
``verify`` is copied in only during scoring, so while the agent runs it is not merely
hidden — it is absent.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from ale.core.domain import AssetsLock, Visibility
from ale.core.errors import AssetError
from ale.core.sandbox import Sandbox
from ale.core.store import AssetOrigin, data_key
from ale.core.taskspec import AssetMount
from ale.run.sources import cache_root

__all__ = ["MaterialisedAsset", "fetch_component", "stage_components"]


@dataclass(frozen=True)
class MaterialisedAsset:
    """One component, and how it got here — both go into provenance."""

    component: str
    key: str
    origin: AssetOrigin
    path: Path


def _store_dir() -> Path:
    return cache_root() / "store"


async def fetch_component(lock: AssetsLock, component: str) -> MaterialisedAsset:
    """Bring a component into the host store, if it is not already there.

    Cached by data key, so two tasks sharing a bundle download it once and a re-run
    downloads nothing.
    """
    spec = lock.component(component)
    key = data_key(lock.repo, lock.revision, component)
    target = _store_dir() / key

    if (target / ".complete").is_file():
        return MaterialisedAsset(component, key, AssetOrigin.CACHE, target)

    await _download(lock, spec.path.strip("/"), target)
    (target / ".complete").write_text(key, encoding="utf-8")
    return MaterialisedAsset(component, key, AssetOrigin.DOWNLOAD, target)


async def _download(lock: AssetsLock, subpath: str, target: Path) -> None:
    """Fetch one subdirectory of the assets dataset.

    The revision is a commit, and dataset commits are immutable — which is what makes a
    recorded data version something a later run can actually reproduce, rather than a
    label on a path whose contents may have changed underneath it.
    """
    staging = target.with_suffix(".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    def _snapshot() -> str:
        from huggingface_hub import snapshot_download

        return snapshot_download(
            repo_id=lock.repo,
            repo_type="dataset",
            revision=lock.revision or None,
            allow_patterns=[f"{subpath}/**"],
            local_dir=str(staging),
        )

    try:
        await asyncio.to_thread(_snapshot)
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise AssetError(f"could not fetch {lock.repo}:{subpath}: {exc}") from exc

    # The snapshot mirrors the repository layout; keep only the part asked for, and
    # rename last so an interrupted download never looks complete.
    fetched = staging / subpath
    if not fetched.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
        raise AssetError(f"{subpath} is not present in {lock.repo}@{lock.revision}")
    shutil.rmtree(target, ignore_errors=True)
    fetched.rename(target)
    shutil.rmtree(staging, ignore_errors=True)


async def stage_components(
    sandbox: Sandbox,
    lock: AssetsLock,
    mounts: tuple[AssetMount, ...],
    *,
    stage: Visibility,
) -> list[MaterialisedAsset]:
    """Copy components into the sandbox at the destinations the task asked for.

    Raises:
        AssetError: if a component's declared visibility does not permit this stage.
            The check is repeated here because this is the last point before the data
            would actually land in a sandbox.
    """
    materialised: list[MaterialisedAsset] = []
    for mount in mounts:
        spec = lock.component(mount.component)
        if spec.visibility == "verify" and stage != "verify":
            raise AssetError(
                f"asset {mount.component!r} is verify-only and cannot be staged for the agent"
            )

        asset = await fetch_component(lock, mount.component)
        await sandbox.exec(["mkdir", "-p", mount.dest])
        await sandbox.upload_dir(str(asset.path), mount.dest)
        materialised.append(asset)
    return materialised
