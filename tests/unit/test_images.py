from __future__ import annotations

import asyncio
import fcntl
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest
from typer.testing import CliRunner

from ale.core.errors import ProviderStartError
from ale.core.sandbox import ImageRef
from ale.core.taskspec import ImageKind
from ale.run.cli.main import app
from ale.run.images import _acquire_lock, _select_repo_digest, publish_vm_image, resolve_vm_image

pytestmark = pytest.mark.unit


def test_windows_arm_builder_uses_the_specialize_bootstrap_chain() -> None:
    root = Path(__file__).parents[2]
    image = root / "images/base/vm-windows11-arm64"
    answer = (image / "Autounattend.xml").read_text()
    ElementTree.fromstring(answer)

    assert "FirstLogonCommands" not in answer
    assert "bootstrap-system.ps1 -InstallTask" in answer
    assert "<Value>ale</Value>" in answer

    build = (image / "build.sh").read_text()
    assert 'cp "$source_dir/bootstrap-system.ps1" "$config/"' in build
    assert 'cp "$source_dir/bootstrap-user.ps1" "$config/"' in build

    installer = (image / "install.ps1").read_text()
    assert '$_.FullName -match "\\\\ARM64\\\\"' in installer
    assert "$LASTEXITCODE -notin 0, 259, 3010" in installer


def test_vm_image_commands_replace_the_legacy_guest_command() -> None:
    help_text = CliRunner().invoke(app, ["--help"]).stdout
    assert "vm-image" in help_text
    assert "pull-guest" not in help_text


def test_vm_image_pull_failure_has_a_nonzero_cli_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise ProviderStartError("registry denied the image")

    monkeypatch.setattr("ale.run.images.resolve_vm_image", fail)
    result = CliRunner().invoke(
        app,
        ["vm-image", "pull", "ghcr.io/acme/private:v1", "output.qcow2"],
    )
    assert result.exit_code != 0
    assert "registry denied the image" in result.stderr


@pytest.mark.asyncio
async def test_vm_publish_wraps_the_checked_disk_at_the_pull_contract_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = tmp_path / "windows.qcow2"
    disk.write_bytes(b"qcow2")
    digest = "sha256:" + "7" * 64

    async def valid(path: Path) -> bool:
        return path == disk

    async def fake_run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        assert argv[:3] == ("docker", "buildx", "build")
        assert "--push" in argv
        assert timeout == 7200
        context = Path(argv[-1])
        assert (context / "disk.qcow2").samefile(disk)
        assert (context / "Dockerfile").read_text() == (
            "FROM scratch\nCOPY disk.qcow2 /disk.qcow2\n"
        )
        metadata = Path(argv[argv.index("--metadata-file") + 1])
        metadata.write_text(json.dumps({"containerimage.digest": digest}))
        return 0, "", ""

    monkeypatch.setattr("ale.run.images._valid_qcow2", valid)
    monkeypatch.setattr("ale.run.images._run", fake_run)

    reference = "ghcr.io/acme/windows:v1"
    assert await publish_vm_image(disk, reference) == f"{reference}@{digest}"
    assert not list(tmp_path.glob(".ale-vm-publish-*"))


def test_selects_the_digest_for_the_declared_repository() -> None:
    assert (
        _select_repo_digest(
            "ghcr.io/acme/image:latest",
            [
                "docker.io/other/image@sha256:" + "1" * 64,
                "ghcr.io/acme/image@sha256:" + "2" * 64,
            ],
        )
        == "ghcr.io/acme/image@sha256:" + "2" * 64
    )
    with pytest.raises(ProviderStartError):
        _select_repo_digest("ghcr.io/acme/missing:latest", [])
    assert _select_repo_digest(
        "docker.io/library/python:3.12",
        ["python@sha256:" + "3" * 64],
    ).startswith("python@sha256:")


def test_async_lock_wait_does_not_block_the_event_loop(tmp_path: Path) -> None:
    path = tmp_path / "image.lock"
    path.touch()

    async def exercise() -> None:
        with path.open("r+") as held, path.open("r+") as waiting:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            task = asyncio.create_task(_acquire_lock(waiting))
            await asyncio.sleep(0.02)
            assert not task.done()
            fcntl.flock(held, fcntl.LOCK_UN)
            await asyncio.wait_for(task, timeout=1)
            fcntl.flock(waiting, fcntl.LOCK_UN)

    asyncio.run(exercise())


@pytest.mark.asyncio
async def test_vm_ref_cache_uses_registry_digest_without_disk_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = "sha256:" + "4" * 64
    entry = tmp_path / digest.removeprefix("sha256:")
    entry.mkdir()
    disk = entry / "disk.qcow2"
    disk.write_bytes(b"structurally-valid-fixture")
    commands: list[tuple[str, ...]] = []

    async def fake_run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        commands.append(argv)
        if argv[:3] == ("docker", "image", "inspect"):
            return 0, f"ghcr.io/acme/vm@{digest}\n", ""
        return 0, "", ""

    monkeypatch.setattr("ale.run.images._run", fake_run)
    image = ImageRef(kind="vm", reference="ghcr.io/acme/vm:v1")
    first = await resolve_vm_image(image, cache_dir=tmp_path)
    second = await resolve_vm_image(image, cache_dir=tmp_path)
    assert first == second
    assert first.kind is ImageKind.VM
    assert first.prepared_identity == digest
    assert first.runtime_ref == str(disk.resolve())
    assert not (entry / "content.sha256").exists()
    assert all("sha256sum" not in command for command in commands)


@pytest.mark.asyncio
async def test_vm_ref_uses_crane_when_docker_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = "sha256:" + "8" * 64
    entry = tmp_path / digest.removeprefix("sha256:")
    entry.mkdir()
    disk = entry / "disk.qcow2"
    disk.write_bytes(b"structurally-valid-fixture")
    commands: list[tuple[str, ...]] = []

    async def fake_run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        commands.append(argv)
        if argv[0] == "docker":
            raise FileNotFoundError("docker")
        if argv[:2] == ("crane", "digest"):
            return 0, f"{digest}\n", ""
        if argv[:2] == ("qemu-img", "check"):
            return 0, "", ""
        raise AssertionError(argv)

    monkeypatch.setattr("ale.run.images._run", fake_run)
    monkeypatch.setattr(
        "ale.run.images.shutil.which",
        lambda binary: "/opt/homebrew/bin/crane" if binary == "crane" else None,
    )
    prepared = await resolve_vm_image(
        ImageRef(kind="vm", reference="ghcr.io/acme/vm-windows11-arm64-base:v1"),
        cache_dir=tmp_path,
    )
    assert prepared.resolved_reference == f"ghcr.io/acme/vm-windows11-arm64-base@{digest}"
    assert any(command[:2] == ("crane", "digest") for command in commands)


@pytest.mark.asyncio
async def test_vm_ref_resolution_rejects_wrong_kind(tmp_path: Path) -> None:
    with pytest.raises(ProviderStartError, match="kind=vm"):
        await resolve_vm_image(
            ImageRef(kind="container", reference="ghcr.io/acme/image:v1"),
            cache_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_vm_ref_extracts_once_and_publishes_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = "sha256:" + "6" * 64
    copies = 0

    async def fake_run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        nonlocal copies
        if argv[:3] == ("docker", "image", "inspect"):
            return 0, f"ghcr.io/acme/vm@{digest}\n", ""
        if argv[:2] == ("docker", "create"):
            return 0, "container-id\n", ""
        if argv[:2] == ("docker", "cp"):
            copies += 1
            Path(argv[-1]).write_bytes(b"qcow2")
        return 0, "", ""

    async def valid(path: Path) -> bool:
        return path.is_file() and path.read_bytes() == b"qcow2"

    monkeypatch.setattr("ale.run.images._run", fake_run)
    monkeypatch.setattr("ale.run.images._valid_qcow2", valid)
    image = ImageRef(kind="vm", reference="ghcr.io/acme/vm:v1")
    first, second = await asyncio.gather(
        resolve_vm_image(image, cache_dir=tmp_path),
        resolve_vm_image(image, cache_dir=tmp_path),
    )
    assert first == second
    assert copies == 1
    assert Path(first.runtime_ref).read_bytes() == b"qcow2"
    assert not list(tmp_path.glob(".*-*"))
