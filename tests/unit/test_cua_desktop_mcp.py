"""The built-in CUA MCP stays wire-compatible with AgentsLastExam."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ale.run.tools import cua_desktop_mcp

pytestmark = pytest.mark.unit

REFERENCE_TOOLS_SHA256 = "f8547ee34d5780ce05490d605a10aa5331cadb4b452161f8ef2f6f89ebd3b0de"


class FakeGui:
    def __init__(self) -> None:
        self.actions: list[dict[str, object]] = []

    def dispatch_actions(self, actions):  # type: ignore[no-untyped-def]
        self.actions.extend(actions)
        return len(actions)

    def capture_screen(self) -> bytes:
        return b"\x89PNG\r\n\x1a\nimage"

    def cursor_position(self) -> tuple[int, int]:
        return 250, 750

    def screen_size(self) -> tuple[int, int]:
        return 1920, 1080


def test_server_and_tool_schemas_match_agents_last_exam() -> None:
    encoded = json.dumps(cua_desktop_mcp.TOOLS, sort_keys=True, separators=(",", ":")).encode()
    assert cua_desktop_mcp.SERVER == {"name": "cua-desktop", "version": "0.3.0"}
    assert hashlib.sha256(encoded).hexdigest() == REFERENCE_TOOLS_SHA256


def test_actions_and_returns_match_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    gui = FakeGui()
    sleeps: list[float] = []
    monkeypatch.setattr(cua_desktop_mcp, "gui", gui)
    monkeypatch.setattr(cua_desktop_mcp.time, "sleep", sleeps.append)

    assert cua_desktop_mcp._call("key", {"keys": ["Control", "C"]}) == {
        "content": [{"type": "text", "text": "Pressed: ctrl+c"}]
    }
    assert cua_desktop_mcp._call(
        "drag",
        {"coordinate": [800, 600], "button": "right"},
    ) == {
        "content": [
            {
                "type": "text",
                "text": "Dragged (right) from current to [800, 600]",
            }
        ]
    }
    assert cua_desktop_mcp._call("hold_key", {"keys": ["Shift"], "duration": 0.25}) == {
        "content": [{"type": "text", "text": "Held shift for 0.25s"}]
    }
    assert sleeps == [0.25]
    assert gui.actions == [
        {"type": "key", "keys": ["ctrl", "c"]},
        {
            "type": "drag",
            "coordinate": None,
            "to": [800, 600],
            "button": "right",
        },
        {"type": "key_down", "keys": ["shift"]},
        {"type": "key_up", "keys": ["shift"]},
    ]


def test_screen_results_and_optional_save_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cua_desktop_mcp, "gui", FakeGui())
    target = tmp_path / "shot.png"

    result = cua_desktop_mcp._call("screenshot", {"save_path": str(target)})
    assert target.read_bytes().startswith(b"\x89PNG")
    assert result["content"][0]["text"] == f"Screenshot captured and saved to {target}"
    assert result["content"][1]["type"] == "image"
    assert cua_desktop_mcp._call("cursor_position", {}) == {
        "content": [{"type": "text", "text": "Cursor at [250, 750]"}]
    }
    assert cua_desktop_mcp._call("get_screen_size", {}) == {
        "content": [{"type": "text", "text": "Screen size: 1920x1080"}],
        "structuredContent": {"width": 1920, "height": 1080},
    }


def test_schema_validation_rejects_unknown_or_malformed_arguments() -> None:
    with pytest.raises(ValueError, match="unrecognized"):
        cua_desktop_mcp._call("click", {"unknown": True})
    with pytest.raises(ValueError, match="wrong length"):
        cua_desktop_mcp._call("mouse_move", {"coordinate": [1]})
