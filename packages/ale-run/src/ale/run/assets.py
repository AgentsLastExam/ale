"""Materialising task data.

Data travels in two hops, and the split is the point (ADR 0007):

1. **fetch** — a component lands in the store, addressed by a key derived from its
   content coordinates rather than from any task's name. The store may be an image
   layer, the host cache, or a download, and a task renamed tomorrow still finds it.
2. **stage** — the store is projected into the fixed workspace an episode sees.

The projection is where visibility is enforced. A component the domain's asset lock
marks ``verify`` is staged into ``/ale/reference`` during scoring and is simply not
present while the agent runs — not hidden, absent.
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
from ale.core.taskspec import Workspace
from ale.run.sources import cache_root

__all__ = ["MaterialisedAsset", "fetch_component", "stage_components"]

#: Subdirectories of a component bundle, and where each is projected in the workspace.
#: `reference` is the one that must never appear before scoring.
_PROJECTION: dict[str, str] = {
    "input": Workspace.INPUT,
    "software": Workspace.SOFTWARE,
    "reference": Workspace.REFERENCE,
}


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

    source = f"{lock.repo.rstrip('/')}/{spec.path.lstrip('/')}"
    await _download(source, target)
    (target / ".complete").write_text(key, encoding="utf-8")
    return MaterialisedAsset(component, key, AssetOrigin.DOWNLOAD, target)


async def _download(source: str, target: Path) -> None:
    """Copy a bundle out of object storage.

    ``gsutil`` is used rather than a client library: it is already installed wherever
    these buckets are used, it handles auth the way the rest of the team's tooling does,
    and it keeps this module free of a cloud SDK dependency.
    """
    if not source.startswith("gs://"):
        raise AssetError(f"unsupported asset source {source!r}; only gs:// is implemented")

    staging = target.with_suffix(".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        "gsutil",
        "-q",
        "-m",
        "cp",
        "-r",
        f"{source.rstrip('/')}/*",
        str(staging),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise AssetError(f"could not fetch {source}: {stderr.decode('utf-8', 'replace').strip()}")

    # Rename last, so an interrupted download never looks complete.
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)


async def stage_components(
    sandbox: Sandbox,
    lock: AssetsLock,
    components: tuple[str, ...],
    *,
    stage: Visibility,
) -> list[MaterialisedAsset]:
    """Project components into the workspace for one stage.

    Raises:
        AssetError: if a component's declared visibility does not permit this stage.
            The check is here as well as in the loader because this is the last place
            it can be caught before answers would reach a sandbox.
    """
    materialised: list[MaterialisedAsset] = []
    for name in components:
        spec = lock.component(name)
        if spec.visibility == "verify" and stage != "verify":
            raise AssetError(f"asset {name!r} is verify-only and cannot be staged for the agent")

        asset = await fetch_component(lock, name)
        for subdir, destination in _PROJECTION.items():
            source = asset.path / subdir
            if not source.is_dir():
                continue
            if subdir == "reference" and stage != "verify":
                continue  # gold data waits for scoring
            await sandbox.exec(["mkdir", "-p", destination])
            await sandbox.upload_dir(str(source), destination)
        materialised.append(asset)
    return materialised
