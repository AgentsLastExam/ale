"""The stepwise view of a sandbox.

A ``TaskEnv`` is what a step-driven agent is handed: reset it, then step it with actions
until it reports the episode over. The shape is deliberately the one the surrounding
ecosystem already speaks — gymnasium, cua-lite and verifiers all look like this — so an
agent written against any of them plugs in without an adapter.

**The agent drives, and the framework still witnesses everything.** Those are not in
tension: the guarantee comes from the environment being ours, not from who calls whom.
Every ``reset`` and ``step`` passes through our code, so every observation and every
action is recorded whatever is driving. The earlier design had the framework call
``decide(observation)`` instead, and it bought nothing — the recording happened in the
same place either way, while every agent that wanted to drive needed a queue-based
inversion to be adapted.

It also puts the guards where they cannot be sidestepped. A step past the ceiling, or one
taken when the screen has not changed for several rounds, is refused *by the environment*
— so an agent that never heard of our limits is still bound by them.

This is not gymnasium's five-tuple. Our reward comes from the verify stage, after the
agent phase ends, so a per-step reward would be a field that is always zero; a shape that
lies for the sake of familiarity is worse than one that needs an adapter. ``reward``
exists and stays ``None`` until something genuinely produces per-step rewards.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from ale.core.trace import DesktopAction

__all__ = ["Observation", "StepResult", "TaskEnv"]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Observation(BaseModel):
    """What the agent is shown before it acts.

    Screenshot and text only. Structured observations — an accessibility tree, a DOM —
    are the obvious next want and go in ``extras`` until one of them has a second caller
    to justify a field of its own.
    """

    model_config = _FROZEN

    step: int = Field(ge=0)
    screenshot_png: bytes | None = None
    text: str | None = None
    instruction: str | None = Field(
        default=None, description="Present on the first observation only"
    )
    extras: dict[str, Any] = Field(default_factory=dict)


class StepResult(BaseModel):
    """What one step produced."""

    model_config = _FROZEN

    observation: Observation
    terminated: bool = False
    """A natural end: the agent said it was finished."""

    truncated: bool = False
    """A limit ended it — the step ceiling, or no progress for several rounds."""

    reward: float | None = Field(
        default=None,
        description="Reserved. Scoring happens in the verify stage, after this phase.",
    )
    info: dict[str, Any] = Field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.terminated or self.truncated


class TaskEnv(ABC):
    """A sandbox, presented one step at a time."""

    @abstractmethod
    async def reset(self) -> Observation:
        """Begin the episode; the first observation carries the instruction."""

    @abstractmethod
    async def step(self, actions: Sequence[DesktopAction]) -> StepResult:
        """Dispatch a batch of actions and return what followed.

        A batch rather than one action, because that is how agents decide: "click here,
        then type this" is a single thought. An empty batch means the agent considers
        the task finished and yields a terminated result.
        """

    async def close(self) -> None:
        """Release per-episode state. Idempotent."""
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
