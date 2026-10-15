"""Cross-platform desktop capture and input through Cua Driver."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import subprocess
import time
from typing import Any

COORDINATE_SPACE = 1000
#: Where guestd's Cua Driver server listens on Linux.
_LINUX_SOCKET = "/tmp/ale-cua-driver.sock"
#: Programs that put a desktop on screen; an image with none of them is headless.
_DISPLAY_SERVERS = ("Xvnc", "Xvfb", "Xorg", "Xwayland", "gnome-shell", "gdm3", "lightdm", "sddm")
_PROGRAM_DIRS = ("/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/libexec", "/bin", "/sbin")
#: What Cua Driver takes from the session it drives.
_SESSION_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
)
_server: subprocess.Popen[bytes] | None = None


class GuiUnavailable(RuntimeError):
    """The image has no reachable Cua Driver desktop service."""


def _binary() -> str:
    configured = os.environ.get("ALE_CUA_DRIVER")
    found = configured or shutil.which("cua-driver") or shutil.which("cua-driver.exe")
    if not found:
        raise GuiUnavailable("cua-driver is not installed in this image")
    return found


def has_desktop() -> bool:
    """Whether this image can put a desktop on screen at all.

    Decided from the programs the image ships, so a headless image is known to be headless
    at once instead of after waiting for a screen that will never appear.
    """
    if os.name == "nt":
        return True
    return any(
        os.path.exists(os.path.join(directory, name))
        for directory in _PROGRAM_DIRS
        for name in _DISPLAY_SERVERS
    )


def _call(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return _invoke(tool, arguments)
    except GuiUnavailable:
        if not _start_server():
            raise
        return _invoke(tool, arguments)


def _invoke(tool: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    command = [_binary(), "call", tool, json.dumps(arguments or {}, separators=(",", ":"))]
    if socket := _socket():
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
        detail = (result.stdout.strip() or result.stderr.strip())[:300]
        raise GuiUnavailable(f"cua-driver {tool} returned invalid JSON: {detail!r}") from exc
    if not isinstance(payload, dict):
        raise GuiUnavailable(f"cua-driver {tool} returned a non-object result")
    return payload


def _socket() -> str | None:
    configured = os.environ.get("ALE_CUA_DRIVER_SOCKET")
    return configured or (None if os.name == "nt" else _LINUX_SOCKET)


def _start_server() -> bool:
    """Start Cua Driver inside the agent's desktop session; True if one was started.

    The image only has to run a desktop: the engine installed Cua Driver, and guestd
    starts it as the agent account with the environment of that account's session, so
    capture and input reach the screen the agent sees. Windows starts it with the session.
    """
    global _server
    if os.name == "nt" or os.environ.get("ALE_CUA_DRIVER_SOCKET"):
        return False
    if _server is not None and _server.poll() is None:
        return False
    import pwd

    try:
        account = pwd.getpwnam(os.environ.get("ALE_AGENT_USER") or "user")
    except KeyError as exc:
        raise GuiUnavailable("the agent account does not exist in this image") from exc
    session = _session_environment(account.pw_uid)
    if session is None:
        raise GuiUnavailable("no desktop session is running for the agent account yet")
    env = {key: session[key] for key in _SESSION_KEYS if key in session}
    env.update(
        HOME=account.pw_dir,
        USER=account.pw_name,
        LOGNAME=account.pw_name,
        PATH=session.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
    )
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        try:
            with open("/tmp/dbus-session-bus-address") as handle:
                if address := handle.read().strip():
                    env["DBUS_SESSION_BUS_ADDRESS"] = address
        except OSError:
            pass

    def demote() -> None:
        os.setgid(account.pw_gid)
        os.initgroups(account.pw_name, account.pw_gid)
        os.setuid(account.pw_uid)

    with contextlib.suppress(FileNotFoundError):
        os.unlink(_LINUX_SOCKET)
    try:
        log = open("/tmp/ale-cua-driver.log", "ab")  # noqa: SIM115 - handed to the child
        _server = subprocess.Popen(
            [
                _binary(),
                "serve",
                "--socket",
                _LINUX_SOCKET,
                "--permission-mode",
                "unrestricted",
                "--dangerously-bypass-approvals",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            preexec_fn=demote,
        )
        log.close()
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuiUnavailable(f"cua-driver could not be started: {exc}") from exc
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _server.poll() is None:
        if os.path.exists(_LINUX_SOCKET):
            return True
        time.sleep(0.2)
    raise GuiUnavailable("cua-driver did not start in the desktop session")


def _session_environment(uid: int) -> dict[str, str] | None:
    """The environment of the account's desktop session, read from its own processes."""
    best: tuple[int, dict[str, str]] | None = None
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            with open(f"/proc/{entry.name}/environ", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        env = dict(
            item.split("=", 1) for item in raw.decode("utf-8", "replace").split("\0") if "=" in item
        )
        if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
            continue
        score = sum(key in env for key in _SESSION_KEYS)
        if best is None or score > best[0]:
            best = (score, env)
    return best[1] if best else None


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
