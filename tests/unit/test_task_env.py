"""The stepwise contract, and the guards that live inside it.

The property worth protecting: an agent drives, but it cannot take a step the framework
did not see, and it cannot exceed a limit by ignoring one — because the environment is
the only thing that touches the sandbox and the only thing that counts.

A fake sandbox stands in for a real one, so these need no container.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.env import Observation, StepResult
from ale.core.harness import HarnessSession, StepwisePolicy
from ale.core.trace import DesktopAction
from ale.core.trajectory import AtifAgent, TrajectoryBuilder
from ale.run.envs import SandboxEnv
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit

CLICK = [DesktopAction(type="click", coordinate=(10, 20))]
SHOOT = DesktopAction(type="screenshot")
#: A click and a look, which is what a step that wants to see the result looks like.
CLICK_AND_LOOK = [*CLICK, SHOOT]


def session() -> HarnessSession:
    return HarnessSession(episode_id="e", token="t", gateway_url="http://gw", model="m")


class FakeSandbox:
    """Returns a different screen each step unless told to freeze."""

    def __init__(self, *, frozen: bool = False, accept: int | None = None) -> None:
        self.frozen = frozen
        self.accept = accept
        self.dispatched: list[list[DesktopAction]] = []
        self._frame = 0

    async def screenshot(self) -> bytes:
        if not self.frozen:
            self._frame += 1
        return f"frame-{self._frame}".encode()

    async def inject_input(self, actions: list[DesktopAction]) -> int:
        self.dispatched.append(list(actions))
        return len(actions) if self.accept is None else self.accept


def make_env(tmp_path: Path, sandbox: FakeSandbox, **kwargs: object) -> SandboxEnv:
    return SandboxEnv(
        sandbox,
        instruction="do the thing",
        trajectory=TrajectoryBuilder(
            agent=AtifAgent(name="test-policy", version="1"),
            trajectory_id="trajectory-e",
        ),
        blobs=BlobStore(tmp_path),
        **kwargs,  # type: ignore[arg-type]
    )


class TestStepping:
    @pytest.mark.asyncio
    async def test_reset_carries_the_instruction_and_no_picture(self, tmp_path: Path) -> None:
        """An agent that wants to see the screen asks, on this step like any other."""
        env = make_env(tmp_path, FakeSandbox())
        first = await env.reset()
        assert first.instruction == "do the thing"
        assert first.screenshot_png is None

        following = await env.step(CLICK)
        assert following.observation.instruction is None

    @pytest.mark.asyncio
    async def test_a_screenshot_arrives_only_when_asked_for(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, FakeSandbox())
        await env.reset()

        assert (await env.step(CLICK)).observation.screenshot_png is None
        assert (await env.step([SHOOT])).observation.screenshot_png is not None

    @pytest.mark.asyncio
    async def test_asking_for_a_screenshot_touches_nothing(self, tmp_path: Path) -> None:
        """It is a request to the environment, not something done to the desktop."""
        sandbox = FakeSandbox()
        env = make_env(tmp_path, sandbox)
        await env.reset()

        await env.step([SHOOT])
        assert sandbox.dispatched == []

    @pytest.mark.asyncio
    async def test_a_batch_is_dispatched_as_one(self, tmp_path: Path) -> None:
        """ "Click here, then type this" is a single decision, not two steps."""
        sandbox = FakeSandbox()
        env = make_env(tmp_path, sandbox)
        await env.reset()

        batch = [
            DesktopAction(type="click", coordinate=(1, 2)),
            DesktopAction(type="type", text="hello"),
        ]
        result = await env.step(batch)

        assert sandbox.dispatched == [batch]
        assert not result.done
        assert env.step_index == 1

    @pytest.mark.asyncio
    async def test_an_empty_batch_ends_the_episode(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, FakeSandbox())
        await env.reset()

        result = await env.step([])
        assert result.terminated and not result.truncated
        assert result.done

    @pytest.mark.asyncio
    async def test_scoring_is_not_this_phase_s_business(self, tmp_path: Path) -> None:
        """Reward comes from the verify stage; a per-step number would be a lie."""
        env = make_env(tmp_path, FakeSandbox())
        await env.reset()
        assert (await env.step(CLICK)).reward is None


class TestGuards:
    @pytest.mark.asyncio
    async def test_the_step_ceiling_truncates(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, FakeSandbox(), max_steps=2)
        await env.reset()

        assert not (await env.step(CLICK)).done
        result = await env.step(CLICK)
        assert result.truncated and not result.terminated
        assert "2-step ceiling" in result.info["reason"]

    @pytest.mark.asyncio
    async def test_an_unchanging_screen_truncates(self, tmp_path: Path) -> None:
        """Acting while nothing changes is no progress, not patience.

        Judged over the screens the agent chose to look at, since those are the only ones
        that exist now.
        """
        env = make_env(tmp_path, FakeSandbox(frozen=True), max_steps=50, stall_limit=3)
        await env.reset()

        # Four looks to see three identical transitions: the first has nothing to differ
        # from, since reset no longer photographs a screen nobody asked to see.
        results = [await env.step(CLICK_AND_LOOK) for _ in range(4)]
        assert results[-1].truncated
        assert "no progress" in results[-1].info["reason"]

    @pytest.mark.asyncio
    async def test_a_repeated_batch_truncates_only_when_nobody_looked(self, tmp_path: Path) -> None:
        """The blind spot the screen signal leaves.

        An agent that never asks for a screenshot cannot be judged on what the screen
        shows, and a loop of identical actions is the same non-progress by another name.
        """
        env = make_env(tmp_path, FakeSandbox(), max_steps=50, stall_limit=3)
        await env.reset()

        results = [await env.step(CLICK) for _ in range(4)]
        assert results[-1].truncated
        assert "no progress" in results[-1].info["reason"]

    @pytest.mark.asyncio
    async def test_a_changing_screen_resets_the_stall_count(self, tmp_path: Path) -> None:
        sandbox = FakeSandbox(frozen=True)
        env = make_env(tmp_path, sandbox, max_steps=50, stall_limit=3)
        await env.reset()

        await env.step(CLICK_AND_LOOK)
        await env.step(CLICK_AND_LOOK)
        sandbox.frozen = False  # something finally happened
        await env.step(CLICK_AND_LOOK)
        sandbox.frozen = True
        assert not (await env.step(CLICK_AND_LOOK)).done

    @pytest.mark.asyncio
    async def test_repeating_an_action_is_fine_while_the_screen_moves(self, tmp_path: Path) -> None:
        """Scrolling is the same action over and over, and it is progress.

        The screen decides whenever the agent looked, precisely so that a deliberate
        repetition is not mistaken for a loop.
        """
        env = make_env(tmp_path, FakeSandbox(), max_steps=50, stall_limit=3)
        await env.reset()

        results = [await env.step(CLICK_AND_LOOK) for _ in range(6)]
        assert not any(result.done for result in results)

    @pytest.mark.asyncio
    async def test_the_guards_bind_an_agent_that_ignores_them(self, tmp_path: Path) -> None:
        """The point of putting them here: a driver cannot opt out of a ceiling."""

        stopped: list[StepResult] = []

        class Relentless(StepwisePolicy):
            name = "relentless"

            def version(self) -> str:
                return "1"

            async def decide(self, observation: Observation) -> list[DesktopAction]:
                return CLICK  # never finishes on its own

            async def finish(self, result: StepResult) -> str | None:
                stopped.append(result)
                return None

        env = make_env(tmp_path, FakeSandbox(), max_steps=4)
        await Relentless().rollout(env, session())

        # It never asked to stop; the environment stopped it.
        assert env.step_index == 4
        assert stopped[0].truncated
        assert "ceiling" in stopped[0].info["reason"]


class TestWitnessing:
    @pytest.mark.asyncio
    async def test_every_observation_and_action_reaches_the_trace(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, FakeSandbox())
        await env.reset()
        await env.step([DesktopAction(type="click", coordinate=(1, 2)), SHOOT])
        await env.step(
            [DesktopAction(type="type", text="x"), DesktopAction(type="key", keys=("a",))]
        )

        steps = env.trajectory_steps
        calls = [call for step in steps for call in step.tool_calls or ()]
        results = [
            result
            for step in steps
            for result in (step.observation.results if step.observation else ())
        ]

        assert len(steps) == 2
        assert len(calls) == len(results) == 4
        assert all(
            result.source_call_id == call.tool_call_id
            for call, result in zip(calls, results, strict=True)
        )

    @pytest.mark.asyncio
    async def test_screenshots_are_files_not_inlined(self, tmp_path: Path) -> None:
        """Base64 in a trace is how it becomes unreadable and unbounded."""
        env = make_env(tmp_path, FakeSandbox())
        await env.reset()
        await env.step([SHOOT])

        (result,) = env.trajectory_steps[0].observation.results
        assert isinstance(result.content, list)
        assert result.content[0].source is not None
        path = result.content[0].source.path
        assert path.startswith("blobs/image/")
        assert (tmp_path / path).is_file()

    @pytest.mark.asyncio
    async def test_an_undispatched_action_is_recorded_as_rejected(self, tmp_path: Path) -> None:
        """A trajectory has to say what did not happen, or it is not evidence."""
        env = make_env(tmp_path, FakeSandbox(accept=1))
        await env.reset()
        await env.step([DesktopAction(type="click", coordinate=(1, 2)), DesktopAction(type="wait")])

        calls = env.trajectory_steps[0].tool_calls
        assert calls is not None
        assert [call.extra["ale"]["accepted"] for call in calls] == [True, False]
        assert calls[1].extra["ale"]["rejection"]


class TestStepwisePolicy:
    @pytest.mark.asyncio
    async def test_it_supplies_the_loop_for_agents_that_prefer_questions(
        self, tmp_path: Path
    ) -> None:
        seen: list[int] = []

        class Counting(StepwisePolicy):
            name = "counting"

            def version(self) -> str:
                return "1"

            async def decide(self, observation: Observation) -> list[DesktopAction]:
                seen.append(observation.step)
                return CLICK if len(seen) < 3 else []

            async def finish(self, result: StepResult) -> str | None:
                return f"stopped at step {result.observation.step}"

        env = make_env(tmp_path, FakeSandbox())
        closing = await Counting().rollout(env, session())

        assert seen == [0, 1, 2]
        assert closing == "stopped at step 2"
