import json
import subprocess
import time
from pathlib import Path

DRIVER = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"


def call(name, args):
    p = subprocess.run(
        [DRIVER, "call", name, json.dumps(args), "--socket", "/tmp/ale-cua-driver.sock"],
        capture_output=True,
        check=True,
        text=True,
    )
    result = json.loads(p.stdout)
    if result.get("isError") or result.get("error"):
        raise RuntimeError("Driver operation failed")
    return result


desktop = call("get_desktop_state", {"screenshot_out_file": "/Users/admin/output/before.png"})
# A window in front does not hold the keyboard until its document is clicked.
call(
    "click",
    {
        "x": desktop["screenshot_width"] * 2 // 5,
        "y": desktop["screenshot_height"] // 3,
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
