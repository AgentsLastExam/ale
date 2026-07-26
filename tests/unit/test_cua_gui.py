"""The GUI adapter, tested without cua-lite installed.

Two things are worth pinning here. The action translation, because a mistranslated
click lands somewhere else and the trajectory silently stops describing what happened.
And the control inversion, because an agent that owns its loop being driven one step at
a time is the part most likely to deadlock.

A fake stands in for cua-lite's agent: it has the same surface (``sample(env)``) and no
dependencies, so these run everywhere.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ale.core.errors import AgentError
from ale.core.harness import HarnessSession, Observation
from ale.run.harnesses.cua_gui import CuaGuiHarness, translate_action

pytestmark = pytest.mark.unit


class FakeAgent:
    """Owns its loop, exactly as cua-lite's desktop agent does."""

    model_id = "fake-desktop-agent"

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self.script = script
        self.seen: list[str | None] = []

    async def sample(self, env: Any, max_steps: int = 10_000) -> str:
        result = await env.reset()
        self.seen.append(result.observation.text)
        for batch in self.script:
            if result.terminated:
                break
            result = await env.step(batch)
            self.seen.append(result.observation.text)
        await env.close()
        return "done"


class TestTranslateAction:
    def test_click_keeps_its_coordinate(self) -> None:
        action = translate_action({"action": "left_click", "coordinate": [512, 340]})
        assert action.type == "click"
        assert action.coordinate == (512, 340)

    def test_a_chord_is_split_into_keys(self) -> None:
        """cua-lite writes "ctrl+s"; ours is already a sequence."""
        assert translate_action({"action": "key", "key": "ctrl+s"}).keys == ("ctrl", "s")

    def test_a_hyphenated_chord_is_also_split(self) -> None:
        assert translate_action({"action": "keypress", "keys": "ctrl-alt-t"}).keys == (
            "ctrl",
            "alt",
            "t",
        )

    def test_typing_carries_its_text(self) -> None:
        assert translate_action({"action": "type", "text": "hello"}).text == "hello"

    def test_a_drag_carries_both_ends(self) -> None:
        action = translate_action(
            {"action": "left_click_drag", "start_coordinate": [10, 20], "end_coordinate": [30, 40]}
        )
        assert action.type == "drag"
        assert (action.coordinate, action.to) == ((10, 20), (30, 40))

    def test_an_unknown_action_is_refused_not_guessed(self) -> None:
        """Dispatching an approximation is how a trajectory stops being evidence."""
        with pytest.raises(AgentError, match="unsupported action"):
            translate_action({"action": "teleport", "coordinate": [1, 2]})


def session() -> HarnessSession:
    return HarnessSession(episode_id="e", token="t", gateway_url="http://gw", model="m")


class TestControlInversion:
    def harness(self, script: list[list[dict[str, Any]]]) -> tuple[CuaGuiHarness, FakeAgent]:
        agent = FakeAgent(script)
        return CuaGuiHarness(agent_factory=lambda _model: agent), agent

    def observation(self, step: int) -> Observation:
        return Observation(
            step=step,
            screenshot_png=b"\x89PNG-fake",
            instruction="do the thing" if step == 0 else None,
        )

    @pytest.mark.asyncio
    async def test_each_decision_comes_from_the_agents_own_loop(self) -> None:
        harness, agent = self.harness(
            [
                [{"action": "left_click", "coordinate": [100, 200]}],
                [{"action": "type", "text": "hi"}],
            ]
        )
        await harness.start("do the thing", session())

        first = await harness.decide(self.observation(0))
        assert [a.type for a in first] == ["click"]

        second = await harness.decide(self.observation(1))
        assert [a.type for a in second] == ["type"]

        # The instruction reached the agent through the shim's first observation.
        assert agent.seen[0] == "do the thing"
        assert await harness.finish()

    @pytest.mark.asyncio
    async def test_an_agent_that_returns_ends_the_episode(self) -> None:
        """An empty decision is how a policy agent says it is finished."""
        harness, _ = self.harness([[{"action": "wait"}]])
        await harness.start("go", session())

        await harness.decide(self.observation(0))
        # The fake's script is exhausted, so its sample() returns rather than stepping.
        assert await harness.decide(self.observation(1)) == []
        await harness.finish()

    @pytest.mark.asyncio
    async def test_finish_releases_an_agent_still_waiting(self) -> None:
        """A guard can end the episode mid-step; the agent must not be left awaiting."""
        harness, _ = self.harness([[{"action": "wait"}]] * 100)
        await harness.start("go", session())
        await harness.decide(self.observation(0))

        await asyncio.wait_for(harness.finish(), timeout=10)
        assert harness._task is not None and harness._task.done()

    @pytest.mark.asyncio
    async def test_deciding_before_starting_is_an_error(self) -> None:
        harness, _ = self.harness([])
        with pytest.raises(AgentError, match="before start"):
            await harness.decide(self.observation(0))

    @pytest.mark.asyncio
    async def test_a_missing_cua_lite_says_so_precisely(self) -> None:
        """cua-lite is optional, so the failure names the cause and the alternative."""
        harness = CuaGuiHarness()
        with pytest.raises(AgentError, match="cua-lite"):
            await harness.start("go", session())
