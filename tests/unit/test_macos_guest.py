from __future__ import annotations

import base64
import json
import os
import pwd
import subprocess

import pytest

from ale.run.guestd import gui, main

pytestmark = pytest.mark.unit


def test_macos_framework_capture_and_input_use_task_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = pwd.struct_passwd(("artist", "*", 502, 20, "", "/Users/artist", "/bin/zsh"))
    monkeypatch.setattr(gui.sys, "platform", "darwin")
    monkeypatch.setattr(gui.os, "geteuid", lambda: 0)

    def get_account(name):
        assert name == "artist"
        return account

    monkeypatch.setattr(pwd, "getpwnam", get_account)
    monkeypatch.setenv("ALE_AGENT_USER", "artist")
    monkeypatch.setenv("ALE_CUA_DRIVER", "/Applications/CuaDriver.app/Contents/MacOS/cua-driver")
    monkeypatch.setenv("ALE_CUA_DRIVER_SOCKET", "/tmp/artist.sock")
    monkeypatch.setenv("HOME", "/var/root")
    identities = []
    monkeypatch.setattr(gui.os, "setgid", lambda gid: identities.append(("gid", gid)))
    monkeypatch.setattr(
        gui.os, "initgroups", lambda name, gid: identities.append(("groups", name, gid))
    )
    monkeypatch.setattr(gui.os, "setuid", lambda uid: identities.append(("uid", uid)))
    calls = []

    def run(command, **options):
        assert options["env"]["HOME"] == "/Users/artist"
        assert options["env"]["USER"] == options["env"]["LOGNAME"] == "artist"
        assert options["env"]["ALE_CUA_DRIVER_SOCKET"] == "/tmp/artist.sock"
        assert command[-2:] == ["--socket", "/tmp/artist.sock"]
        options["preexec_fn"]()
        calls.append(command[2])
        payload = {
            "width": 200,
            "height": 100,
            "screenshot_width": 400,
            "screenshot_height": 200,
            "screenshot_png_b64": base64.b64encode(b"png").decode(),
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

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
    assert identities == [("gid", 20), ("groups", "artist", 20), ("uid", 502)] * len(calls)
    assert os.environ["HOME"] == "/var/root"


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_driver_preserves_other_platforms_and_unprivileged_macos(monkeypatch, platform):
    monkeypatch.setattr(gui.sys, "platform", platform)

    def geteuid():
        assert platform == "darwin", "non-macOS must not enter identity switching"
        return 502

    monkeypatch.setattr(gui.os, "geteuid", geteuid)
    monkeypatch.setenv("ALE_CUA_DRIVER", "/driver")
    monkeypatch.setenv("ALE_CUA_DRIVER_SOCKET", "/driver.sock")

    def run(command, **options):
        assert command == ["/driver", "call", "get_screen_size", "{}", "--socket", "/driver.sock"]
        assert options == dict(capture_output=True, text=True, timeout=60, check=False)
        return subprocess.CompletedProcess(command, 0, '{"width":200,"height":100}', "")

    monkeypatch.setattr(gui.subprocess, "run", run)
    assert gui.screen_size() == (200, 100)


@pytest.mark.parametrize("user", [None, "missing", "root"])
def test_macos_root_driver_call_requires_non_root_task_account(monkeypatch, user):
    monkeypatch.setattr(gui.sys, "platform", "darwin")
    monkeypatch.setattr(gui.os, "geteuid", lambda: 0)
    monkeypatch.setenv("ALE_CUA_DRIVER", "/driver")
    if user is None:
        monkeypatch.delenv("ALE_AGENT_USER", raising=False)
    else:
        monkeypatch.setenv("ALE_AGENT_USER", user)

    def account(name):
        if name != "root":
            raise KeyError(name)
        return pwd.struct_passwd(("root", "*", 0, 0, "", "/var/root", "/bin/sh"))

    monkeypatch.setattr(pwd, "getpwnam", account)
    monkeypatch.setattr(
        gui.subprocess, "run", lambda *a, **kw: pytest.fail("must not call Driver as root")
    )
    with pytest.raises(gui.DesktopIdentityError):
        gui.capture_screen()


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
    monkeypatch.setenv("ALE_CUA_DRIVER", "/opt/ale/bin/cua-driver")
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
