"""Runtime binding for one rendered Task instance."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ale.core.sandbox import PreparedTaskImage
from ale.core.taskspec import TaskSpec
from ale.core.verdict import Rewards

if TYPE_CHECKING:
    from ale.core.environment import EpisodeContext

__all__ = ["Task", "TaskAssetObservation", "TaskFolder", "TaskSourceContext"]


@dataclass(frozen=True)
class TaskSourceContext:
    repository_root: Path | None = None
    repository_name: str | None = None
    task_relative_path: str | None = None

    def __post_init__(self) -> None:
        values = self.repository_root, self.repository_name, self.task_relative_path
        if any(value is None for value in values) and any(value is not None for value in values):
            raise ValueError("Task source context fields must be present or absent together")


@dataclass(frozen=True)
class TaskAssetObservation:
    repository: str
    task_path: str
    commit: str | None
    dirty: bool


class TaskFolder(Protocol):
    """Filesystem surface used by the standard runtime."""

    root: Path

    @property
    def image_dir(self) -> Path: ...

    @property
    def image_dockerfile(self) -> Path | None: ...

    @property
    def verifier_dockerfile(self) -> Path | None: ...

    def stage_dir(self, name: str) -> Path | None: ...

    def stage_entry(self, name: str) -> Path | None: ...


class Task(ABC):
    def __init__(
        self,
        spec: TaskSpec,
        *,
        folder: TaskFolder | None,
        source: TaskSourceContext | None = None,
        task_digest: str,
        image_source_digest: str | None,
        verifier_image_source_digest: str | None = None,
    ) -> None:
        self.spec = spec
        self.folder = folder
        self.source = source or TaskSourceContext()
        self.task_digest = task_digest
        self.image_source_digest = image_source_digest
        self.verifier_image_source_digest = verifier_image_source_digest
        self.prepared_image: PreparedTaskImage | None = None
        self.prepared_verifier_image: PreparedTaskImage | None = None
        self.asset_observation: TaskAssetObservation | None = None

    @property
    def id(self) -> str:
        return str(self.spec.name)

    async def setup(self, ctx: EpisodeContext) -> None:
        return None

    @abstractmethod
    async def score(self, ctx: EpisodeContext) -> Rewards:
        """Produce rewards for a finished episode."""

    async def cleanup(self, ctx: EpisodeContext) -> None:
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.spec.label})"
