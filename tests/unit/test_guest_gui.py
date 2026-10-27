from __future__ import annotations

import base64
import json
import subprocess

import pytest

from ale.run.guestd import gui

pytestmark = pytest.mark.unit


def test_cua_driver_is_the_only_gui_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui.sys, "platform", "linux")
    calls: list[tuple[str, dict[str, object]]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        tool = command[2]
        arguments = json.loads(command[3])
        calls.append((tool, arguments))
        payload: dict[str, object] = {"verified": True}
        if tool == "get_screen_size":
            payload = {"width": 200, "height": 100, "scale_factor": 1}
        elif tool == "get_desktop_state":
            payload = {"screenshot_png_b64": base64.b64encode(b"png").decode()}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(gui.shutil, "which", lambda name: "/usr/bin/cua-driver")
    monkeypatch.setattr(gui.subprocess, "run", run)

    assert gui.capture_screen() == b"png"
    assert gui.dispatch_actions([{"type": "click", "coordinate": [500, 500]}]) == 1
    assert calls == [
        ("get_desktop_state", {}),
        ("get_screen_size", {}),
        ("click", {"x": 100, "y": 50, "scope": "desktop", "button": "left", "count": 1}),
    ]


def test_mouse_actions_without_coordinates_use_current_cursor(monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "linux")
    calls = []

    def call(tool, arguments=None):
        calls.append((tool, arguments))
        if tool == "get_cursor_position":
            return {"x": 80, "y": 30}
        if tool == "get_screen_size":
            return {"width": 200, "height": 100}
        return {}

    monkeypatch.setattr(gui, "_call", call)
    assert (
        gui.dispatch_actions(
            [{"type": "click"}, {"type": "scroll", "direction": "down", "amount": 3}]
        )
        == 2
    )
    performed = [(name, args) for name, args in calls if name in {"click", "scroll"}]
    assert [name for name, _ in performed] == ["click", "scroll"]
    assert all(args["x"] == 80 and args["y"] == 30 for _, args in performed)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("action", ["click", "move", "scroll", "drag"])
@pytest.mark.parametrize("backing", [1, 2])
def test_desktop_coordinates_match_each_driver_contract(monkeypatch, platform, action, backing):
    calls = []

    def call(tool, arguments=None):
        calls.append((tool, arguments))
        if tool == "get_screen_size":
            return {"width": 1024, "height": 768, "scale_factor": 1.0}
        if tool == "get_desktop_state":
            return {"screenshot_width": 1024 * backing, "screenshot_height": 768 * backing}
        if tool == "get_cursor_position":
            return {"x": 256, "y": 192}
        return {}

    monkeypatch.setattr(gui.sys, "platform", platform)
    monkeypatch.setattr(gui, "_call", call)
    gui.dispatch_actions([{"type": action, "coordinate": [250, 250], "to": [750, 750]}])
    args = calls[-1][1]
    size = backing if platform == "darwin" else 1
    if action == "drag":
        assert (args["from_x"], args["from_y"]) == (256 * size, 192 * size)
        assert (args["to_x"], args["to_y"]) == (768 * size, 576 * size)
    else:
        assert (args["x"], args["y"]) == (256 * size, 192 * size)
    assert gui.cursor_position() == (250, 250)
    if platform != "darwin":
        assert all(tool != "get_desktop_state" for tool, _ in calls)


@pytest.mark.parametrize("action", ["click", "scroll", "drag"])
def test_macos_current_cursor_is_converted_to_screenshot_pixels(monkeypatch, action):
    calls = []

    def call(tool, arguments=None):
        calls.append((tool, arguments))
        return {
            "get_screen_size": {"width": 1024, "height": 768},
            "get_desktop_state": {"screenshot_width": 2048, "screenshot_height": 1536},
            "get_cursor_position": {"x": 256, "y": 192},
        }.get(tool, {})

    monkeypatch.setattr(gui.sys, "platform", "darwin")
    monkeypatch.setattr(gui, "_call", call)
    gui.dispatch_actions([{"type": action, "to": [750, 750]}])
    args = calls[-1][1]
    keys = ("from_x", "from_y") if action == "drag" else ("x", "y")
    assert tuple(args[k] for k in keys) == (512, 384)


@pytest.mark.parametrize("width", [None, 0, -1, True, "2048"])
def test_macos_refuses_unknown_screenshot_geometry(monkeypatch, width):
    calls = []

    def call(tool, arguments=None):
        calls.append(tool)
        return {"screenshot_width": width, "screenshot_height": 1536}

    monkeypatch.setattr(gui.sys, "platform", "darwin")
    monkeypatch.setattr(gui, "_call", call)
    with pytest.raises(gui.GuiUnavailable, match="screenshot dimensions"):
        gui.dispatch_actions([{"type": "click", "coordinate": [500, 500]}])
    assert calls == ["get_desktop_state"]


@pytest.mark.parametrize("duration,seconds", [(None, 0.5), (0, 0), (1250, 1.25)])
def test_wait_preserves_explicit_duration(monkeypatch, duration, seconds):
    sleeps = []
    monkeypatch.setattr(gui.time, "sleep", sleeps.append)
    gui.dispatch_actions([{"type": "wait", "duration_ms": duration}])
    assert sleeps == [seconds]


@pytest.mark.parametrize("stored", [False, True])
def test_screenshot_can_return_a_private_file_instead_of_inline_data(monkeypatch, tmp_path, stored):
    import hashlib
    import sys

    from ale.run.guestd.main import Handler

    png = b"\x89PNG\r\n\x1a\n" + b"pixels" * 800_000
    monkeypatch.setitem(sys.modules, "gui", gui)
    monkeypatch.setattr(gui, "capture_screen", lambda: png)
    path = tmp_path.resolve() / "screenshot.png"
    result = Handler(lambda event: pytest.fail("unexpected output event")).dispatch(
        1, "screenshot", {"path": str(path)} if stored else {}
    )["data"]
    if stored:
        assert result == {"bytes": len(png), "sha256": hashlib.sha256(png).hexdigest()}
        assert path.read_bytes() == png
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(FileExistsError):
            Handler(lambda event: None).dispatch(2, "screenshot", {"path": str(path)})
    else:
        assert base64.b64decode(result["png_b64"]) == png
        assert not path.exists()
