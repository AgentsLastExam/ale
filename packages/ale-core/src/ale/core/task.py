"""Tasks and task loaders.

A :class:`Task` binds a specification to behaviour; a :class:`Taskset` produces tasks.
Variants are expanded here rather than duplicated on disk: one folder with a variant
table yields several tasks, each with its own identity and its own rendered prompt.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

from ale.core.taskspec import TaskSpec
from ale.core.verdict import Rewards

if TYPE_CHECKING:
    from ale.core.environment import EpisodeContext

__all__ = ["Task", "Taskset"]


class Task(ABC):
    """One task instance, ready to run."""

    def __init__(self, spec: TaskSpec) -> None:
        self.spec = spec

    @property
    def id(self) -> str:
        return self.spec.id

    async def setup(self, ctx: EpisodeContext) -> None:
        """Prepare the sandbox beyond the declared setup steps. Usually nothing."""
        return None

    @abstractmethod
    async def score(self, ctx: EpisodeContext) -> Rewards:
        """Produce the rewards for a finished episode."""

    async def cleanup(self, ctx: EpisodeContext) -> None:
        """Undo task-specific side effects. The sandbox itself is not yours to destroy."""
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.spec.id})"


class Taskset(ABC):
    """Produces tasks. Variant expansion happens here, not on disk."""

    infinite: bool = False
    """Set by generators that can emit unboundedly many tasks."""

    @abstractmethod
    def load(self) -> Iterable[Task]:
        """Yield tasks. May be a generator."""

    def select(self, ids: set[str] | None = None) -> Iterator[Task]:
        """Yield tasks, optionally filtered by identifier or family."""
        for task in self.load():
            if ids is None or task.spec.id in ids or task.spec.family in ids:
                yield task

    def metadata(self) -> dict[str, Any]:
        """Anything worth recording about this loader."""
        return {}
