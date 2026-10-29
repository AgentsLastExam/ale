from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from ale.core.errors import ProviderStartError, TaskDefinitionError
from ale.core.sandbox import PreparedTaskImage
from ale.core.taskspec import OperatingSystem
from ale.run import framework, guestd
from ale.run.framework import (
    LABEL,
    MARKER,
    MATERIALIZER_IMAGE,
    WINDOWS_MARKER,
    frame_disk,
    framed_disk,
    framework_identity,
    framework_on_disk,
    windows_framework_identity,
    with_framework,
)
from ale.run.guestd import gui

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def pythons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand-ins for the framework's Python builds, so no test downloads one."""
    root = tmp_path / "pythons"
    for platform in ("linux", "windows"):
        (root / platform / "python").mkdir(parents=True)
    monkeypatch.setattr(framework, "_python", lambda platform: root / platform / "python")
    return root


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pythons: Path
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
            built["context"] = argv[argv.index("--build-context") + 1]
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
    setup = ["/bin/sh", "/opt/ale/setup.sh", "app", "true", identity, "/tmp/python"]
    # The Python is mounted for the setup to install, never copied into the layer twice.
    assert (
        "RUN --mount=type=bind,from=python,target=/tmp/python " + json.dumps(setup)
    ) in dockerfile
    assert built["context"] == f"python={pythons / 'linux' / 'python'}"
    assert dockerfile.rstrip().splitlines()[-2] == "USER app"
    assert set(guestd.FILES) <= set(built["files"])
    assert "Environment=ALE_AGENT_USER=app" in built["unit"]
    assert f"--tcp 0.0.0.0:{guestd.PORT}" in built["unit"]
    # The same layer again is a no-op.
    images["task"] = {**images["task"], "Labels": {}}
    assert await with_framework("task", user="app", sudo=True, vm=True) == layered


@pytest.mark.asyncio
async def test_a_vm_disk_gets_the_same_pieces_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pythons: Path
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
    await frame_disk(
        disk, operating_system=OperatingSystem.LINUX, user="app", sudo=True, builder="builder:1"
    )
    argv = seen["argv"]
    assert isinstance(argv, tuple)
    assert argv[argv.index("--entrypoint") + 1] == "virt-customize"
    assert f"type=bind,src={tmp_path},dst=/disk" in argv
    assert argv[argv.index("--add") + 1] == "/disk/disk.qcow2"
    identity = framework_identity(user="app", sudo=True, vm=True)
    setup = argv[argv.index("--run-command") + 1]
    assert setup == f"/bin/sh /opt/ale/setup.sh app true {identity} /tmp/python"
    assert f"type=bind,src={pythons / 'linux' / 'python'},dst=/python,readonly" in argv
    assert "/python:/tmp" in argv
    assert argv[-2:] == ("--delete", "/tmp/python")  # after the setup installed it
    assert "/framework/ale-guestd.service:/etc/systemd/system/ale-guestd.service" in argv
    assert set(guestd.FILES) <= set(seen["files"])  # type: ignore[arg-type]
    assert "Environment=ALE_AGENT_USER=app" in str(seen["unit"])
    assert f"ExecStart={guestd.PYTHON['linux']} /opt/ale/guestd/main.py" in str(seen["unit"])


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
        await frame_disk(
            tmp_path / "disk.qcow2",
            operating_system=OperatingSystem.LINUX,
            user="app",
            sudo=False,
            builder="b",
        )
    assert str(failure.value).endswith(reason)


def test_a_framing_failure_gives_each_reason_once() -> None:
    """buildx repeats a failed step's output in its summary."""
    stderr = "#7 0.21 ALE: no glibc\n#7 ERROR: exit code 1\n > [4/4] RUN:\n0.21 ALE: no glibc\n"
    assert framework._explained(stderr) == "no glibc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operating_system", "marker"),
    [(OperatingSystem.LINUX, MARKER), (OperatingSystem.WINDOWS, WINDOWS_MARKER)],
)
async def test_the_framework_a_disk_carries_is_read_from_its_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operating_system: OperatingSystem,
    marker: str,
) -> None:
    identity = "sha256:" + "a" * 64
    answers = iter([(1, f"{identity}\n", "path must start with /"), (1, "", "No such file")])

    async def run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        assert argv[-1] == marker and "--network=none" in argv
        return next(answers)

    monkeypatch.setattr(framework, "_run", run)
    disk = tmp_path / "d.qcow2"
    found = await framework_on_disk(disk, operating_system=operating_system, builder="b")
    assert found == identity
    assert await framework_on_disk(disk, operating_system=operating_system, builder="b") is None


def _external_disk(path: str, digest: str) -> PreparedTaskImage:
    return PreparedTaskImage(
        kind="vm",
        source="external-ref",
        input_identity=digest,
        runtime_ref=path,
        prepared_identity=digest,
        resolved_reference=f"file://{path}",
    )


@pytest.fixture
def framing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """``framed_disk`` with the copy, the builder and the disks' markers stubbed out."""
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path / "cache"))
    seen: dict[str, object] = {"framed": [], "carried": {}}

    async def builder_identity() -> str:
        return "sha256:" + "9" * 64

    async def valid(path: Path) -> bool:
        return path.is_file()

    async def run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        assert argv[:2] == ("qemu-img", "convert")
        Path(argv[-1]).write_bytes(Path(argv[-2]).read_bytes())
        return 0, "", ""

    async def frame(
        disk: Path, *, operating_system: OperatingSystem, user: str, sudo: bool, builder: str
    ) -> None:
        assert builder == MATERIALIZER_IMAGE
        seen["framed"].append((disk, operating_system))  # type: ignore[attr-defined]

    async def on_disk(disk: Path, *, operating_system: OperatingSystem, builder: str) -> str | None:
        return seen["carried"].get(disk)  # type: ignore[attr-defined]

    monkeypatch.setattr(framework, "_builder_identity", builder_identity)
    monkeypatch.setattr(framework, "_valid_qcow2", valid)
    monkeypatch.setattr(framework, "_run", run)
    monkeypatch.setattr(framework, "frame_disk", frame)
    monkeypatch.setattr(framework, "framework_on_disk", on_disk)
    return seen


@pytest.mark.asyncio
async def test_a_vm_disk_is_framed_once_and_a_framed_disk_is_used_as_it_is(
    tmp_path: Path, framing: dict[str, object]
) -> None:
    source = tmp_path / "input.qcow2"
    source.write_bytes(b"disk")
    framed: list[tuple[Path, OperatingSystem]] = framing["framed"]  # type: ignore[assignment]
    carried: dict[Path, str | None] = framing["carried"]  # type: ignore[assignment]
    image = _external_disk(str(source), "sha256:" + "d" * 64)
    linux = {"operating_system": OperatingSystem.LINUX, "user": "user"}

    first, reused = await framed_disk(image, sudo=False, **linux)  # type: ignore[arg-type]
    assert not reused and len(framed) == 1
    assert first.runtime_ref != image.runtime_ref
    assert first.resolved_reference == image.resolved_reference
    assert first.prepared_identity != image.prepared_identity
    assert await framed_disk(image, sudo=False, **linux) == (first, True)  # type: ignore[arg-type]
    assert len(framed) == 1
    await framed_disk(image, sudo=True, **linux)  # type: ignore[arg-type]
    assert len(framed) == 2  # another framework is another copy

    ready = image.model_copy(update={"prepared_identity": "sha256:" + "e" * 64})
    carried[source] = framework_identity(user="user", sudo=False, vm=True)
    assert await framed_disk(ready, sudo=False, **linux) == (ready, True)  # type: ignore[arg-type]
    carried.clear()  # recorded once, not looked at again
    assert await framed_disk(ready, sudo=False, **linux) == (ready, True)  # type: ignore[arg-type]
    assert len(framed) == 2


@pytest.mark.asyncio
async def test_a_windows_disk_is_framed_for_its_account_whatever_sudo_says(
    tmp_path: Path, framing: dict[str, object]
) -> None:
    source = tmp_path / "windows.qcow2"
    source.write_bytes(b"disk")
    framed: list[tuple[Path, OperatingSystem]] = framing["framed"]  # type: ignore[assignment]
    carried: dict[Path, str | None] = framing["carried"]  # type: ignore[assignment]
    image = _external_disk(str(source), "sha256:" + "d" * 64)
    windows = {"operating_system": OperatingSystem.WINDOWS}

    first, reused = await framed_disk(image, user="user", sudo=True, **windows)  # type: ignore[arg-type]
    assert not reused and framed[0][1] is OperatingSystem.WINDOWS
    # The Host grants Administrator access per boot, so it is not in the disk.
    assert await framed_disk(image, user="user", sudo=False, **windows) == (first, True)  # type: ignore[arg-type]
    await framed_disk(image, user="other", sudo=True, **windows)  # type: ignore[arg-type]
    assert len(framed) == 2  # but the account is
    carried[source] = windows_framework_identity(user="user")
    ready = image.model_copy(update={"prepared_identity": "sha256:" + "e" * 64})
    assert await framed_disk(ready, user="user", sudo=True, **windows) == (ready, True)  # type: ignore[arg-type]
    assert len(framed) == 2


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


@pytest.mark.asyncio
async def test_qemu_frames_a_disk_unless_it_carries_exactly_this_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ale.core.sandbox import ResolvedImage, SandboxRequest
    from ale.core.taskspec import NetworkPolicy, Resources
    from ale.run.providers import qemu

    original = "sha256:" + "1" * 64
    framed = "sha256:" + "2" * 64
    prepared = _external_disk("/disks/task.qcow2", original)
    copy = prepared.model_copy(
        update={"runtime_ref": "/cache/framed.qcow2", "prepared_identity": framed}
    )
    answers = iter([prepared, copy, copy])
    asked: list[tuple[PreparedTaskImage, OperatingSystem, str, bool]] = []

    async def frame(
        image: PreparedTaskImage, *, operating_system: OperatingSystem, user: str, sudo: bool
    ) -> tuple:
        asked.append((image, operating_system, user, sudo))
        return next(answers), False

    monkeypatch.setattr(qemu, "framed_disk", frame)
    resolved = ResolvedImage(
        kind="vm",
        prepared_identity=original,
        observed_identity=original,
        observed_ref=prepared.runtime_ref,
    )
    request = SandboxRequest(
        episode_id="e1",
        prepared_image=prepared,
        resources=Resources(sudo=True),
        network=NetworkPolicy(),
        user="app",
        sudo=True,
    )
    provider = qemu.QemuProvider()
    assert await provider._framed(resolved, request) == resolved
    reframed = await provider._framed(resolved, request)
    assert reframed.prepared_identity == original  # what the request named, still
    assert (reframed.observed_identity, reframed.observed_ref) == (framed, copy.runtime_ref)
    assert asked == [(prepared, OperatingSystem.LINUX, "app", True)] * 2
    windows = request.model_copy(update={"os": OperatingSystem.WINDOWS})
    assert (await provider._framed(resolved, windows)).observed_ref == copy.runtime_ref
    assert asked[-1] == (prepared, OperatingSystem.WINDOWS, "app", True)


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


def test_the_framework_python_is_downloaded_once_and_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()  # the real _python, not the stand-ins
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path))
    payload = _tar({"python/bin/python3.12": b"elf", "python/lib/python3.12/os.py": b""})
    downloads: list[str] = []

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

    def urlopen(url: str, timeout: float) -> Response:
        downloads.append(url)
        return Response(payload)

    monkeypatch.setattr(framework.urllib.request, "urlopen", urlopen)
    asset = "cpython-3.12.0+1-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
    monkeypatch.setitem(framework._PYTHON, "linux", (asset, hashlib.sha256(payload).hexdigest()))
    tree = framework._python("linux")
    assert tree.name == "python" and (tree / "lib/python3.12/os.py").is_file()
    assert framework._python("linux") == tree
    assert len(downloads) == 1 and downloads[0].endswith(asset.replace("+", "%2B"))
    monkeypatch.setitem(framework._PYTHON, "windows", (asset, "0" * 64))
    with pytest.raises(Exception, match="pinned checksum"):
        framework._python("windows")


@pytest.mark.asyncio
async def test_a_windows_disk_is_framed_by_the_installer_inside_the_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pythons: Path
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "cua-driver.exe").write_bytes(b"pe")
    monkeypatch.setattr(framework, "_cua_driver", lambda platform: release)
    seen: dict[str, object] = {}

    async def run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        seen["argv"] = argv
        context = Path(
            next(a for a in argv if a.endswith(",dst=/framework,readonly")).split(",")[1][4:]
        )
        seen["files"] = sorted(
            str(path.relative_to(context)) for path in context.rglob("*") if path.is_file()
        )
        return 0, "", ""

    monkeypatch.setattr(framework, "_run", run)
    await frame_disk(
        tmp_path / "disk.qcow2",
        operating_system=OperatingSystem.WINDOWS,
        user="user",
        sudo=True,
        builder="builder:1",
    )
    argv = seen["argv"]
    assert isinstance(argv, tuple)
    assert argv[argv.index("--entrypoint") + 1] == "python3"
    assert "--network=none" in argv  # nothing to download: the pieces are staged
    install = argv[argv.index("builder:1") + 1 :]
    assert install[:4] == ("/framework/install.py", "/disk/disk.qcow2", "/framework", "/python")
    assert f"type=bind,src={pythons / 'windows' / 'python'},dst=/python,readonly" in argv
    assert install[install.index("--user") + 1] == "user"
    assert install[install.index("--identity") + 1] == windows_framework_identity(user="user")
    assert install[install.index("--runner-address") + 1] == guestd.HOST_ADDRESS
    assert {f"guestd/{name}" for name in guestd.FILES} | {
        "cua-driver/cua-driver.exe",
        "install.py",
    } == set(seen["files"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_windows_disk_that_cannot_be_framed_fails_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(framework, "_cua_driver", lambda platform: tmp_path)
    reason = "the agent account 'app' does not exist on this Windows disk"

    async def run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        return 1, "", f"Traceback...\nALE: {reason}\n"

    monkeypatch.setattr(framework, "_run", run)
    with pytest.raises(TaskDefinitionError) as failure:
        await frame_disk(
            tmp_path / "disk.qcow2",
            operating_system=OperatingSystem.WINDOWS,
            user="app",
            sudo=False,
            builder="b",
        )
    assert str(failure.value).endswith(reason)


def test_the_windows_identity_follows_everything_it_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = windows_framework_identity(user="user")
    assert windows_framework_identity(user="user") == base
    assert windows_framework_identity(user="admin") != base
    installer = tmp_path / "install.py"
    installer.write_text("changed\n")
    monkeypatch.setattr(framework, "_WINDOWS_INSTALLER", installer)
    assert windows_framework_identity(user="user") != base
    assert "service.py" in guestd.FILES  # the boot service is part of guestd's files


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
        return {"structuredContent": {"width": 10, "height": 20}}

    monkeypatch.setattr(gui, "_invoke", invoke)
    monkeypatch.setattr(gui, "_start_server", lambda: started.append(True) or True)
    assert gui.screen_size() == (10, 20)
    assert calls == ["get_screen_size", "get_screen_size"]
    assert started == [True]


def _driver_answers(monkeypatch: pytest.MonkeyPatch, *results: dict | None) -> list[list[str]]:
    """Answer each Cua Driver MCP run with the next tools/call result (None: no daemon)."""
    import subprocess

    runs: list[list[str]] = []
    answers = iter(results)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        runs.append(command)
        sent = [json.loads(line) for line in str(kwargs["input"]).splitlines()]
        assert [message.get("method") for message in sent] == [
            "initialize",
            "notifications/initialized",
            "tools/call",
        ]
        result = next(answers)
        if result is None:
            return subprocess.CompletedProcess(command, 1, "", "daemon is not running")
        replies = [
            {"jsonrpc": "2.0", "id": 0, "result": {}},
            {"jsonrpc": "2.0", "id": 1, "result": result},
        ]
        return subprocess.CompletedProcess(
            command, 0, "\n".join(json.dumps(reply) for reply in replies) + "\n", "update notice"
        )

    monkeypatch.setattr(gui, "_binary", lambda: "cua-driver")
    monkeypatch.setattr(gui.subprocess, "run", run)
    return runs


def test_a_driver_tool_succeeds_unless_it_reports_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A scroll answers with text alone; it still succeeded.
    _driver_answers(
        monkeypatch,
        {"content": [{"type": "text", "text": "Scrolled down"}]},
        {"content": [], "structuredContent": {"width": 1280, "height": 800}},
        {"content": [{"type": "image", "data": base64.b64encode(b"png").decode()}]},
        {"content": [{"type": "text", "text": "No window under screen point"}], "isError": True},
    )
    monkeypatch.setattr(gui, "_start_server", lambda: pytest.fail("the driver was running"))
    assert gui._call("scroll", {"x": 1, "y": 1}) == {}
    assert gui._call("get_screen_size") == {"width": 1280, "height": 800}
    assert gui.capture_screen() == b"png"
    with pytest.raises(gui.ActionFailed, match="click failed: No window under screen point"):
        gui._call("click", {"x": 99999, "y": 99999})


def test_an_unreachable_driver_is_started_and_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = _driver_answers(monkeypatch, None, {"structuredContent": {"x": 5, "y": 6}})
    started: list[bool] = []
    monkeypatch.setattr(gui, "_start_server", lambda: started.append(True) or True)
    assert gui._call("get_cursor_position") == {"x": 5, "y": 6}
    assert started == [True] and len(runs) == 2 and runs[0][1] == "mcp"
