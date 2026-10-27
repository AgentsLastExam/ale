"""macOS desktop readiness must hold for the account that runs agent tools."""

from __future__ import annotations

import asyncio
import base64
import json
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.run.guestd import gui
from ale.run.providers import orchard_desktop
from ale.run.transport import GuestClient

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
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"screenshot_png_b64": base64.b64encode(PNG).decode()}),
            stderr="",
        )

    monkeypatch.setattr(gui.subprocess, "run", run)
    return console, socket, calls


def test_agent_desktop_probe_uses_the_framework_capture_path(desktop):
    _, _, calls = desktop
    gui.probe_macos_desktop("evaluator", DRIVER, SOCKET)
    assert calls == [[DRIVER, "call", "get_desktop_state", "{}", "--socket", SOCKET]]


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


async def wait(client, **kwargs):
    await orchard_desktop.await_desktop(
        client,
        agent_user="evaluator",
        python_path="/usr/local/bin/python3",
        driver_path=DRIVER,
        socket_path=SOCKET,
        **kwargs,
    )


async def test_readiness_runs_capture_as_agent_without_root_fallback():
    client = AsyncMock(spec=GuestClient)
    client.exec.return_value = (0, "ready\n", "", False)
    await wait(client)
    argv = client.exec.call_args.args[0]
    assert argv[:4] == ["/usr/local/bin/python3", "-I", "-S", "-c"]
    assert argv[-3:] == ["evaluator", DRIVER, SOCKET]
    assert client.exec.call_args.kwargs["run_as"] == "evaluator"
    client.screenshot.assert_not_called()


async def test_readiness_retries_starting_desktop(monkeypatch):
    monkeypatch.setattr(orchard_desktop, "_RETRY_INTERVAL", 0)
    client = AsyncMock(spec=GuestClient)
    client.exec.side_effect = [(75, "", "not ready", False), (0, "ready\n", "", False)]
    await wait(client)
    assert client.exec.call_count == 2


async def test_readiness_fails_identity_mismatch_without_retry():
    client = AsyncMock(spec=GuestClient)
    client.exec.return_value = (78, "", "console uid differs", False)
    with pytest.raises(ProviderCapabilityError, match="console uid differs"):
        await wait(client)
    assert client.exec.call_count == 1


async def test_readiness_failure_has_bounded_wait_and_last_diagnostic(monkeypatch):
    monkeypatch.setattr(orchard_desktop, "_RETRY_INTERVAL", 0)
    client = AsyncMock(spec=GuestClient)
    client.exec.return_value = (75, "", "Permission denied", False)
    with pytest.raises(ProviderStartError, match="Permission denied"):
        await wait(client, timeout_sec=0.01)


async def test_readiness_does_not_accept_invalid_success():
    client = AsyncMock(spec=GuestClient)
    client.exec.return_value = (0, "unexpected", "", False)
    with pytest.raises(ProviderStartError, match="invalid"):
        await wait(client)


async def test_readiness_cancellation_is_not_retried():
    entered = asyncio.Event()

    async def hanging(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    client = AsyncMock(spec=GuestClient)
    client.exec.side_effect = hanging
    task = asyncio.create_task(wait(client))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.exec.call_count == 1


async def test_readiness_overall_deadline_bounds_stuck_transport():
    client = AsyncMock(spec=GuestClient)

    async def hanging(*args, **kwargs):
        await asyncio.Event().wait()

    client.exec.side_effect = hanging
    with pytest.raises(ProviderStartError, match="did not become ready"):
        await wait(client, timeout_sec=0.01)
