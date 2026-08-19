"""Translating what the model asks for into what the guest dispatches.

A mistranslated click lands somewhere else and the trajectory stops describing what
happened, so each case here pins one shape of the computer-use tool's vocabulary.
"""

from __future__ import annotations

import pytest

from ale.core.errors import AgentError
from ale.run.harnesses.computer_use import GRID, _computer_tool, translate_action


def test_computer_tool_uses_native_claude_and_generic_compatible_schemas() -> None:
    assert _computer_tool("claude-opus-4-8")["type"] == "computer_20250124"
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
    """The model is told the screen is 1000x1000, which is the space our actions use."""
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
