"""The sandbox, presented as a stepwise environment.

This is the single place a step-driven agent touches a sandbox, which is what makes the
framework's guarantees hold no matter what is driving. Every observation is captured
here and written to the trace here; every action is recorded here before it is
dispatched. An agent cannot report a step it did not take, or take one it did not
report, because it never touches the sandbox directly.

The two guards live here for the same reason. A step past the ceiling, or one taken when
the screen has not changed for several rounds, is refused by the environment — so an
agent that has never heard of our limits is bound by them anyway.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from hashlib import sha256

from ale.core.env import Observation, StepResult, TaskEnv
from ale.core.trace import ActionRecord, DesktopAction, ObservationRecord, TraceLayer, TraceWriter

__all__ = ["DEFAULT_MAX_STEPS", "DEFAULT_STALL_LIMIT", "SandboxEnv"]

DEFAULT_MAX_STEPS = 100

#: Identical screens in a row that count as no progress. Two could be a slow redraw.
DEFAULT_STALL_LIMIT = 3


class SandboxEnv(TaskEnv):
    """A live sandbox behind the stepwise contract."""

    def __init__(
        self,
        sandbox: object,
        *,
        instruction: str,
        trace: TraceWriter,
        max_steps: int = DEFAULT_MAX_STEPS,
        stall_limit: int = DEFAULT_STALL_LIMIT,
    ) -> None:
        self._sandbox = sandbox
        self._instruction = instruction
        self._trace = trace
        self.max_steps = max_steps
        self.stall_limit = stall_limit

        self.step_index = 0
        self._previous: str | None = None
        self._stalled = 0
        self._closed = False
        self._last = Observation(step=0)

    async def reset(self) -> Observation:
        self.step_index = 0
        self._previous = None
        self._stalled = 0
        return await self._observe(instruction=self._instruction)

    async def step(self, actions: Sequence[DesktopAction]) -> StepResult:
        # An empty batch is how an agent says it is finished. It is answering the
        # observation it was just given, so that is what the terminal result carries —
        # capturing another would cost a screenshot, write a duplicate blob, and add a
        # second record for a step during which nothing happened.
        if not actions:
            return StepResult(
                observation=self._last,
                terminated=True,
                info={"reason": "the agent returned no actions"},
            )

        applied = await self._sandbox.inject_input(list(actions))  # type: ignore[attr-defined]
        for index, action in enumerate(actions):
            self._trace.write_semantic(
                ActionRecord(
                    seq=self._trace.next_seq(TraceLayer.SEMANTIC),
                    step=self.step_index,
                    action=action,
                    accepted=index < applied,
                    rejection=None if index < applied else "not dispatched by the guest",
                )
            )

        self.step_index += 1
        observation = await self._observe()

        if self.step_index >= self.max_steps:
            return StepResult(
                observation=observation,
                truncated=True,
                info={"reason": f"reached the {self.max_steps}-step ceiling"},
            )
        if self._stalled >= self.stall_limit:
            return StepResult(
                observation=observation,
                truncated=True,
                info={
                    "reason": (
                        f"no progress: the screen was identical for {self.stall_limit} "
                        "consecutive steps"
                    )
                },
            )
        return StepResult(observation=observation)

    async def close(self) -> None:
        self._closed = True

    # --- internals ---

    async def _observe(self, *, instruction: str | None = None) -> Observation:
        """Capture the screen, record it, and notice whether anything changed.

        The image is written beside the trace and referenced by path. Inlining
        screenshots as base64 is how a trace becomes unreadable and unbounded, which the
        previous framework paid for.
        """
        png = await self._sandbox.screenshot()  # type: ignore[attr-defined]
        digest = sha256(png).hexdigest()

        if digest == self._previous:
            self._stalled += 1
        else:
            self._stalled = 0
        self._previous = digest

        path = self._trace.blob_path(f"step-{self.step_index:04d}.png")
        await asyncio.to_thread(path.write_bytes, png)
        reference = self._trace.relative(path)

        self._trace.write_semantic(
            ObservationRecord(
                seq=self._trace.next_seq(TraceLayer.SEMANTIC),
                step=self.step_index,
                screenshot_ref=reference,
            )
        )
        self._last = Observation(step=self.step_index, screenshot_png=png, instruction=instruction)
        return self._last
