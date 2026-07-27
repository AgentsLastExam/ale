"""The sandbox, presented as a stepwise environment.

This is the single place a step-driven agent touches a sandbox, which is what makes the
framework's guarantees hold no matter what is driving. Every observation is captured
here and written to the trace here; every action is recorded here before it is
dispatched. An agent cannot report a step it did not take, or take one it did not
report, because it never touches the sandbox directly.

The two guards live here for the same reason. A step past the ceiling, or one that makes
no difference several rounds running, is refused by the environment — so an agent that has
never heard of our limits is bound by them anyway.

**Screenshots are asked for, not given.** The environment photographs the screen when the
agent's batch contains a ``screenshot`` action and at no other time. cua-lite captures
after every step; we deliberately do not, for three reasons. An image the agent did not
ask for is one it did not choose to look at, so a trajectory that contains one everywhere
cannot show when the agent decided it needed to see. Every step pays for a capture and a
blob whether or not it is read. And an agent that never looks at a screen — most of them —
drags a screenshot through a context window on every turn of a task that has no desktop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from hashlib import sha256

from ale.core.env import Observation, StepResult, TaskEnv
from ale.core.trace import ActionRecord, DesktopAction, ObservationRecord, TraceLayer, TraceWriter

__all__ = ["DEFAULT_MAX_STEPS", "DEFAULT_STALL_LIMIT", "SandboxEnv"]

DEFAULT_MAX_STEPS = 100

#: Rounds without a difference that count as no progress. Two could be a slow redraw.
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
        self._previous_actions: str | None = None
        self._stalled = 0
        self._closed = False
        self._last = Observation(step=0)

    async def reset(self) -> Observation:
        """The instruction, and nothing to look at yet.

        An agent that wants to see the screen asks for it, on this step like any other.
        """
        self.step_index = 0
        self._previous = None
        self._previous_actions = None
        self._stalled = 0
        self._last = Observation(step=0, instruction=self._instruction)
        return self._last

    async def step(self, actions: Sequence[DesktopAction]) -> StepResult:
        # An empty batch is how an agent says it is finished. It is answering the
        # observation it was just given, so that is what the terminal result carries.
        if not actions:
            return StepResult(
                observation=self._last,
                terminated=True,
                info={"reason": "the agent returned no actions"},
            )

        # `screenshot` is served here rather than dispatched: it asks the environment for
        # an observation, it does not do anything to the desktop. Sending it onward would
        # reach a guest that has no such action and record it as rejected.
        wants_screenshot = any(action.type == "screenshot" for action in actions)
        dispatched = [action for action in actions if action.type != "screenshot"]

        applied = (
            await self._sandbox.inject_input(dispatched)  # type: ignore[attr-defined]
            if dispatched
            else 0
        )
        index = 0
        for action in actions:
            if action.type == "screenshot":
                accepted, rejection = True, None
            else:
                accepted = index < applied
                rejection = None if accepted else "not dispatched by the guest"
                index += 1
            self._trace.write_semantic(
                ActionRecord(
                    seq=self._trace.next_seq(TraceLayer.SEMANTIC),
                    step=self.step_index,
                    action=action,
                    accepted=accepted,
                    rejection=rejection,
                )
            )

        self.step_index += 1
        observation = (
            await self._observe() if wants_screenshot else Observation(step=self.step_index)
        )
        self._last = observation
        self._note_progress(actions, observation)

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
                info={"reason": (f"no progress for {self.stall_limit} consecutive steps")},
            )
        return StepResult(observation=observation)

    async def close(self) -> None:
        self._closed = True

    # --- internals ---

    def _note_progress(self, actions: Sequence[DesktopAction], observation: Observation) -> None:
        """Decide whether this step changed anything, without assuming a screenshot.

        The screen is the better signal and wins whenever there is one: an agent may
        repeat an identical action deliberately — scrolling down, clicking through pages —
        and as long as the screen keeps changing it is making progress. Judging such a run
        by its actions would cut it off for doing exactly the right thing.

        Only when the agent did not look is there nothing else to go on, and then a batch
        identical to the last counts as no progress. That covers the case the screen signal
        cannot see at all: an agent looping without ever opening its eyes.

        Either signal differing resets the count. A stall is a run of sameness, not a tally.
        """
        signature = repr([action.model_dump(mode="json") for action in actions])
        previous_actions, self._previous_actions = self._previous_actions, signature

        if observation.screenshot_png is not None:
            digest = sha256(observation.screenshot_png).hexdigest()
            previous, self._previous = self._previous, digest
            stalled = previous is not None and digest == previous
        else:
            stalled = signature == previous_actions

        self._stalled = self._stalled + 1 if stalled else 0

    async def _observe(self, *, instruction: str | None = None) -> Observation:
        """Capture the screen, record it, and notice whether anything changed.

        The image is written beside the trace and referenced by path. Inlining
        screenshots as base64 is how a trace becomes unreadable and unbounded, which the
        previous framework paid for.
        """
        png = await self._sandbox.screenshot()  # type: ignore[attr-defined]

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
        return Observation(step=self.step_index, screenshot_png=png, instruction=instruction)
