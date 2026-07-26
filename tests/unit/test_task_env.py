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
from ale.core.trace import DesktopAction, TraceLayer, TraceWriter, read_records
from ale.run.envs import SandboxEnv

pytestmark = pytest.mark.unit

CLICK = [DesktopAction(type="click", coordinate=(10, 20))]


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
        trace=TraceWriter(tmp_path),
        **kwargs,  # type: ignore[arg-type]
    )


class TestStepping:
    @pytest.mark.asyncio
    async def test_reset_carries_the_instruction_once(self, tmp_path: Path) -> None:
        env = make_env(tmp_path, FakeSandbox())
        first = await env.reset()
        assert first.instruction == "do the thing"

        following = await env.step(CLICK)
        assert following.observation.instruction is None

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
        """Acting while nothing changes is no progress, not patience."""
        env = make_env(tmp_path, FakeSandbox(frozen=True), max_steps=50, stall_limit=3)
        await env.reset()

        results = [await env.step(CLICK) for _ in range(3)]
        assert results[-1].truncated
        assert "no progress" in results[-1].info["reason"]

    @pytest.mark.asyncio
    async def test_a_changing_screen_resets_the_stall_count(self, tmp_path: Path) -> None:
        sandbox = FakeSandbox(frozen=True)
        env = make_env(tmp_path, sandbox, max_steps=50, stall_limit=3)
        await env.reset()

        await env.step(CLICK)
        await env.step(CLICK)
        sandbox.frozen = False  # something finally happened
        await env.step(CLICK)
        sandbox.frozen = True
        assert not (await env.step(CLICK)).done

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
        await env.step([DesktopAction(type="click", coordinate=(1, 2))])
        await env.step(
            [DesktopAction(type="type", text="x"), DesktopAction(type="key", keys=("a",))]
        )

        records = list(read_records(TraceWriter(tmp_path).path(TraceLayer.SEMANTIC)))
        observations = [r for r in records if r["kind"] == "observation"]
        actions = [r for r in records if r["kind"] == "action"]

        assert len(observations) == 3  # reset, then one after each step
        assert len(actions) == 3
        assert [a["step"] for a in actions] == [0, 1, 1]

    @pytest.mark.asyncio
    async def test_screenshots_are_files_not_inlined(self, tmp_path: Path) -> None:
        """Base64 in a trace is how it becomes unreadable and unbounded."""
        env = make_env(tmp_path, FakeSandbox())
        await env.reset()

        (observation,) = [
            r
            for r in read_records(TraceWriter(tmp_path).path(TraceLayer.SEMANTIC))
            if r["kind"] == "observation"
        ]
        assert observation["screenshot_ref"].startswith("blobs/")
        assert (tmp_path / observation["screenshot_ref"]).is_file()

    @pytest.mark.asyncio
    async def test_an_undispatched_action_is_recorded_as_rejected(self, tmp_path: Path) -> None:
        """A trajectory has to say what did not happen, or it is not evidence."""
        env = make_env(tmp_path, FakeSandbox(accept=1))
        await env.reset()
        await env.step([DesktopAction(type="click", coordinate=(1, 2)), DesktopAction(type="wait")])

        actions = [
            r
            for r in read_records(TraceWriter(tmp_path).path(TraceLayer.SEMANTIC))
            if r["kind"] == "action"
        ]
        assert [a["accepted"] for a in actions] == [True, False]
        assert actions[1]["rejection"]


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
