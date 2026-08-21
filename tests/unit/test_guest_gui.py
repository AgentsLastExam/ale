from __future__ import annotations

import base64
import json
import subprocess

import pytest

from ale.run.guestd import gui

pytestmark = pytest.mark.unit


def test_cua_driver_is_the_only_gui_backend(monkeypatch: pytest.MonkeyPatch) -> None:
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
