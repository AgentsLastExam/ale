from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from ale.core.errors import TaskDefinitionError
from ale.core.sandbox import ExecResult
from ale.run import framework, guestd
from ale.run.framework import (
    LABEL,
    framework_identity,
    install_on_windows,
    windows_framework_identity,
    with_framework,
)
from ale.run.guestd import gui

pytestmark = pytest.mark.unit


def test_the_layer_identity_follows_everything_it_is_built_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = framework_identity(user="user", sudo=False, vm=False)
    assert framework_identity(user="user", sudo=False, vm=False) == base
    assert framework_identity(user="app", sudo=False, vm=False) != base
    assert framework_identity(user="user", sudo=True, vm=False) != base
    assert framework_identity(user="user", sudo=False, vm=True) != base
    monkeypatch.setattr(framework, "_SETUP", "true\n")
    assert framework_identity(user="user", sudo=False, vm=False) != base


@pytest.mark.asyncio
async def test_the_layer_is_built_on_the_task_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path))
    driver = tmp_path / "cua-driver-release"
    driver.mkdir()
    (driver / "cua-driver").write_bytes(b"driver")
    monkeypatch.setattr(framework, "_cua_driver", lambda platform: driver)
    built: dict[str, str] = {}
    images = {"task": {"Id": "sha256:" + "1" * 64, "User": "app", "Labels": {}}}

    async def inspect(reference: str, *, missing_ok: bool = False) -> dict:
        if reference in images:
            return images[reference]
        assert missing_ok
        return {}

    async def run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        if argv[:3] == ("docker", "buildx", "build"):
            context = Path(argv[-1])
            built["dockerfile"] = (context / "Dockerfile").read_text()
            built["files"] = sorted(path.name for path in context.rglob("*"))
            built["unit"] = (context / "ale-guestd.service").read_text()
            tag = argv[argv.index("--tag") + 1]
            identity = framework_identity(user="app", sudo=True, vm=True)
            images[tag] = {"Id": "sha256:" + "2" * 64, "Labels": {LABEL: identity}}
        return 0, "", ""

    monkeypatch.setattr(framework, "_inspect", inspect)
    monkeypatch.setattr(framework, "_run", run)
    layered = await with_framework("task", user="app", sudo=True, vm=True)
    assert layered == "sha256:" + "2" * 64
    dockerfile = built["dockerfile"]
    assert dockerfile.startswith("FROM ale-framework-base:" + "1" * 64)
    assert "COPY guestd/ /opt/ale/guestd/" in dockerfile
    assert "COPY --chmod=0755 cua-driver /usr/local/bin/cua-driver" in dockerfile
    assert json.dumps(["/bin/sh", "/opt/ale/setup.sh", "app", "true"]) in dockerfile
    assert dockerfile.rstrip().splitlines()[-2] == "USER app"
    assert set(guestd.FILES) <= set(built["files"])
    assert "Environment=ALE_AGENT_USER=app" in built["unit"]
    assert f"--tcp 0.0.0.0:{guestd.PORT}" in built["unit"]
    # The same layer again is a no-op.
    images["task"] = {**images["task"], "Labels": {}}
    assert await with_framework("task", user="app", sudo=True, vm=True) == layered


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("platform", "asset", "payload", "binary"),
    [
        ("linux", "x.tar.gz", _tar({"release/cua-driver": b"elf", "README": b"r"}), "cua-driver"),
        (
            "windows",
            "x.zip",
            _zip({"cua-driver.exe": b"pe", "cua_driver_sdk.dll": b"d"}),
            "cua-driver.exe",
        ),
    ],
)
def test_cua_driver_is_downloaded_once_and_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    asset: str,
    payload: bytes,
    binary: str,
) -> None:
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path))
    downloads: list[str] = []

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

    def urlopen(url: str, timeout: float) -> Response:
        downloads.append(url)
        return Response(payload)

    monkeypatch.setattr(framework.urllib.request, "urlopen", urlopen)
    monkeypatch.setitem(
        framework._CUA_DRIVER, platform, (asset, hashlib.sha256(payload).hexdigest())
    )
    files = framework._cua_driver(platform)
    assert (files / binary).is_file()
    assert framework._cua_driver(platform) == files
    assert len(downloads) == 1 and downloads[0].endswith(f"/{asset}")
    # A different release behind the same name is refused.
    monkeypatch.setitem(framework._CUA_DRIVER, platform, (asset, "0" * 64))
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path / "fresh"))
    with pytest.raises(Exception, match="pinned checksum"):
        framework._cua_driver(platform)


class _WindowsBuildVm:
    def __init__(self, setup: ExecResult) -> None:
        self.setup = setup
        self.uploaded: list[str] = []
        self.commands: list[list[str]] = []

    async def upload_dir(self, source: str, target: str) -> None:
        self.uploaded = sorted(str(path.relative_to(source)) for path in Path(source).rglob("*"))
        self.target = target

    async def exec(self, argv: list[str], *, timeout_sec: float) -> ExecResult:
        self.commands.append(argv)
        if "-File" in argv:
            return self.setup
        return ExecResult(exit_code=0, stdout="", stderr="")


@pytest.mark.asyncio
async def test_windows_builds_get_the_current_guestd_and_cua_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "cua-driver.exe").write_bytes(b"pe")
    monkeypatch.setattr(framework, "_cua_driver", lambda platform: release)
    vm = _WindowsBuildVm(ExecResult(exit_code=0, stdout="", stderr=""))
    await install_on_windows(vm, user="user")  # type: ignore[arg-type]
    assert {f"guestd/{name}" for name in guestd.FILES} | {
        "cua-driver/cua-driver.exe",
        "setup.ps1",
    } <= set(vm.uploaded)
    setup, cleanup = vm.commands
    assert setup[setup.index("-User") + 1] == "user"
    assert "Remove-Item" in cleanup[-1] and vm.target in cleanup[-1]
    assert windows_framework_identity(user="user") != windows_framework_identity(user="admin")


@pytest.mark.asyncio
async def test_a_windows_base_with_another_account_fails_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(framework, "_cua_driver", lambda platform: tmp_path)
    reason = "this Windows base signs in 'user' as its agent account, but image.user names 'app'"
    vm = _WindowsBuildVm(ExecResult(exit_code=1, stdout="", stderr=f"ALE: {reason}\r\n"))
    with pytest.raises(TaskDefinitionError) as failure:
        await install_on_windows(vm, user="app")  # type: ignore[arg-type]
    assert str(failure.value).endswith(reason)
    assert len(vm.commands) == 2  # the staging files are removed even so


def test_a_desktop_is_decided_by_the_images_own_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    manager = tmp_path / "display-manager.service"
    monkeypatch.setattr(gui, "_DISPLAY_MANAGER", str(manager))
    assert not gui.has_desktop()  # e.g. an image that only ships Xvfb as a tool
    monkeypatch.setenv("DISPLAY", ":1")
    assert gui.has_desktop()  # a container image that points its processes at a display
    monkeypatch.delenv("DISPLAY")
    manager.symlink_to("/lib/systemd/system/gdm3.service")
    assert gui.has_desktop()  # a VM whose systemd enables a display manager


def test_a_windows_desktop_is_drivable_once_the_framework_installed_cua_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = tmp_path / "cua-driver.exe"
    monkeypatch.setattr(gui, "_WINDOWS_DRIVER", str(driver))
    monkeypatch.setattr(gui.os, "name", "nt")
    assert not gui.has_desktop()  # the bare base, booted to build a Task image
    driver.write_bytes(b"pe")
    assert gui.has_desktop()  # any Task image


def test_a_failed_call_starts_the_driver_once_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    started: list[bool] = []

    def invoke(tool: str, arguments: object) -> dict:
        calls.append(tool)
        if not started:
            raise gui.GuiUnavailable("not serving")
        return {"width": 10, "height": 20}

    monkeypatch.setattr(gui, "_invoke", invoke)
    monkeypatch.setattr(gui, "_start_server", lambda: started.append(True) or True)
    assert gui.screen_size() == (10, 20)
    assert calls == ["get_screen_size", "get_screen_size"]
    assert started == [True]
