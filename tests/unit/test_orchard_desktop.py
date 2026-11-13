"""macOS desktop readiness must hold for the account that runs agent tools."""

from __future__ import annotations

import asyncio
import base64
import json
import stat
from types import SimpleNamespace

import pytest

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.run.framework import MACOS_DRIVER, MACOS_DRIVER_SOCKET, MACOS_PYTHON
from ale.run.guestd import gui
from ale.run.providers.orchard import desktop as orchard_desktop

pytestmark = pytest.mark.unit

DRIVER = "/usr/local/bin/cua-driver"
SOCKET = "/Users/evaluator/driver.sock"
PNG = b"\x89PNG\r\n\x1a\nimage"


@pytest.fixture
def desktop(monkeypatch):
    import pwd

    monkeypatch.setattr(gui.sys, "platform", "darwin")
    monkeypatch.setattr(gui.os, "geteuid", lambda: 502)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=502))
    monkeypatch.setenv("ALE_CUA_DRIVER", DRIVER)
    monkeypatch.setenv("ALE_CUA_DRIVER_SOCKET", SOCKET)
    console = SimpleNamespace(st_uid=502)
    socket = SimpleNamespace(st_uid=502, st_mode=stat.S_IFSOCK | 0o600)
    monkeypatch.setattr(gui.os, "stat", lambda path: console)
    monkeypatch.setattr(gui.os, "lstat", lambda path: socket)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        image = {"type": "image", "data": base64.b64encode(PNG).decode()}
        reply = {"jsonrpc": "2.0", "id": 1, "result": {"content": [image]}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(reply) + "\n", stderr="")

    monkeypatch.setattr(gui.subprocess, "run", run)
    return console, socket, calls


def test_agent_desktop_probe_uses_the_framework_capture_path(desktop):
    _, _, calls = desktop
    gui.probe_macos_desktop("evaluator", DRIVER, SOCKET)
    assert calls == [[DRIVER, "mcp", "--socket", SOCKET]]


@pytest.mark.parametrize("uid", [0, 501])
def test_desktop_probe_rejects_root_or_another_executing_user(desktop, monkeypatch, uid):
    monkeypatch.setattr(gui.os, "geteuid", lambda: uid)
    with pytest.raises(gui.DesktopIdentityError, match="task account"):
        gui.probe_macos_desktop("evaluator", DRIVER, SOCKET)
    assert not desktop[2]


def test_desktop_probe_rejects_another_logged_in_user(desktop):
    desktop[0].st_uid = 501
    with pytest.raises(gui.DesktopIdentityError, match="console"):
        gui.probe_macos_desktop("evaluator", DRIVER, SOCKET)


def test_desktop_probe_waits_for_login(desktop):
    desktop[0].st_uid = 0
    with pytest.raises(gui.GuiUnavailable, match="logged-in"):
        gui.probe_macos_desktop("evaluator", DRIVER, SOCKET)


@pytest.mark.parametrize("mode", [stat.S_IFREG, stat.S_IFLNK])
def test_desktop_probe_rejects_non_socket_paths(desktop, mode):
    desktop[1].st_mode = mode | 0o600
    with pytest.raises(gui.DesktopIdentityError, match="socket"):
        gui.probe_macos_desktop("evaluator", DRIVER, SOCKET)


def test_desktop_probe_rejects_socket_owned_by_another_user(desktop):
    desktop[1].st_uid = 501
    with pytest.raises(gui.DesktopIdentityError, match="socket owner"):
        gui.probe_macos_desktop("evaluator", DRIVER, SOCKET)


def test_desktop_probe_waits_for_socket(desktop, monkeypatch):
    def missing(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(gui.os, "lstat", missing)
    with pytest.raises(gui.GuiUnavailable, match="socket"):
        gui.probe_macos_desktop("evaluator", DRIVER, SOCKET)


def test_desktop_probe_checks_real_agent_socket_access(desktop, monkeypatch):
    monkeypatch.setattr(
        gui.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="Permission denied"
        ),
    )
    with pytest.raises(gui.GuiUnavailable, match="Permission denied"):
        gui.probe_macos_desktop("evaluator", DRIVER, SOCKET)


@pytest.mark.parametrize("key", ["ALE_CUA_DRIVER", "ALE_CUA_DRIVER_SOCKET"])
def test_desktop_probe_rejects_different_agent_tool_configuration(desktop, monkeypatch, key):
    monkeypatch.setenv(key, "/different")
    with pytest.raises(gui.DesktopIdentityError, match="configuration"):
        gui.probe_macos_desktop("evaluator", DRIVER, SOCKET)


def test_desktop_probe_rejects_non_image_capture(desktop, monkeypatch):
    monkeypatch.setattr(gui, "capture_screen", lambda: b"")
    with pytest.raises(gui.GuiUnavailable, match="PNG"):
        gui.probe_macos_desktop("evaluator", DRIVER, SOCKET)


class Guest:
    """The guest as readiness sees it: who holds the console, then each probe's result."""

    def __init__(self, *probes, console=("evaluator",), start=(0, "", "", False)):
        self.console, self.probes, self.start = list(console), list(probes), start
        self.calls = []

    @staticmethod
    def _next(results):
        return results.pop(0) if len(results) > 1 else results[0]

    async def exec(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "/usr/bin/stat":
            result = self._next(self.console)
            return await result() if callable(result) else (0, result + "\n", "", False)
        if argv[0] == "/bin/launchctl":
            return self.start
        result = self._next(self.probes)
        return await result() if callable(result) else result

    def probe_calls(self):
        return [call for call in self.calls if call[0][0] == MACOS_PYTHON]


async def wait(client, **kwargs):
    await orchard_desktop.await_desktop(client, agent_user="evaluator", agent_uid=502, **kwargs)


async def test_readiness_starts_the_driver_in_the_signed_in_session_then_probes_as_agent(
    monkeypatch,
):
    monkeypatch.setattr(orchard_desktop, "_RETRY_INTERVAL", 0)
    client = Guest((0, "ready\n", "", False), console=("root", "evaluator"))
    await wait(client)
    starts = [call for call in client.calls if call[0][0] == "/bin/launchctl"]
    assert starts == [(orchard_desktop.driver_argv("evaluator", 502), {"timeout_sec": 30})]
    assert starts[0][0][:7] == [
        "/bin/launchctl",
        "asuser",
        "502",
        "/usr/bin/sudo",
        "-u",
        "evaluator",
        "/usr/bin/open",
    ]
    assert MACOS_DRIVER_SOCKET in starts[0][0]
    ((argv, kwargs),) = client.probe_calls()
    assert argv[:4] == [MACOS_PYTHON, "-I", "-S", "-c"]
    assert argv[-3:] == ["evaluator", MACOS_DRIVER, MACOS_DRIVER_SOCKET]
    assert kwargs["run_as"] == "evaluator"


async def test_readiness_probes_again_until_the_desktop_captures(monkeypatch):
    monkeypatch.setattr(orchard_desktop, "_RETRY_INTERVAL", 0)
    client = Guest((75, "", "not ready", False), (0, "ready\n", "", False))
    await wait(client)
    assert len(client.probe_calls()) == 2
    assert sum(call[0][0] == "/bin/launchctl" for call in client.calls) == 1


async def test_readiness_fails_identity_mismatch_without_retry():
    client = Guest((78, "", "console uid differs", False))
    with pytest.raises(ProviderCapabilityError, match="console uid differs"):
        await wait(client)
    assert len(client.probe_calls()) == 1


async def test_readiness_failure_has_bounded_wait_and_last_diagnostic(monkeypatch):
    monkeypatch.setattr(orchard_desktop, "_RETRY_INTERVAL", 0)
    client = Guest((75, "", "Permission denied", False))
    with pytest.raises(ProviderStartError, match="Permission denied"):
        await wait(client, timeout_sec=0.01)


async def test_readiness_waits_for_the_account_to_sign_in(monkeypatch):
    monkeypatch.setattr(orchard_desktop, "_RETRY_INTERVAL", 0)
    client = Guest((0, "ready\n", "", False), console=("root",))
    with pytest.raises(ProviderStartError, match="not signed in"):
        await wait(client, timeout_sec=0.01)
    assert not client.probe_calls()


async def test_a_driver_that_cannot_start_fails_readiness():
    client = Guest((0, "ready\n", "", False), start=(1, "", "LSOpen failed", False))
    with pytest.raises(ProviderStartError, match="LSOpen failed"):
        await wait(client)


async def test_readiness_does_not_accept_invalid_success():
    client = Guest((0, "unexpected", "", False))
    with pytest.raises(ProviderStartError, match="invalid"):
        await wait(client)


async def test_readiness_cancellation_is_not_retried():
    entered = asyncio.Event()

    async def hanging():
        entered.set()
        await asyncio.Event().wait()

    client = Guest(hanging)
    task = asyncio.create_task(wait(client))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(client.probe_calls()) == 1


async def test_readiness_overall_deadline_bounds_stuck_transport():
    async def hanging():
        await asyncio.Event().wait()

    with pytest.raises(ProviderStartError, match="did not become ready"):
        await wait(Guest(hanging, console=(hanging,)), timeout_sec=0.01)
