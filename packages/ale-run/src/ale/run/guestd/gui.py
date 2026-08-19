"""Cross-platform desktop capture and input through Cua Driver."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from typing import Any

COORDINATE_SPACE = 1000


class GuiUnavailable(RuntimeError):
    """The image has no reachable Cua Driver desktop service."""


def _binary() -> str:
    configured = os.environ.get("ALE_CUA_DRIVER")
    found = configured or shutil.which("cua-driver") or shutil.which("cua-driver.exe")
    if not found:
        raise GuiUnavailable("cua-driver is not installed in this image")
    return found


def _call(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    command = [_binary(), "call", tool, json.dumps(arguments or {}, separators=(",", ":"))]
    if socket := os.environ.get("ALE_CUA_DRIVER_SOCKET"):
        command += ["--socket", socket]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuiUnavailable(f"cua-driver {tool} failed: {exc}") from exc
    if result.returncode != 0:
        raise GuiUnavailable(
            f"cua-driver {tool} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GuiUnavailable(f"cua-driver {tool} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise GuiUnavailable(f"cua-driver {tool} returned a non-object result")
    return payload


def available() -> bool:
    try:
        screen_size()
    except GuiUnavailable:
        return False
    return True


def screen_size() -> tuple[int, int]:
    payload = _call("get_screen_size")
    try:
        return int(payload["width"]), int(payload["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GuiUnavailable("cua-driver get_screen_size omitted width or height") from exc


def _to_pixels(coordinate: list[int] | tuple[int, int]) -> tuple[int, int]:
    width, height = screen_size()
    x, y = coordinate
    return round(x * width / COORDINATE_SPACE), round(y * height / COORDINATE_SPACE)


def cursor_position() -> tuple[int, int]:
    payload = _call("get_cursor_position")
    width, height = screen_size()
    try:
        return (
            round(float(payload["x"]) * COORDINATE_SPACE / width),
            round(float(payload["y"]) * COORDINATE_SPACE / height),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GuiUnavailable("cua-driver did not report a cursor position") from exc


def capture_screen() -> bytes:
    payload = _call("get_desktop_state")
    encoded = payload.get("screenshot_png_b64")
    if not isinstance(encoded, str):
        raise GuiUnavailable("cua-driver get_desktop_state returned no screenshot")
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise GuiUnavailable("cua-driver returned an invalid screenshot") from exc


def _point(action: dict[str, Any], key: str = "coordinate") -> tuple[int, int]:
    coordinate = action.get(key)
    if not isinstance(coordinate, (list, tuple)) or len(coordinate) != 2:
        raise GuiUnavailable(f"desktop action requires a two-value {key}")
    return _to_pixels(coordinate)


def dispatch_actions(actions: list[dict[str, Any]]) -> int:
    applied = 0
    for action in actions:
        kind = action.get("type")
        if kind in {"click", "double_click", "right_click"}:
            x, y = _point(action)
            _call(
                "click",
                {
                    "x": x,
                    "y": y,
                    "scope": "desktop",
                    "button": "right" if kind == "right_click" else action.get("button", "left"),
                    "count": 2 if kind == "double_click" else int(action.get("clicks") or 1),
                },
            )
        elif kind == "move":
            x, y = _point(action)
            _call("move_cursor", {"x": x, "y": y, "scope": "desktop"})
        elif kind == "drag":
            end_x, end_y = _point(action, "to")
            start = action.get("coordinate")
            start_x, start_y = _to_pixels(start) if start else cursor_position()
            _call(
                "drag",
                {
                    "from_x": start_x,
                    "from_y": start_y,
                    "to_x": end_x,
                    "to_y": end_y,
                    "scope": "desktop",
                    "button": action.get("button", "left"),
                },
            )
        elif kind == "scroll":
            coordinate = action.get("coordinate")
            x, y = _to_pixels(coordinate) if coordinate else cursor_position()
            _call(
                "scroll",
                {
                    "x": x,
                    "y": y,
                    "direction": action.get("direction", "down"),
                    "amount": max(1, min(50, int(action.get("amount") or 3))),
                    "scope": "desktop",
                },
            )
        elif kind == "type":
            _call("type_text", {"text": action.get("text") or "", "scope": "desktop"})
        elif kind == "key":
            keys = [str(key) for key in action.get("keys") or []]
            if len(keys) > 1:
                _call("hotkey", {"keys": keys, "scope": "desktop"})
            elif keys:
                _call("press_key", {"key": keys[0], "scope": "desktop"})
        elif kind == "wait":
            time.sleep((action.get("duration_ms") or 500) / 1000)
        elif kind in {"mouse_down", "mouse_up", "key_down", "key_up"}:
            raise GuiUnavailable(f"cua-driver does not expose the stateful action {kind}")
        else:
            continue
        applied += 1
    return applied
