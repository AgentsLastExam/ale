from __future__ import annotations

import base64
import json
import os
import pwd
import subprocess

import pytest

from ale.run.guestd import gui, main

pytestmark = pytest.mark.unit


def _reply(content: list[dict] | None = None, structured: dict | None = None) -> str:
    result = {"content": content or [], "structuredContent": structured}
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}) + "\n"


def test_macos_framework_capture_and_input_use_task_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = pwd.struct_passwd(("artist", "*", 502, 20, "", "/Users/artist", "/bin/zsh"))
    monkeypatch.setattr(gui.sys, "platform", "darwin")
    monkeypatch.setattr(gui.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gui.os, "stat", lambda path: type("Stat", (), {"st_uid": 502})())
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: account)
    monkeypatch.setenv("ALE_CUA_DRIVER", "/Applications/CuaDriver.app/Contents/MacOS/cua-driver")
    monkeypatch.setenv("ALE_CUA_DRIVER_SOCKET", "/tmp/artist.sock")
    monkeypatch.setenv("HOME", "/var/root")
    identities = []
    monkeypatch.setattr(gui.os, "setgid", lambda gid: identities.append(("gid", gid)))
    monkeypatch.setattr(
        gui.os, "initgroups", lambda name, gid: identities.append(("groups", name, gid))
    )
    monkeypatch.setattr(gui.os, "setuid", lambda uid: identities.append(("uid", uid)))
    calls, arguments = [], []

    def run(command, **options):
        assert options["env"]["HOME"] == "/Users/artist"
        assert options["env"]["USER"] == options["env"]["LOGNAME"] == "artist"
        assert command[-2:] == ["--socket", "/tmp/artist.sock"]
        options["preexec_fn"]()
        params = json.loads(options["input"].splitlines()[-1])["params"]
        calls.append(params["name"])
        arguments.append(params.get("arguments") or {})
        image = {"type": "image", "data": base64.b64encode(b"png").decode()}
        structured = {"screenshot_width": 400, "screenshot_height": 200}
        return subprocess.CompletedProcess(command, 0, _reply([image], structured), "")

    monkeypatch.setattr(gui.subprocess, "run", run)
    assert gui.capture_screen() == b"png"
    assert (
        gui.dispatch_actions(
            [
                {"type": "click", "coordinate": [500, 500]},
                {"type": "type", "text": "hello"},
                {"type": "key", "keys": ["cmd", "s"]},
            ]
        )
        == 3
    )
    assert calls == ["get_desktop_state", "get_desktop_state", "click", "type_text", "hotkey"]
    # No one else uses a sandbox's screen, so the pointer is driven even over background apps.
    assert arguments[2]["scope"] == "desktop" and arguments[2]["delivery_mode"] == "foreground"
    assert "delivery_mode" not in arguments[3] and "delivery_mode" not in arguments[4]
    assert identities == [("gid", 20), ("groups", "artist", 20), ("uid", 502)] * len(calls)
    assert os.environ["HOME"] == "/var/root"


def test_health_identifies_macos_and_both_process_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    account = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setenv("ALE_AGENT_USER", account.pw_name)
    monkeypatch.setenv("ALE_AGENT_HOME", account.pw_dir)
    health = main.Handler(lambda _: None).op_health(1, {})["data"]
    assert health["os"] == "macos"
    assert health["framework_uid"] == os.geteuid()
    assert health["agent_uid"] == account.pw_uid
    assert health["agent_home"] == account.pw_dir


def test_macos_requires_a_preconfigured_desktop_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui.sys, "platform", "darwin")
    monkeypatch.delenv("ALE_CUA_DRIVER_SOCKET", raising=False)
    monkeypatch.setattr(gui.shutil, "which", lambda _: "/usr/local/bin/cua-driver")
    assert not gui.has_desktop()
    assert not gui._start_server()


def test_macos_desktop_reports_actual_driver_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui.sys, "platform", "darwin")
    monkeypatch.setattr(gui.os, "geteuid", lambda: 501)
    monkeypatch.setenv("ALE_CUA_DRIVER_SOCKET", "/tmp/ale-desktop.sock")
    monkeypatch.setenv("ALE_CUA_DRIVER", "/Applications/CuaDriver.app/Contents/MacOS/cua-driver")
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "screen capture permission missing")

    monkeypatch.setattr(gui.subprocess, "run", run)
    assert gui.has_desktop()
    assert not gui.available()
    assert calls[0][-2:] == ["--socket", "/tmp/ale-desktop.sock"]


def test_macos_agent_demotion_sets_account_and_resets_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = pwd.struct_passwd(("agent", "*", 501, 20, "", "/Users/agent", "/bin/zsh"))
    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setattr(main.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwnam", lambda _: account)
    calls: list[object] = []
    monkeypatch.setattr(main.os, "setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr(
        main.os, "initgroups", lambda user, gid: calls.append(("groups", user, gid))
    )
    monkeypatch.setattr(main.os, "setuid", lambda uid: calls.append(("uid", uid)))
    env: dict[str, str] = {}
    options = main._drop_to("agent", env)
    options["preexec_fn"]()
    assert calls == [("gid", 20), ("groups", "agent", 20), ("uid", 501)]
    assert env["HOME"] == "/Users/agent"
    assert env["USER"] == env["LOGNAME"] == "agent"
    assert env["PATH"].startswith("/Users/agent/.local/bin:/opt/homebrew/bin:/usr/local/bin:")
    assert "XDG_RUNTIME_DIR" not in env
