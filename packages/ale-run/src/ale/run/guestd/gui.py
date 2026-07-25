"""Desktop capture and input for GUI images.

Imported lazily by the guest service, so headless images never need these tools
installed. Everything shells out to X utilities that the GUI base image provides
(``scrot``/``import`` for capture, ``xdotool`` for input) — no Python GUI stack, in
keeping with the standard-library-only rule.

Coordinates arrive in a normalised [0, 1000] space so a recorded trajectory stays
meaningful across resolutions; they are mapped to pixels here, at the last moment.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

COORDINATE_SPACE = 1000


class GuiUnavailable(RuntimeError):
    """The image has no desktop, or lacks the tools to drive one."""


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise GuiUnavailable(f"{tool} is not installed in this image")
    return path


def screen_size() -> tuple[int, int]:
    """Return the desktop size in pixels."""
    xdotool = _require("xdotool")
    out = subprocess.run(
        [xdotool, "getdisplaygeometry"], capture_output=True, text=True, check=True
    ).stdout
    width, height = out.split()
    return int(width), int(height)


def _to_pixels(coordinate: list[int] | tuple[int, int]) -> tuple[int, int]:
    width, height = screen_size()
    x, y = coordinate
    return round(x * width / COORDINATE_SPACE), round(y * height / COORDINATE_SPACE)


def capture_screen() -> bytes:
    """Capture the desktop as PNG bytes."""
    for tool, argv in (
        ("scrot", ["-o", "-z"]),
        ("import", ["-window", "root"]),
    ):
        path = shutil.which(tool)
        if path is None:
            continue
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            subprocess.run([path, *argv, tmp.name], check=True, capture_output=True)
            return Path(tmp.name).read_bytes()
    raise GuiUnavailable("no screenshot tool available (need scrot or ImageMagick import)")


def dispatch_actions(actions: list[dict[str, Any]]) -> int:
    """Perform desktop actions; returns how many were applied."""
    xdotool = _require("xdotool")
    applied = 0
    for action in actions:
        kind = action.get("type")
        if kind in {"click", "double_click", "right_click", "move"}:
            if coordinate := action.get("coordinate"):
                x, y = _to_pixels(coordinate)
                subprocess.run([xdotool, "mousemove", str(x), str(y)], check=True)
            if kind == "click":
                subprocess.run([xdotool, "click", "1"], check=True)
            elif kind == "double_click":
                subprocess.run([xdotool, "click", "--repeat", "2", "1"], check=True)
            elif kind == "right_click":
                subprocess.run([xdotool, "click", "3"], check=True)
        elif kind == "drag":
            start, end = action.get("coordinate"), action.get("to")
            if not start or not end:
                continue
            sx, sy = _to_pixels(start)
            ex, ey = _to_pixels(end)
            subprocess.run([xdotool, "mousemove", str(sx), str(sy), "mousedown", "1"], check=True)
            subprocess.run([xdotool, "mousemove", str(ex), str(ey), "mouseup", "1"], check=True)
        elif kind == "scroll":
            button = {"up": "4", "down": "5", "left": "6", "right": "7"}.get(
                str(action.get("direction", "down")), "5"
            )
            amount = int(action.get("amount") or 3)
            subprocess.run([xdotool, "click", "--repeat", str(amount), button], check=True)
        elif kind == "type":
            subprocess.run([xdotool, "type", "--delay", "12", action.get("text") or ""], check=True)
        elif kind == "key":
            keys = action.get("keys") or []
            if keys:
                subprocess.run([xdotool, "key", "+".join(keys)], check=True)
        elif kind == "wait":
            import time

            time.sleep((action.get("duration_ms") or 500) / 1000)
        else:
            continue
        applied += 1
    return applied
