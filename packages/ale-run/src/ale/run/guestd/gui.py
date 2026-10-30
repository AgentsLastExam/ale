"""Cross-platform desktop capture and input through Cua Driver."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any

COORDINATE_SPACE = 1000
#: Where guestd's Cua Driver server listens on Linux.
_LINUX_SOCKET = "/tmp/ale-cua-driver.sock"
#: The pipe the Windows boot service (``service.py``) has Cua Driver serve on.
_WINDOWS_PIPE = r"\\.\pipe\ale-cua-driver"
#: Where the framework installs Cua Driver on Windows; a Linux image has it on PATH.
_WINDOWS_DRIVER = os.path.join(
    os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "ALE", "cua-driver", "cua-driver.exe"
)
#: The alias systemd links when a display manager (GDM, LightDM, SDDM) is enabled.
_DISPLAY_MANAGER = "/etc/systemd/system/display-manager.service"
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


class ActionFailed(GuiUnavailable):
    """Cua Driver ran the tool and reported that it failed."""


def _binary() -> str:
    configured = os.environ.get("ALE_CUA_DRIVER")
    if not configured and os.name == "nt" and os.path.isfile(_WINDOWS_DRIVER):
        configured = _WINDOWS_DRIVER
    found = configured or shutil.which("cua-driver") or shutil.which("cua-driver.exe")
    if not found:
        raise GuiUnavailable("cua-driver is not installed in this image")
    return found


def has_desktop() -> bool:
    """Whether this sandbox is configured to show a desktop, decided before anything starts.

    A virtual machine has one when its systemd enables a display manager. A container has
    one when its image sets ``DISPLAY`` or ``WAYLAND_DISPLAY``: that is how a desktop image
    tells its processes where to draw, while an image that ships X only as a tool, such as
    Xvfb for a headless browser, does not. Windows always draws one, and it is ALE's to
    drive once the framework has installed Cua Driver, as in every Task image; the bare
    base, booted only to build a Task image, has none to wait for.
    """
    if os.name == "nt":
        return os.path.isfile(_WINDOWS_DRIVER)
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) or os.path.lexists(
        _DISPLAY_MANAGER
    )


def _call(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """The tool's structured result, or ``{}`` when it answers with text alone."""
    structured = _result(tool, arguments).get("structuredContent")
    return structured if isinstance(structured, dict) else {}


def _result(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return _invoke(tool, arguments)
    except ActionFailed:
        raise
    except GuiUnavailable:
        if not _start_server():
            raise
        return _invoke(tool, arguments)


def _invoke(tool: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """One tool call through the driver's MCP proxy; the MCP result of a tool that succeeded.

    ``cua-driver call`` exits 0 whatever the tool says and prints its text when there is no
    structured result, which some successes (a scroll) and every failure are; MCP marks a
    failure with ``isError``.
    """
    command = [_binary(), "mcp"]
    socket = _socket()
    if socket:
        command += ["--socket", socket]
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ale-guestd", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        },
    ]
    try:
        result = subprocess.run(
            command,
            input="".join(json.dumps(message) + "\n" for message in messages),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            **_as_daemon_owner(socket),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuiUnavailable(f"cua-driver {tool} failed: {exc}") from exc
    answer = next((reply for reply in _replies(result.stdout) if reply.get("id") == 1), None)
    if answer is None or not isinstance(answer.get("result"), dict):
        problem = (answer or {}).get("error") or result.stderr.strip()[-300:] or result.returncode
        raise GuiUnavailable(f"cua-driver {tool} failed: {problem}")
    outcome = answer["result"]
    if outcome.get("isError"):
        said = " ".join(
            str(part.get("text", ""))
            for part in outcome.get("content") or []
            if isinstance(part, dict) and part.get("type") == "text"
        )
        raise ActionFailed(f"cua-driver {tool} failed: {said.strip() or 'no reason given'}")
    return outcome


def _as_daemon_owner(socket: str | None) -> dict[str, Any]:
    """Run the client as the account that owns the daemon's socket, usually the agent's.

    Cua Driver answers only its own account, and guestd runs as root on Linux.
    """
    if os.name == "nt" or not socket or os.geteuid() != 0:
        return {}
    try:
        owner = os.stat(socket).st_uid
    except OSError:
        return {}  # no daemon yet: the call fails, and guestd starts one
    if owner == 0:
        return {}
    import pwd

    account = pwd.getpwuid(owner)
    env = dict(os.environ, HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
    return {"env": env, "preexec_fn": _demote(account)}


def _replies(output: str) -> list[dict[str, Any]]:
    replies = []
    for line in output.splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            reply = json.loads(line)
            if isinstance(reply, dict):
                replies.append(reply)
    return replies


def _socket() -> str | None:
    configured = os.environ.get("ALE_CUA_DRIVER_SOCKET")
    return configured or (_WINDOWS_PIPE if os.name == "nt" else _LINUX_SOCKET)


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
    session = _session_environment(account)
    if session is None:
        raise GuiUnavailable("the agent account's desktop session is still starting")
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
            preexec_fn=_demote(account),
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


#: Run as the agent account, to which it drops itself: reading another account's process
#: environment needs CAP_SYS_PTRACE, which a container's root does not have, but an account
#: reads its own.
#: It also reports whether the session has reached its window manager or shell, because an
#: X server answers screenshots long before the desktop the agent will use is drawn.
_SESSION_SCAN = """
import json, os, pwd, sys
if os.geteuid() == 0:
    # Everything is imported first: the interpreter's own files are root's alone.
    account = pwd.getpwnam(sys.argv[1])
    os.initgroups(account.pw_name, account.pw_gid)
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
shells = set(sys.argv[2].split(","))
keys = set(sys.argv[3:])
best, shell = None, False
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    try:
        if os.stat("/proc/" + name).st_uid != os.getuid():
            continue
        with open("/proc/" + name + "/comm") as handle:
            shell = shell or handle.read().strip() in shells
        with open("/proc/" + name + "/environ", "rb") as handle:
            raw = handle.read()
    except OSError:
        continue
    env = dict(i.split("=", 1) for i in raw.decode("utf-8", "replace").split("\\0") if "=" in i)
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        continue
    score = sum(key in env for key in keys)
    if best is None or score > best[0]:
        best = (score, env)
print(json.dumps(best[1] if best and shell else None))
"""
#: Window managers and desktop shells; one of them running means the desktop is drawn.
_SHELLS = (
    "gnome-shell",
    "mutter",
    "xfwm4",
    "xfce4-session",
    "kwin_x11",
    "kwin_wayland",
    "plasmashell",
    "openbox",
    "fluxbox",
    "i3",
    "sway",
    "marco",
    "metacity",
    "muffin",
    "cinnamon",
    "budgie-wm",
    "icewm",
    "fvwm",
)


def _session_environment(account: Any) -> dict[str, str] | None:
    """The environment of the account's desktop session, read from its own processes."""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _SESSION_SCAN,
                account.pw_name,
                ",".join(_SHELLS),
                *_SESSION_KEYS,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        found = json.loads(result.stdout or "null")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return found if isinstance(found, dict) else None


def _demote(account: Any):  # type: ignore[no-untyped-def]
    def demote() -> None:
        os.setgid(account.pw_gid)
        os.initgroups(account.pw_name, account.pw_gid)
        os.setuid(account.pw_uid)

    return demote


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
    for part in _result("get_desktop_state").get("content") or []:
        if isinstance(part, dict) and part.get("type") == "image":
            try:
                return base64.b64decode(str(part.get("data")), validate=True)
            except ValueError as exc:
                raise GuiUnavailable("cua-driver returned an invalid screenshot") from exc
    raise GuiUnavailable("cua-driver get_desktop_state returned no screenshot")


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
