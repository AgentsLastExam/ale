"""AgentsLastExam's CUA desktop MCP contract, backed by ALE's local GUI layer."""

from __future__ import annotations

import base64
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any


def _load_gui() -> Any:
    beside = Path(__file__).resolve().parent / "gui.py"
    if beside.is_file():
        sys.path.insert(0, str(beside.parent))
        return importlib.import_module("gui")
    return importlib.import_module("ale.run.guestd.gui")


gui = _load_gui()

PROTOCOL_VERSION = "2024-11-05"
SERVER = {"name": "cua-desktop", "version": "0.3.0"}
JSON_SCHEMA = "http://json-schema.org/draft-07/schema#"


def _coordinate(description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 2,
        "maxItems": 2,
        "description": description,
    }


def _input(
    properties: dict[str, Any] | None = None,
    *,
    required: tuple[str, ...] = (),
    strict: bool = True,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = list(required)
    if strict:
        schema["additionalProperties"] = False
    schema["$schema"] = JSON_SCHEMA
    return schema


def _tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    *,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": name,
        "description": description,
        "inputSchema": input_schema,
        "execution": {"taskSupport": "forbidden"},
    }
    if output_schema is not None:
        tool["outputSchema"] = output_schema
    return tool


_KEY_NAMES = (
    "Valid key names: ctrl, shift, alt, cmd/meta, enter, esc, tab, space, backspace, "
    "delete, up, down, left, right, home, end, page_up, page_down, f1-f12, or any "
    "single character."
)

TOOLS: list[dict[str, Any]] = [
    _tool(
        "key",
        "On a desktop, press and release keys. For hotkeys pass multiple keys "
        f'(e.g. ["ctrl", "c"]). {_KEY_NAMES}',
        _input(
            {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of keys to press. Use lowercase names: ctrl, shift, alt, "
                        "enter, tab, etc."
                    ),
                }
            },
            required=("keys",),
        ),
    ),
    _tool(
        "key_down",
        f"On a desktop, press keys down without releasing them. Use with key_up to hold "
        f"modifiers. {_KEY_NAMES}",
        _input(
            {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of keys to press down. Use lowercase names: ctrl, shift, alt, etc."
                    ),
                }
            },
            required=("keys",),
        ),
    ),
    _tool(
        "key_up",
        f"On a desktop, release keys that were previously pressed down with key_down. {_KEY_NAMES}",
        _input(
            {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of keys to release. Use lowercase names: ctrl, shift, alt, etc."
                    ),
                }
            },
            required=("keys",),
        ),
    ),
    _tool(
        "type",
        "On a desktop, type text content into the currently focused input field.",
        _input(
            {"text": {"type": "string", "description": "The text content to type."}},
            required=("text",),
        ),
    ),
    _tool(
        "hold_key",
        f"On a desktop, hold keys down for a specified duration then release. {_KEY_NAMES}",
        _input(
            {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of keys to hold down. Use lowercase names: ctrl, shift, alt, etc."
                    ),
                },
                "duration": {"type": "number", "description": "Duration in seconds."},
            },
            required=("keys", "duration"),
        ),
    ),
    _tool(
        "mouse_move",
        "On a desktop, move the mouse cursor to specified coordinates.",
        _input(
            {"coordinate": _coordinate("(x, y) coordinates normalized to [0, 1000].")},
            required=("coordinate",),
        ),
    ),
    _tool(
        "click",
        "On a desktop, perform mouse click at specified coordinates.",
        _input(
            {
                "coordinate": _coordinate("(x, y) coordinates normalized to [0, 1000]."),
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "default": "left",
                    "description": "Mouse button to click.",
                },
                "clicks": {
                    "type": "number",
                    "enum": [1, 2, 3],
                    "default": 1,
                    "description": "Number of clicks: 1=single, 2=double, 3=triple.",
                },
            }
        ),
    ),
    _tool(
        "drag",
        "On a desktop, drag the mouse from start to end coordinates. Uses mouse_down + "
        "move_cursor + mouse_up for reliable cross-platform dragging.",
        _input(
            {
                "coordinate": _coordinate("Ending (x, y) coordinates, normalized to [0, 1000]."),
                "start_coordinate": _coordinate(
                    "Starting (x, y) coordinates, normalized to [0, 1000]."
                ),
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "default": "left",
                    "description": "Mouse button to hold while dragging.",
                },
            },
            required=("coordinate",),
        ),
    ),
    _tool(
        "mouse_down",
        "On a desktop, press the mouse button without releasing.",
        _input(
            {
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "default": "left",
                    "description": "Mouse button to press.",
                }
            }
        ),
    ),
    _tool(
        "mouse_up",
        "On a desktop, release the mouse button.",
        _input(
            {
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "default": "left",
                    "description": "Mouse button to release.",
                }
            }
        ),
    ),
    _tool(
        "scroll",
        "On a desktop, scroll in a specified direction by a specified amount.",
        _input(
            {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "description": "The direction to scroll.",
                },
                "amount": {"type": "number", "description": "Number of scroll units."},
                "coordinate": _coordinate(
                    "(x, y) coordinates. If provided, cursor moves here before scrolling."
                ),
            },
            required=("direction", "amount"),
        ),
    ),
    _tool(
        "wait",
        "On a desktop, pause execution for a specified duration.",
        _input(
            {"duration": {"type": "number", "description": "Time in seconds to wait."}},
            required=("duration",),
        ),
    ),
    _tool(
        "screenshot",
        "On a desktop, take a screenshot. Optionally save the image to a path on the VM.",
        _input(
            {
                "save_path": {
                    "type": "string",
                    "description": (
                        "Absolute file path on the VM to save the screenshot "
                        "(e.g. C:\\\\tmp\\\\shot.png). If omitted, the screenshot is "
                        "returned as base64 only without saving to disk."
                    ),
                }
            }
        ),
    ),
    _tool(
        "cursor_position",
        "On a desktop, get the current cursor position.",
        _input(strict=False),
    ),
    _tool(
        "get_screen_size",
        "On a desktop, get the screen size in absolute pixels. Coordinates for the other "
        "tools are normalized to [0, 1000]; this reports the underlying pixel dimensions "
        "for callers that work in pixel space.",
        _input(strict=False),
        output_schema={
            "type": "object",
            "properties": {
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
            "required": ["width", "height"],
            "additionalProperties": False,
            "$schema": JSON_SCHEMA,
        },
    ),
]

_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}

KEY_MAP = {
    "ARROWUP": "up",
    "ARROWDOWN": "down",
    "ARROWLEFT": "left",
    "ARROWRIGHT": "right",
    "ArrowUp": "up",
    "ArrowDown": "down",
    "ArrowLeft": "left",
    "ArrowRight": "right",
    "control": "ctrl",
    "Control": "ctrl",
    "CONTROL": "ctrl",
    "Ctrl": "ctrl",
    "CTRL": "ctrl",
    "Shift": "shift",
    "SHIFT": "shift",
    "Alt": "alt",
    "ALT": "alt",
    "option": "alt",
    "Option": "alt",
    "meta": "cmd",
    "Meta": "cmd",
    "command": "cmd",
    "Command": "cmd",
    "win": "cmd",
    "Win": "cmd",
    "super": "cmd",
    "Super": "cmd",
    "Enter": "enter",
    "ENTER": "enter",
    "Return": "enter",
    "return": "enter",
    "Escape": "esc",
    "escape": "esc",
    "ESC": "esc",
    "Space": "space",
    "SPACE": "space",
    "Tab": "tab",
    "TAB": "tab",
    "Backspace": "backspace",
    "BACKSPACE": "backspace",
    "Delete": "delete",
    "DELETE": "delete",
    "Home": "home",
    "End": "end",
    "PageUp": "page_up",
    "pageup": "page_up",
    "PageDown": "page_down",
    "pagedown": "page_down",
    "CapsLock": "caps_lock",
    "capslock": "caps_lock",
    "Insert": "insert",
    "PrintScreen": "print_screen",
}

XDOTOOL_KEYS = {
    "cmd": "super",
    "enter": "Return",
    "esc": "Escape",
    "page_up": "Page_Up",
    "page_down": "Page_Down",
    "caps_lock": "Caps_Lock",
    "print_screen": "Print",
    "backspace": "BackSpace",
    "delete": "Delete",
    "home": "Home",
    "end": "End",
    "insert": "Insert",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "tab": "Tab",
}


def _normalize_key(key: str) -> str:
    return KEY_MAP.get(key, key.lower())


def _backend_key(key: str) -> str:
    return XDOTOOL_KEYS.get(key, key)


def _text(label: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": label}]}


def _error(label: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": label}], "isError": True}


def _validate(arguments: Any, schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ValueError(f"unrecognized key(s): {', '.join(unknown)}")
    for name in schema.get("required", []):
        if name not in arguments:
            raise ValueError(f"required argument missing: {name}")
    for name, value in arguments.items():
        expected = properties.get(name, {})
        kind = expected.get("type")
        if kind == "string" and not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if kind == "number" and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise ValueError(f"{name} must be a number")
        if kind == "array":
            if not isinstance(value, list):
                raise ValueError(f"{name} must be an array")
            if len(value) < expected.get("minItems", 0) or len(value) > expected.get(
                "maxItems", len(value)
            ):
                raise ValueError(f"{name} has the wrong length")
            item_type = expected.get("items", {}).get("type")
            if item_type == "string" and not all(isinstance(item, str) for item in value):
                raise ValueError(f"{name} must contain strings")
            if item_type == "number" and not all(
                not isinstance(item, bool) and isinstance(item, int | float) for item in value
            ):
                raise ValueError(f"{name} must contain numbers")
        if "enum" in expected and value not in expected["enum"]:
            raise ValueError(f"{name} must be one of {expected['enum']}")
    return arguments


def _call(name: str, arguments: Any) -> dict[str, Any]:
    tool = _TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ValueError(f"no such tool: {name}")
    args = _validate(arguments, tool["inputSchema"])

    if name == "key":
        keys = [_normalize_key(key) for key in args["keys"]]
        gui.dispatch_actions([{"type": "key", "keys": [_backend_key(key) for key in keys]}])
        return _text(f"Pressed: {'+'.join(keys)}")

    if name in {"key_down", "key_up"}:
        keys = [_normalize_key(key) for key in args["keys"]]
        gui.dispatch_actions([{"type": name, "keys": [_backend_key(key) for key in keys]}])
        return _text(f"{'Key down' if name == 'key_down' else 'Key up'}: {'+'.join(keys)}")

    if name == "type":
        text = args["text"]
        gui.dispatch_actions([{"type": "type", "text": text}])
        preview = text[:50] + "..." if len(text) > 50 else text
        return _text(f'Typed: "{preview}"')

    if name == "hold_key":
        keys = [_normalize_key(key) for key in args["keys"]]
        backend = [_backend_key(key) for key in keys]
        gui.dispatch_actions([{"type": "key_down", "keys": backend}])
        time.sleep(args["duration"])
        gui.dispatch_actions([{"type": "key_up", "keys": list(reversed(backend))}])
        return _text(f"Held {'+'.join(keys)} for {args['duration']}s")

    if name == "mouse_move":
        coordinate = args["coordinate"]
        gui.dispatch_actions([{"type": "move", "coordinate": coordinate}])
        return _text(f"Moved cursor to {coordinate}")

    if name == "click":
        coordinate = args.get("coordinate")
        button = args.get("button", "left")
        clicks = args.get("clicks", 1)
        gui.dispatch_actions(
            [
                {
                    "type": "click",
                    "coordinate": coordinate,
                    "button": button,
                    "clicks": clicks,
                }
            ]
        )
        suffix = f" at {coordinate}" if coordinate is not None else ""
        return _text(f"Clicked ({button}, {clicks}x){suffix}")

    if name == "drag":
        coordinate = args["coordinate"]
        start = args.get("start_coordinate")
        button = args.get("button", "left")
        gui.dispatch_actions(
            [{"type": "drag", "coordinate": start, "to": coordinate, "button": button}]
        )
        return _text(
            f"Dragged ({button}) from {start if start is not None else 'current'} to {coordinate}"
        )

    if name in {"mouse_down", "mouse_up"}:
        button = args.get("button", "left")
        gui.dispatch_actions([{"type": name, "button": button}])
        return _text(f"{'Mouse down' if name == 'mouse_down' else 'Mouse up'}: {button}")

    if name == "scroll":
        coordinate = args.get("coordinate")
        actions: list[dict[str, Any]] = []
        if coordinate is not None:
            actions.append({"type": "move", "coordinate": coordinate})
        actions.append(
            {
                "type": "scroll",
                "direction": args["direction"],
                "amount": args["amount"],
            }
        )
        gui.dispatch_actions(actions)
        suffix = f" at {coordinate}" if coordinate is not None else ""
        return _text(f"Scrolled {args['direction']} {args['amount']}{suffix}")

    if name == "wait":
        time.sleep(args["duration"])
        return _text(f"Waited {args['duration']}s")

    if name == "screenshot":
        save_path = args.get("save_path")
        if save_path is not None:
            last_sep = max(save_path.rfind("/"), save_path.rfind("\\"))
            if last_sep <= 0:
                return _error(
                    f'Error: invalid save_path "{save_path}" — must be an absolute path '
                    "with a parent directory."
                )
            parent = save_path[:last_sep]
            if not Path(parent).is_dir():
                return _error(f'Error: parent directory "{parent}" does not exist on the VM.')
        png = gui.capture_screen()
        encoded = base64.b64encode(png).decode("ascii")
        if save_path is not None:
            Path(save_path).write_bytes(png)
            label = f"Screenshot captured and saved to {save_path}"
        else:
            label = "Screenshot captured"
        return {
            "content": [
                {"type": "text", "text": label},
                {"type": "image", "data": encoded, "mimeType": "image/png"},
            ]
        }

    if name == "cursor_position":
        x, y = gui.cursor_position()
        return _text(f"Cursor at [{x}, {y}]")

    if name == "get_screen_size":
        width, height = gui.screen_size()
        return {
            "content": [{"type": "text", "text": f"Screen size: {width}x{height}"}],
            "structuredContent": {"width": width, "height": height},
        }

    raise ValueError(f"no such tool: {name}")


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": SERVER,
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        try:
            result = _call(str(params.get("name")), params.get("arguments") or {})
        except Exception as exc:
            result = _error(str(exc))
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"unsupported method: {method}"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = _handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
