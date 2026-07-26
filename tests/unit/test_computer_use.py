"""Translating what the model asks for into what the guest dispatches.

A mistranslated click lands somewhere else and the trajectory stops describing what
happened, so each case here pins one shape of the computer-use tool's vocabulary.
"""

from __future__ import annotations

import pytest

from ale.core.errors import AgentError
from ale.run.harnesses.computer_use import GRID, translate_action

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


def test_a_screenshot_request_dispatches_nothing() -> None:
    """One is sent every step already; acting on the request would waste a round trip."""
    assert translate_action({"action": "screenshot"}) is None


def test_a_drag_carries_both_ends() -> None:
    action = translate_action({"action": "left_click_drag", "coordinate": [10, 20], "to": [30, 40]})
    assert action is not None
    assert (action.coordinate, action.to) == ((10, 20), (30, 40))


def test_an_unknown_action_is_refused_not_guessed() -> None:
    with pytest.raises(AgentError, match="unsupported"):
        translate_action({"action": "teleport", "coordinate": [1, 2]})
