import json
import subprocess
import time
from pathlib import Path

DRIVER = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"


def call(name, args):
    p = subprocess.run(
        [DRIVER, "call", name, json.dumps(args), "--socket", "/tmp/ale-cua-driver.sock"],
        capture_output=True,
        text=True,
    )
    result = json.loads(p.stdout) if p.stdout.strip() else {}
    if p.returncode or result.get("isError") or result.get("error"):
        raise RuntimeError(f"Driver {name} failed: {p.stdout or p.stderr}")
    return result


def document(title, timeout=30):
    """The open document's window, once TextEdit shows it."""
    deadline = time.monotonic() + timeout
    while True:
        for window in call("list_windows", {}).get("windows", []):
            if window.get("title") == title and window.get("is_on_screen"):
                return window
        if time.monotonic() > deadline:
            raise RuntimeError(f"TextEdit shows no {title} window")
        time.sleep(0.5)


call("get_desktop_state", {"screenshot_out_file": "/Users/admin/output/before.png"})
# A window in front does not hold the keyboard until its document is clicked. The image
# restores a Terminal window, so the click goes to the middle of the document's own
# window once TextEdit shows it; window bounds are in points, desktop clicks in pixels.
window = document("result.txt")
bounds = window["bounds"]
state = call(
    "get_window_state",
    {"pid": window["pid"], "window_id": window["window_id"], "include_accessibility_tree": False},
)
scale = state["screenshot_scale"]
call(
    "click",
    {
        "x": round((bounds["x"] + bounds["width"] / 2) * scale),
        "y": round((bounds["y"] + bounds["height"] / 2) * scale),
        "scope": "desktop",
    },
)
time.sleep(1)
call("hotkey", {"scope": "desktop", "keys": ["cmd", "a"]})
call("type_text", {"scope": "desktop", "text": "ALE desktop verified"})
call("hotkey", {"scope": "desktop", "keys": ["cmd", "s"]})
time.sleep(1)
call("get_desktop_state", {"screenshot_out_file": "/Users/admin/output/after.png"})
assert Path("/Users/admin/output/result.txt").read_text().strip() == "ALE desktop verified"
