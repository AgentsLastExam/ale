"""A desktop the agent can drive, exposed as MCP tools.

This file runs **inside the sandbox**, as the agent, launched by the agent's own CLI. It
speaks MCP over stdin and stdout and performs each action locally, through the same
``gui`` module the guest service uses — staged beside this one.

Three choices, each with a plausible alternative that was rejected:

* **In the sandbox, not on the host.** The alternative is a server on the host that the
  agent reaches over the network, which would mean a second permitted destination through
  a boundary whose whole value is that it has exactly one. Acting locally needs no egress
  at all.
* **The standard library only.** The reference implementation is Node and is rebuilt with
  ``npm install`` wherever it lands; that is a network dependency and a build step inside
  something meant to be hermetic. Every sandbox image already has one Python interpreter,
  which is all this needs.
* **The same action layer as the stepwise path.** Two implementations of "click at these
  coordinates" drift, and the drift shows up as a task that scores differently depending
  on which family of agent attempted it. Coordinates are normalised to [0, 1000] here for
  the same reason they are there: a trajectory stays meaningful across resolutions.

MCP is JSON-RPC 2.0 over a line-delimited stream. Only the three methods a client needs
are implemented — ``initialize``, ``tools/list``, ``tools/call`` — because a partial
implementation that says so is easier to reason about than a general one that is subtly
incomplete.
"""

from __future__ import annotations

import base64
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any


def _load_gui() -> Any:
    """The action layer, wherever this file happens to be running from.

    Staged into a sandbox it sits beside a copy of ``gui.py`` and there is no package
    around it; imported from the source tree it is part of ``ale.run``. Both are real
    situations — the second is how it is tested — so both are supported here rather than
    by keeping two copies of the file in step.
    """
    beside = Path(__file__).resolve().parent / "gui.py"
    if beside.is_file():
        sys.path.insert(0, str(beside.parent))
        return importlib.import_module("gui")
    return importlib.import_module("ale.run.guestd.gui")


gui = _load_gui()

PROTOCOL_VERSION = "2024-11-05"
SERVER = {"name": "ale-desktop", "version": "0.1.0"}

#: One entry per tool: what the model is told, and what argument shape is accepted.
#: Declarative so the schema the client sees and the code that runs cannot disagree.
COORDINATE = {
    "type": "array",
    "items": {"type": "integer"},
    "minItems": 2,
    "maxItems": 2,
    "description": "(x, y), normalised to [0, 1000] with [0, 0] at the top left.",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "screenshot",
        "description": "Capture the desktop and return it as an image.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "click",
        "description": (
            "Click at a point on the desktop. Omit the coordinate to click where the "
            "cursor already is."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "coordinate": COORDINATE,
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer", "minimum": 1, "maximum": 3},
            },
        },
    },
    {
        "name": "mouse_move",
        "description": "Move the cursor without pressing anything.",
        "inputSchema": {
            "type": "object",
            "properties": {"coordinate": COORDINATE},
            "required": ["coordinate"],
        },
    },
    {
        "name": "drag",
        "description": "Press at one point, move to another, release.",
        "inputSchema": {
            "type": "object",
            "properties": {"start_coordinate": COORDINATE, "coordinate": COORDINATE},
            "required": ["coordinate"],
        },
    },
    {
        "name": "type",
        "description": "Type text into whatever currently has focus.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "key",
        "description": (
            'Press keys together, e.g. ["ctrl", "c"]. Names: ctrl, shift, alt, super, '
            "enter, esc, tab, space, backspace, delete, up, down, left, right, home, end, "
            "page_up, page_down, f1-f12, or a single character."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"keys": {"type": "array", "items": {"type": "string"}}},
            "required": ["keys"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll in a direction, optionally after moving to a point first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "amount": {"type": "integer", "minimum": 1},
                "coordinate": COORDINATE,
            },
            "required": ["direction"],
        },
    },
    {
        "name": "wait",
        "description": "Pause, to let the desktop finish reacting to the last action.",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        },
    },
    {
        "name": "get_screen_size",
        "description": (
            "The desktop's size in real pixels. The other tools take normalised "
            "coordinates; this is for callers that need the underlying resolution."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

#: Names a model is likely to use for keys, mapped to what xdotool expects. Left as-is
#: when unrecognised: any single character is already a valid key name.
KEY_ALIASES = {
    "control": "ctrl",
    "cmd": "super",
    "command": "super",
    "meta": "super",
    "win": "super",
    "option": "alt",
    "arrowup": "Up",
    "arrowdown": "Down",
    "arrowleft": "Left",
    "arrowright": "Right",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    "delete": "Delete",
    "backspace": "BackSpace",
    "space": "space",
    "tab": "Tab",
}


def _key(name: str) -> str:
    return KEY_ALIASES.get(name.strip().lower(), name)


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool and describe what happened.

    Every reply says what was done rather than "ok": a model that cannot see the effect
    of its own action re-issues it, and a screenshot per action would cost more than the
    sentence does.
    """
    if name == "screenshot":
        png = gui.capture_screen()
        return {
            "content": [
                {"type": "text", "text": "Captured the desktop."},
                {
                    "type": "image",
                    "data": base64.b64encode(png).decode("ascii"),
                    "mimeType": "image/png",
                },
            ]
        }

    if name == "get_screen_size":
        width, height = gui.screen_size()
        return _text(f"The desktop is {width}x{height} pixels.")

    if name == "wait":
        seconds = float(args.get("seconds") or 0.5)
        time.sleep(min(seconds, 30))  # a tool call is not a place to sleep for a minute
        return _text(f"Waited {seconds:g}s.")

    if name == "click":
        coordinate = args.get("coordinate")
        button = str(args.get("button") or "left")
        clicks = int(args.get("clicks") or 1)
        kind = {"left": "click", "right": "right_click", "middle": "click"}[button]
        if clicks == 2 and button == "left":
            kind = "double_click"
            clicks = 1
        actions = [{"type": kind, "coordinate": coordinate} for _ in range(clicks)]
        gui.dispatch_actions(actions)
        where = f" at {coordinate}" if coordinate else " where the cursor was"
        return _text(f"Clicked ({button}, {len(actions)}x){where}.")

    if name == "mouse_move":
        gui.dispatch_actions([{"type": "move", "coordinate": args["coordinate"]}])
        return _text(f"Moved the cursor to {args['coordinate']}.")

    if name == "drag":
        start = args.get("start_coordinate")
        end = args["coordinate"]
        gui.dispatch_actions([{"type": "drag", "coordinate": start or end, "to": end}])
        return _text(f"Dragged from {start or 'the cursor'} to {end}.")

    if name == "type":
        gui.dispatch_actions([{"type": "type", "text": args.get("text") or ""}])
        text = args.get("text") or ""
        preview = text if len(text) <= 50 else text[:50] + "…"
        return _text(f'Typed "{preview}".')

    if name == "key":
        keys = [_key(key) for key in args.get("keys") or []]
        if not keys:
            raise ValueError("no keys were given")
        gui.dispatch_actions([{"type": "key", "keys": keys}])
        return _text(f"Pressed {'+'.join(keys)}.")

    if name == "scroll":
        actions: list[dict[str, Any]] = []
        if coordinate := args.get("coordinate"):
            actions.append({"type": "move", "coordinate": coordinate})
        direction = str(args.get("direction") or "down")
        amount = int(args.get("amount") or 3)
        actions.append({"type": "scroll", "direction": direction, "amount": amount})
        gui.dispatch_actions(actions)
        return _text(f"Scrolled {direction} by {amount}.")

    raise ValueError(f"no such tool: {name}")


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """One request in, one reply out — or none, for a notification."""
    method = message.get("method")
    request_id = message.get("id")

    if request_id is None:
        return None  # a notification; MCP forbids replying to one

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER,
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = message.get("params") or {}
        try:
            result = _call(str(params.get("name")), params.get("arguments") or {})
        except Exception as exc:
            # An error the model can read and act on. Returned as a result rather than a
            # protocol error, because a failed click is the agent's problem to solve and
            # a transport fault would end the session instead.
            result = {"content": [{"type": "text", "text": f"{exc}"}], "isError": True}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"unsupported method: {method}"},
    }


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue  # a client that sends garbage should not take the desktop down
        reply = _handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
