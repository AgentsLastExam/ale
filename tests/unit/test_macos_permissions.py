"""Opt-in permission bootstrap configuration and process lifecycle."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from ale.core.config import OrchardProviderConfig
from ale.run.providers.orchard_client import OrchardClient


def config(**kw):
    return OrchardProviderConfig(endpoint="https://localhost:8443", **kw)


def test_permissions_default_off():
    assert config().desktop_bootstrap is None


def test_bootstrap_selects_installed_driver_paths():
    c = config(desktop_bootstrap={})
    assert c.driver_path == "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"
    assert c.socket_path == "/tmp/ale-cua-driver.sock"
    assert c.desktop_bootstrap.retries == 3
    assert c.desktop_bootstrap.password_env == "ALE_MACOS_ADMIN_PASSWORD"


@pytest.mark.parametrize("options", [{"retries": 4}, {"password_env": "bad name"}])
def test_invalid_permission_config(options):
    with pytest.raises(ValidationError):
        config(desktop_bootstrap=options)


def test_bootstrap_refuses_conflicting_driver_paths():
    with pytest.raises(ValidationError):
        config(desktop_bootstrap={}, driver_path="/elsewhere", socket_path="/tmp/d.sock")


async def test_forward_closes_connection_and_listener():
    client = OrchardClient("https://localhost", username="fake", password="fake")
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def relay(name, uid, reader, writer):
        try:
            assert (name, uid) == ("owned", "uid")
            writer.write(b"RFB test")
            await writer.drain()
            entered.set()
            await asyncio.Event().wait()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    client._relay_vnc = relay
    async with client.forward_vnc("owned", "uid") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await reader.read(8) == b"RFB test"
        await entered.wait()
    await asyncio.wait_for(closed.wait(), 1)
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_forward_refuses_changed_uid():
    client = OrchardClient("https://localhost", username="fake", password="fake")
    client.get_vm = AsyncMock(return_value={"uid": "replacement"})
    reader = asyncio.StreamReader()
    writer = AsyncMock()
    writer.close = lambda: None
    await client._relay_vnc("owned", "original", reader, writer)
    writer.wait_closed.assert_awaited_once()


async def test_ui_subprocess_is_killed_on_cancellation(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from ale.run.providers import orchard_permissions as permissions

    monkeypatch.setattr(permissions, "preflight", lambda c: None)
    monkeypatch.setenv("ALE_MACOS_ADMIN_PASSWORD", "fake-private-password")
    compiler = SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))
    child = SimpleNamespace(
        returncode=None,
        stdin=SimpleNamespace(write=lambda value: None, drain=AsyncMock()),
        stdout=asyncio.StreamReader(),
    )
    killed = asyncio.Event()

    def kill():
        child.returncode = -9
        killed.set()

    child.kill = kill
    child.wait = AsyncMock(return_value=-9)
    spawn = AsyncMock(side_effect=[compiler, child])
    monkeypatch.setattr(permissions.asyncio, "create_subprocess_exec", spawn)
    closed = asyncio.Event()

    @asynccontextmanager
    async def forward(name, uid):
        try:
            yield 15900
        finally:
            closed.set()

    client = SimpleNamespace(forward_vnc=forward)
    task = asyncio.create_task(
        permissions.authorize_driver(client, "owned", "uid", "admin", config(desktop_bootstrap={}))
    )
    while spawn.await_count < 2:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed.is_set() and closed.is_set()
    assert "fake-private-password" not in repr(spawn.call_args_list)


def test_bootstrap_missing_secret_fails_without_exposing_value(monkeypatch):
    from ale.core.errors import ProviderCapabilityError
    from ale.run.providers import orchard_permissions as permissions

    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    monkeypatch.setattr(permissions.shutil, "which", lambda name: "/usr/bin/swiftc")
    monkeypatch.delenv("ALE_MACOS_ADMIN_PASSWORD", raising=False)
    with pytest.raises(ProviderCapabilityError, match="guest password"):
        permissions.preflight(config(desktop_bootstrap={}))


def test_driver_archive_rejects_wrong_checksum(monkeypatch, tmp_path):
    import io

    from ale.core.errors import ProviderStartError
    from ale.run import framework

    monkeypatch.setattr(framework, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(
        framework.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"tampered")
    )
    with pytest.raises(ProviderStartError, match="checksum"):
        framework.macos_driver_archive()
    assert not list(tmp_path.rglob("*.tar.gz"))


@pytest.fixture
def consent_flow(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from ale.run.providers import macos_permission_ui as ui
    from ale.run.providers.macos_permission_policy import Row

    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(ui.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(
        ui.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds)
    )
    runner = ui.Bootstrap(Mock(), Mock(), None, "fake", timeout=20)
    prompt = [
        Row('"CuaDriver" is requesting to', 610, 250, 200, 14),
        Row("bypass the system private window picker and directly", 610, 270, 200, 14),
        Row("access your screen and audio.", 610, 300, 200, 14),
        Row("Allow", 700, 420, 40, 14),
        Row("Open System Settings", 640, 460, 150, 14),
    ]
    clear = [Row("Finder", 10, 10, 50, 14)]
    return runner, clock, prompt, clear


def test_delayed_consent_must_disappear_before_ready(consent_flow):
    from unittest.mock import Mock

    runner, clock, prompt, clear = consent_flow
    runner.read_rows = Mock(
        side_effect=lambda _: prompt if clock.now >= 3 and not runner.vnc.click.called else clear
    )
    probe = Mock(return_value=True)
    runner.confirm_capture_ready("CuaDriver", probe)
    runner.vnc.click.assert_called_once()
    assert clock.now >= 9
    assert probe.call_count >= 3


def test_already_authorized_needs_repeated_capture_and_observation(consent_flow):
    from unittest.mock import Mock

    runner, clock, _, clear = consent_flow
    runner.read_rows = Mock(return_value=clear)
    probe = Mock(return_value=True)
    runner.confirm_capture_ready("CuaDriver", probe)
    runner.vnc.click.assert_not_called()
    assert clock.now >= 6 and probe.call_count >= 2


def test_missed_ocr_is_reobserved_not_accepted(consent_flow):
    from unittest.mock import Mock

    runner, clock, prompt, clear = consent_flow

    def rows(_):
        if clock.now < 3:
            return []
        return clear if runner.vnc.click.called else prompt

    runner.read_rows = Mock(side_effect=rows)
    runner.confirm_capture_ready("CuaDriver", Mock(return_value=True))
    runner.vnc.click.assert_called_once()
    assert clock.now >= 9


def test_ineffective_click_cannot_pass_with_valid_png(consent_flow):
    from unittest.mock import Mock

    runner, _, prompt, _ = consent_flow
    runner.read_rows = Mock(return_value=prompt)
    with pytest.raises(RuntimeError):
        runner.confirm_capture_ready("CuaDriver", Mock(return_value=True))
    assert 1 <= runner.vnc.click.call_count <= 3


def test_incomplete_or_foreign_dialog_is_never_accepted(consent_flow):
    from unittest.mock import Mock

    runner, _, prompt, _ = consent_flow
    runner.read_rows = Mock(return_value=prompt[1:])
    with pytest.raises(RuntimeError):
        runner.confirm_capture_ready("CuaDriver", Mock(return_value=True))
    runner.vnc.click.assert_not_called()


def test_capture_failure_cannot_pass_with_clear_ui(consent_flow):
    from unittest.mock import Mock

    runner, _, _, clear = consent_flow
    runner.read_rows = Mock(return_value=clear)
    with pytest.raises(RuntimeError):
        runner.confirm_capture_ready("CuaDriver", Mock(return_value=False))
    runner.vnc.click.assert_not_called()


def test_consent_failure_enters_whole_flow_retry(consent_flow):
    from unittest.mock import Mock

    from ale.run.providers.macos_permission_retry import run_with_retries

    runner, _, prompt, clear = consent_flow
    runner.read_rows = Mock(return_value=prompt)

    def recover():
        runner.read_rows.return_value = clear

    result = run_with_retries(
        lambda: runner.confirm_capture_ready("CuaDriver", Mock(return_value=True)), recover, 1
    )
    assert [a["status"] for a in result["attempts"]] == ["failed", "ui_enabled"]


@pytest.mark.parametrize(
    "kind", ["png", "no_image", "invalid_base64", "tool_error", "timeout", "exit"]
)
def test_capture_probe_checks_real_driver_result_without_returning_image(kind, monkeypatch, capsys):
    import base64
    import json
    import subprocess
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock

    from ale.run.providers.orchard_permissions import _DRIVER_CAPTURE

    payload = {"screenshot_png_b64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()}
    if kind == "no_image":
        payload = {}
    elif kind == "invalid_base64":
        payload["screenshot_png_b64"] = "invalid!"
    elif kind == "tool_error":
        payload["isError"] = True
    call = Mock(return_value=SimpleNamespace(stdout=json.dumps(payload).encode()))
    if kind == "timeout":
        call.side_effect = subprocess.TimeoutExpired("fake-driver", 5)
    elif kind == "exit":
        call.side_effect = subprocess.CalledProcessError(1, "fake-driver")
    monkeypatch.setattr(subprocess, "run", call)
    monkeypatch.setattr(sys, "argv", ["probe", "/fake/driver", "/fake/socket"])
    exec(_DRIVER_CAPTURE, {})
    assert capsys.readouterr().out == ("ready\n" if kind == "png" else "not_ready\n")
    assert call.call_args.args[0] == [
        "/fake/driver",
        "call",
        "get_desktop_state",
        "{}",
        "--socket",
        "/fake/socket",
    ]
    assert call.call_args.kwargs["timeout"] == 5


def test_worker_does_not_report_success_after_consent_timeout(monkeypatch, tmp_path, capsys):
    import io
    import json
    from unittest.mock import Mock

    from ale.run.providers import macos_permission_worker as worker

    settings = {
        "temporary": str(tmp_path),
        "port": 15900,
        "user": "fake",
        "password": "fake",
        "ocr": "/fake/ocr",
        "socket": "/fake/socket",
        "capture_argv": ["fake-probe"],
        "retries": 0,
    }
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps(settings)))
    monkeypatch.setattr(worker.tempfile, "tempdir", str(tmp_path))
    runner = Mock()
    runner.confirm_capture_ready.side_effect = RuntimeError("consent timeout")
    monkeypatch.setattr(worker, "Bootstrap", Mock(return_value=runner))
    monkeypatch.setattr(worker, "OrchardVNC", Mock())
    monkeypatch.setattr(worker, "guest", Mock(return_value="ready"))
    monkeypatch.setattr(worker.api, "shutdown", Mock())
    worker.main()
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["status"] == "failed"
    assert len(result["attempts"]) == 1
    runner.confirm_capture_ready.assert_called_once()
