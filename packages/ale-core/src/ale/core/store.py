"""Data store: where task data lives before it becomes a workspace.

Two layers, deliberately separate:

* the **store** (``/ale/store/<data_key>/...``) can hold many tasks' data at once, is
  addressed by a content-derived key, and may be baked into an image or cached on the
  host;
* the **workspace** (``/ale/input``, ``/ale/output``, ...) is fixed, per-episode, and
  the only thing an agent or a task script ever sees.

Collapsing them would cost a real optimisation: if the only location were the fixed
workspace, one image could hold exactly one task's data, and pre-baking many tasks into
a shared image — the thing that removes per-episode download cost — becomes impossible.

Keying by content rather than by task identifier is what keeps a baked image valid
across renames: moving a task to another group changes its identifier and not one byte
of its data key.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from ale.core.ids import content_hash

__all__ = ["STORE_ROOT", "AssetOrigin", "StoreEntry", "StoreManifest", "data_key"]

STORE_ROOT = PurePosixPath("/ale/store")

MANIFEST_NAME = "manifest.json"


class AssetOrigin(StrEnum):
    """Where a materialised component actually came from.

    Recorded per component so a fast run stays as explainable as a slow one.
    """

    BAKED = "baked"
    """Already present in the image; nothing was fetched."""

    CACHE = "cache"
    """Found in the host cache and copied in."""

    DOWNLOAD = "download"
    """Fetched from the asset repository during this episode."""


def data_key(repo: str, revision: str, component: str) -> str:
    """Content-derived address for one asset component.

    Independent of the task identifier by design: the same bundle referenced by two
    tasks resolves to one key, and renaming a task invalidates nothing.
    """
    digest = content_hash({"repo": repo, "revision": revision, "component": component})
    return digest.removeprefix("sha256:")[:32]


class StoreEntry(BaseModel):
    """One component present in a store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    component: str
    repo: str
    revision: str
    bytes: int = Field(default=0, ge=0)

    @property
    def path(self) -> PurePosixPath:
        return STORE_ROOT / self.key


class StoreManifest(BaseModel):
    """Inventory of what an image or cache already holds.

    Written to ``/ale/store/manifest.json`` when data is baked into an image, so the
    engine skips a fetch by lookup instead of by guesswork.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    entries: tuple[StoreEntry, ...] = ()

    def contains(self, key: str) -> bool:
        return any(entry.key == key for entry in self.entries)

    def entry(self, key: str) -> StoreEntry | None:
        return next((entry for entry in self.entries if entry.key == key), None)
