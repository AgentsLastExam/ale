from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

from ale.run.guestd import gui

pytestmark = pytest.mark.unit


def test_cua_driver_is_the_only_gui_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[1] == "mcp"
        request = json.loads(str(kwargs["input"]).splitlines()[-1])["params"]
        tool, arguments = request["name"], request["arguments"]
        calls.append((tool, arguments))
        result: dict[str, object] = {"structuredContent": {"verified": True}}
        if tool == "get_screen_size":
            result = {"structuredContent": {"width": 200, "height": 100, "scale_factor": 1}}
        elif tool == "get_desktop_state":
            result = {"content": [{"type": "image", "data": base64.b64encode(b"png").decode()}]}
        reply = {"jsonrpc": "2.0", "id": 1, "result": result}
        return subprocess.CompletedProcess(command, 0, json.dumps(reply) + "\n", "")

    monkeypatch.setattr(gui.shutil, "which", lambda name: "/usr/bin/cua-driver")
    monkeypatch.setattr(gui.subprocess, "run", run)

    assert gui.capture_screen() == b"png"
    assert gui.dispatch_actions([{"type": "click", "coordinate": [500, 500]}]) == 1
    assert calls == [
        ("get_desktop_state", {}),
        ("get_screen_size", {}),
        ("click", {"x": 100, "y": 50, "scope": "desktop", "button": "left", "count": 1}),
    ]


def _program(path: Path, subsystem: int) -> Path:
    """A file with just the PE header fields the Windows launcher reads."""
    header = bytearray(0x200)
    header[0x3C:0x40] = (0x80).to_bytes(4, "little")
    header[0x80 + 0x5C : 0x80 + 0x5E] = subsystem.to_bytes(2, "little")
    path.write_bytes(bytes(header))
    path.chmod(0o755)
    return path


def test_only_console_programs_start_with_a_hidden_window(tmp_path: Path) -> None:
    from ale.run.guestd import windows

    _program(tmp_path / "notepad.exe", 2)
    _program(tmp_path / "git.exe", 3)
    (tmp_path / "npm.cmd").write_text("@echo off\n")
    env = {"PATH": str(tmp_path)}
    assert not windows._console_program(str(tmp_path / "notepad.exe"), env)
    assert windows._console_program(str(tmp_path / "git.exe"), env)
    assert windows._console_program(str(tmp_path / "npm.cmd"), env)
    assert windows._console_program("missing.exe", env)


def test_root_calls_the_driver_as_the_account_that_runs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pwd

    socket = tmp_path / "driver.sock"
    socket.write_text("")
    agent = pwd.struct_passwd(("user", "x", 1000, 1000, "", "/home/user", "/bin/sh"))
    monkeypatch.setattr(gui.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gui.os, "stat", lambda path: type("Stat", (), {"st_uid": 1000})())
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: agent)
    options = gui._as_daemon_owner(str(socket))
    assert options["env"]["HOME"] == "/home/user" and callable(options["preexec_fn"])
    monkeypatch.setattr(gui.os, "geteuid", lambda: 1000)
    assert gui._as_daemon_owner(str(socket)) == {}  # already that account
