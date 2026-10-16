from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from ale.core.errors import ProviderStartError, TaskDefinitionError
from ale.core.sandbox import ExecResult
from ale.run import framework, guestd
from ale.run.framework import (
    LABEL,
    MARKER,
    frame_disk,
    framework_identity,
    framework_on_disk,
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
    identity = framework_identity(user="app", sudo=True, vm=True)
    assert json.dumps(["/bin/sh", "/opt/ale/setup.sh", "app", "true", identity]) in dockerfile
    assert dockerfile.rstrip().splitlines()[-2] == "USER app"
    assert set(guestd.FILES) <= set(built["files"])
    assert "Environment=ALE_AGENT_USER=app" in built["unit"]
    assert f"--tcp 0.0.0.0:{guestd.PORT}" in built["unit"]
    # The same layer again is a no-op.
    images["task"] = {**images["task"], "Labels": {}}
    assert await with_framework("task", user="app", sudo=True, vm=True) == layered


@pytest.mark.asyncio
async def test_a_vm_disk_gets_the_same_pieces_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = tmp_path / "cua-driver-release"
    driver.mkdir()
    (driver / "cua-driver").write_bytes(b"driver")
    monkeypatch.setattr(framework, "_cua_driver", lambda platform: driver)
    seen: dict[str, object] = {}

    async def run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        seen["argv"] = argv
        context = Path(
            next(a for a in argv if a.endswith(",dst=/framework,readonly")).split(",")[1][4:]
        )
        seen["files"] = sorted(path.name for path in context.rglob("*"))
        seen["unit"] = (context / "ale-guestd.service").read_text()
        return 0, "", ""

    monkeypatch.setattr(framework, "_run", run)
    disk = tmp_path / "disk.qcow2"
    await frame_disk(disk, user="app", sudo=True, builder="builder:1")
    argv = seen["argv"]
    assert isinstance(argv, tuple)
    assert argv[argv.index("--entrypoint") + 1] == "virt-customize"
    assert f"type=bind,src={tmp_path},dst=/disk" in argv
    assert argv[argv.index("--add") + 1] == "/disk/disk.qcow2"
    identity = framework_identity(user="app", sudo=True, vm=True)
    assert argv[-1] == f"/bin/sh /opt/ale/setup.sh app true {identity}"
    assert "/framework/ale-guestd.service:/etc/systemd/system/ale-guestd.service" in argv
    assert set(guestd.FILES) <= set(seen["files"])  # type: ignore[arg-type]
    assert "Environment=ALE_AGENT_USER=app" in str(seen["unit"])


@pytest.mark.asyncio
async def test_a_disk_that_cannot_be_framed_fails_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(framework, "_cua_driver", lambda platform: tmp_path)
    (tmp_path / "cua-driver").write_bytes(b"driver")
    reason = "the agent account 'app' does not exist in this image"

    async def run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        return 1, f"ALE: {reason}\n", "virt-customize: error: command failed"

    monkeypatch.setattr(framework, "_run", run)
    with pytest.raises(TaskDefinitionError) as failure:
        await frame_disk(tmp_path / "disk.qcow2", user="app", sudo=False, builder="b")
    assert str(failure.value).endswith(reason)


@pytest.mark.asyncio
async def test_the_framework_a_disk_carries_is_read_from_its_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = "sha256:" + "a" * 64
    answers = iter([(1, f"{identity}\n", "path must start with /"), (1, "", "No such file")])

    async def run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        assert argv[-1] == MARKER and "--network=none" in argv
        return next(answers)

    monkeypatch.setattr(framework, "_run", run)
    assert await framework_on_disk(tmp_path / "d.qcow2", builder="b") == identity
    assert await framework_on_disk(tmp_path / "d.qcow2", builder="b") is None


@pytest.mark.asyncio
async def test_a_framing_container_does_not_outlive_its_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:2] == ("docker", "run"):
            raise ProviderStartError("docker run --rm timed out after 1s")
        return 0, "", ""

    monkeypatch.setattr(framework, "_run", run)
    with pytest.raises(ProviderStartError):
        await framework._in_container("builder", timeout=1)
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ("docker", "rm", "--force", name)


@pytest.mark.asyncio
async def test_docker_frames_an_image_unless_it_carries_exactly_this_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from ale.core.sandbox import ResolvedImage
    from ale.core.taskspec import ImageKind
    from ale.run.providers import docker

    original = "sha256:" + "1" * 64
    framed = "sha256:" + "2" * 64
    answers = iter([original, framed])

    async def layer(image: str, *, user: str, sudo: bool) -> str:
        return next(answers)

    monkeypatch.setattr(docker, "with_framework", layer)
    resolved = ResolvedImage(
        kind=ImageKind.CONTAINER,
        prepared_identity=original,
        observed_identity=original,
        observed_ref="ghcr.io/acme/task@sha256:" + "3" * 64,
    )
    request = SimpleNamespace(user="user", sudo=False)
    provider = docker.DockerProvider()
    assert await provider._framed(resolved, request) == resolved  # type: ignore[arg-type]
    reframed = await provider._framed(resolved, request)  # type: ignore[arg-type]
    assert (reframed.observed_identity, reframed.observed_ref) == (framed, framed)


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
