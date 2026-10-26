"""Private subprocess: bounded UI automation, killed with provisioning on cancellation."""

import json
import sys
import tempfile
from pathlib import Path

from vncdotool import api

from ale.run.providers.macos_permission_retry import run_with_retries
from ale.run.providers.macos_permission_ui import Bootstrap
from ale.run.providers.macos_permission_vnc import OrchardVNC


def guest(*argv):
    print(json.dumps({"guest": list(argv)}), flush=True)
    response = json.loads(sys.stdin.readline())
    if response["code"]:
        raise RuntimeError("Guest UI command failed")
    return response["stdout"]


def main():
    settings = json.loads(sys.stdin.readline())
    tempfile.tempdir = settings["temporary"]

    vnc = None
    runner = None
    try:

        def flow():
            nonlocal vnc, runner
            if vnc is not None:
                vnc.close()
            vnc = OrchardVNC(
                "127.0.0.1",
                settings["port"],
                settings["user"],
                settings["password"],
            )
            runner = Bootstrap(guest, vnc, Path(settings["ocr"]), settings["password"])
            results = [
                runner.ensure("/Applications/CuaDriver.app", "CuaDriver", p)
                for p in ("accessibility", "screen_recording")
            ]
            # Settings relaunch drops CLI arguments; re-establish the ALE endpoint.
            guest("/bin/sh", "-c", "/usr/bin/killall cua-driver || test $? = 1")
            guest(
                "/usr/bin/open",
                "-a",
                "/Applications/CuaDriver.app",
                "--args",
                "serve",
                "--socket",
                settings["socket"],
            )
            guest("/bin/sleep", "2")
            # First-use capture consent is separate from the System Settings grants.
            runner.confirm_capture_ready(
                "CuaDriver", lambda: guest(*settings["capture_argv"]).strip() == "ready"
            )
            return results

        def recover():
            if runner is not None:
                runner.recover()

        result = run_with_retries(flow, recover, settings["retries"])
        print(json.dumps({"result": result}), flush=True)
    except Exception as exc:
        print(
            json.dumps({"result": {"status": "failed", "error_type": type(exc).__name__}}),
            flush=True,
        )
    finally:
        if vnc is not None:
            vnc.close()
        api.shutdown()


if __name__ == "__main__":
    main()
