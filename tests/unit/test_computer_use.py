"""Translating what the model asks for into what the guest dispatches.

A mistranslated click lands somewhere else and the trajectory stops describing what
happened, so each case here pins one shape of the computer-use tool's vocabulary.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Sequence
from typing import Any

import pytest
from aiohttp import web

from ale.core.env import Observation, StepResult, TaskEnv
from ale.core.errors import AgentError
from ale.core.harness import HarnessSession
from ale.core.trace import DesktopAction
from ale.run.harnesses.computer_use import (
    GRID,
    ComputerUseHarness,
    _computer_tool,
    translate_action,
)


@pytest.mark.parametrize("status", [200, 503])
async def test_model_error_keeps_native_cause_and_redacts_gateway_token(status: int) -> None:
    async def reply(_request: web.Request) -> web.Response:
        return web.json_response(
            {"type": "error", "error": {"message": "capacity exhausted secret-episode-key"}},
            status=status,
        )

    app = web.Application()
    app.router.add_post("/v1/messages", reply)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        harness = ComputerUseHarness()
        await harness.start(
            HarnessSession(
                episode_id="episode",
                model="model",
                gateway_url=f"http://127.0.0.1:{port}",
                token="secret-episode-key",
            )
        )
        with pytest.raises(AgentError) as caught:
            await harness._ask()
        assert f"HTTP {status}" in str(caught.value)
        assert "capacity exhausted" in str(caught.value)
        assert "secret-episode-key" not in str(caught.value)
    finally:
        await runner.cleanup()


async def test_concurrent_rollouts_keep_their_own_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, int]] = []

    async def ask(self: ComputerUseHarness) -> dict[str, Any]:
        assert self._session is not None
        asked_for = self._session.episode_id
        await asyncio.sleep(0.01)
        seen.append((asked_for, self._session.episode_id, len(self._messages)))
        return {"content": [{"type": "text", "text": "finished"}]}

    class Env(TaskEnv):
        async def reset(self) -> Observation:
            return Observation(step=0, instruction="do the task")

        async def step(self, actions: Sequence[DesktopAction]) -> StepResult:
            return StepResult(observation=Observation(step=1), terminated=not actions)

    monkeypatch.setattr(ComputerUseHarness, "_ask", ask)
    harness = ComputerUseHarness()
    sessions = [
        HarnessSession(episode_id=name, model="m", gateway_url="http://gateway", token=name)
        for name in ("a", "b")
    ]

    await asyncio.gather(*(harness.rollout(Env(), session) for session in sessions))

    assert sorted(seen) == [("a", "a", 1), ("b", "b", 1)]


def test_computer_tool_uses_native_claude_and_generic_compatible_schemas() -> None:
    native = _computer_tool("claude-opus-5-5")
    assert native["type"] == "computer_toolset_20260801"
    assert not {"name", "display_width_px", "display_height_px"} & native.keys()
    assert native["configs"]["zoom"] == {"enabled": False}
    assert native["configs"]["cursor_position"] == {"enabled": False}
    generic = _computer_tool("qwen3.5-plus")
    assert "type" not in generic
    assert {"screenshot", "type", "key"} <= set(
        generic["input_schema"]["properties"]["action"]["enum"]
    )


pytestmark = pytest.mark.unit


def test_a_click_keeps_its_coordinate() -> None:
    action = translate_action({"action": "left_click", "coordinate": [512, 340]})
    assert action is not None
    assert (action.type, action.coordinate) == ("click", (512, 340))


def test_coordinates_need_no_rescaling() -> None:
    """Generic computer tools continue to use ALE's normalized coordinate space."""
    assert GRID == 1000
    action = translate_action({"action": "mouse_move", "coordinate": [GRID, 0]})
    assert action is not None and action.coordinate == (GRID, 0)


def test_typing_carries_its_text() -> None:
    action = translate_action({"action": "type", "text": "ALE-38D85F"})
    assert action is not None and action.text == "ALE-38D85F"


def test_a_chord_becomes_a_key_sequence() -> None:
    action = translate_action({"action": "key", "text": "ctrl+s"})
    assert action is not None
    assert (action.type, action.keys) == ("key", ("ctrl", "s"))
    spaced = translate_action({"action": "key", "text": "ctrl s"})
    assert spaced is not None and spaced.keys == ("ctrl", "s")
    array = translate_action({"action": "key", "keys": ["ctrl", "s"]})
    assert array is not None and array.keys == ("ctrl", "s")
    encoded = translate_action({"action": "key", "keys": '["ctrl", "s"]'})
    assert encoded is not None and encoded.keys == ("ctrl", "s")


def test_a_screenshot_request_becomes_an_action() -> None:
    """The environment photographs the screen when asked and not otherwise.

    This used to translate to nothing, because a screenshot arrived after every step
    whether or not the model wanted one. Now the request is the only way to get one.
    """
    action = translate_action({"action": "screenshot"})
    assert action is not None
    assert action.type == "screenshot"


def test_a_drag_carries_both_ends() -> None:
    action = translate_action({"action": "left_click_drag", "coordinate": [10, 20], "to": [30, 40]})
    assert action is not None
    assert (action.coordinate, action.to) == ((10, 20), (30, 40))


def test_an_unknown_action_is_refused_not_guessed() -> None:
    with pytest.raises(AgentError, match="unsupported"):
        translate_action({"action": "teleport", "coordinate": [1, 2]})


# Only the PNG header is needed to exercise coordinate-space metadata.
PNG = b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", 1440, 900)


@pytest.mark.parametrize("model", ["claude-opus-5-5", "qwen3.5-plus"])
async def test_computer_protocol_round_trip(model):
    native = model.startswith("claude")
    requests = []

    async def reply(request):
        body = await request.json()
        requests.append(body)
        assert "anthropic-beta" not in request.headers
        assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
        if len(requests) == 1:
            block = {"type": "tool_use", "id": "click-1", "input": {"coordinate": [720, 450]}}
            if native:
                block.update(name="left_click", toolset_name="computer")
            else:
                block["name"] = "computer"
                block["input"]["action"] = "left_click"
            return web.json_response({"content": [block]})
        if len(requests) == 2:
            block = {
                "type": "tool_use",
                "id": "shot-2",
                "name": "computer",
                "input": {"action": "screenshot"},
            }
            if native:
                block.update(name="screenshot", toolset_name="computer", input={})
            return web.json_response({"content": [block]})
        return web.json_response({"content": [{"type": "text", "text": "done"}]})

    app = web.Application()
    app.router.add_post("/v1/messages", reply)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        harness = ComputerUseHarness(model=model)
        await harness.start(
            HarnessSession(
                episode_id="test", model=model, gateway_url=f"http://127.0.0.1:{port}", token="test"
            )
        )
        actions = await harness.decide(Observation(step=0, instruction="Click", screenshot_png=PNG))
        assert actions[0].coordinate == ((500, 500) if native else (720, 450))
        assert (await harness.decide(Observation(step=1)))[0].type == "screenshot"
        assert await harness.decide(Observation(step=2, screenshot_png=PNG)) == []
        result = requests[1]["messages"][-1]["content"][0]
        assert result["tool_use_id"] == "click-1"
        assert result.get("toolset_name") == ("computer" if native else None)
        assert result["content"][0]["type"] == "text"
        result = requests[2]["messages"][-1]["content"][0]
        assert result["tool_use_id"] == "shot-2"
        assert result.get("toolset_name") == ("computer" if native else None)
        assert result["content"][0]["source"]["media_type"] == "image/png"
    finally:
        await runner.cleanup()


async def toolset_decision(monkeypatch, content, screenshot=PNG):
    harness = ComputerUseHarness(model="claude-opus-5-5")
    await harness.start(
        HarnessSession(
            episode_id="test", model=harness.model, gateway_url="http://gateway", token="test"
        )
    )

    async def reply():
        return {"content": content}

    monkeypatch.setattr(harness, "_ask", reply)
    return await harness.decide(Observation(step=0, screenshot_png=screenshot))


def member(name, **arguments):
    return {
        "type": "tool_use",
        "id": "tool-1",
        "toolset_name": "computer",
        "name": name,
        "input": arguments,
    }


async def test_native_drag_repeat_and_wait_keep_their_meaning(monkeypatch):
    actions = await toolset_decision(
        monkeypatch, [member("left_click_drag", start_coordinate=[144, 90], coordinate=[720, 450])]
    )
    assert actions[0].coordinate == (100, 100)
    assert actions[0].to == (500, 500)
    repeated = await toolset_decision(monkeypatch, [member("key", text="Tab", repeat=3)])
    assert len(repeated) == 3 and all(a.keys == ("Tab",) for a in repeated)
    waiting = await toolset_decision(monkeypatch, [member("wait", duration=1.25)])
    assert waiting[0].duration_ms == 1250


@pytest.mark.parametrize(
    "blocks",
    [
        [member("screenshot"), member("key", text="Tab")],
        [{**member("screenshot"), "toolset_name": "browser"}],
        [member("zoom", region=[0, 0, 20, 20])],
        [member("left_click", coordinate=[1440, 0])],
        [member("left_click", coordinate=[10, 10], text="shift")],
        [member("key", text="Tab", repeat=0)],
        [member("wait", duration=301)],
    ],
)
async def test_unsupported_native_calls_fail_explicitly(monkeypatch, blocks):
    with pytest.raises(AgentError):
        await toolset_decision(monkeypatch, blocks)


async def test_native_coordinates_require_observed_dimensions(monkeypatch):
    with pytest.raises(AgentError, match="screenshot"):
        await toolset_decision(monkeypatch, [member("left_click", coordinate=[10, 10])], None)
