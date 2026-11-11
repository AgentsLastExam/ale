from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
from pathlib import Path

import pytest
from tests.support import FakeQemuImg, write_qcow2
from typer.testing import CliRunner

from ale.core.errors import ProviderStartError
from ale.core.sandbox import ImageRef, PreparedTaskImage
from ale.core.taskspec import ImageKind
from ale.run.cli.main import app
from ale.run.images import (
    _acquire_lock,
    _select_repo_digest,
    base_layer,
    chain_identity,
    chain_mounts,
    layer_identity,
    publish_vm_image,
    record_layer,
    remove_disk,
    resolve_prepared_vm_image,
    resolve_vm_image,
    set_parent,
)

pytestmark = pytest.mark.unit


def test_vm_image_commands_replace_the_legacy_guest_command() -> None:
    help_text = CliRunner().invoke(app, ["--help"]).stdout
    assert "vm-image" in help_text
    assert "pull-guest" not in help_text


@pytest.mark.asyncio
async def test_vm_publish_wraps_the_checked_disk_at_the_pull_contract_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = write_qcow2(tmp_path / "windows.qcow2", data=b"qcow2")
    digest = "sha256:" + "7" * 64
    qemu_img = FakeQemuImg()

    async def valid(path: Path) -> bool:
        return path.is_file()

    async def fake_run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        if argv[0] == "qemu-img":
            return await qemu_img(*argv)
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
    # A layer would arrive without the disk below it.
    layer = write_qcow2(tmp_path / "layer.qcow2", backing="windows.qcow2")
    with pytest.raises(ProviderStartError, match="backing file"):
        await publish_vm_image(layer, reference)


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
async def test_vm_ref_resolution_rejects_wrong_kind(tmp_path: Path) -> None:
    with pytest.raises(ProviderStartError, match="kind=vm"):
        await resolve_vm_image(
            ImageRef(kind="container", reference="ghcr.io/acme/image:v1"),
            cache_dir=tmp_path,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dependency",
    [
        {"backing-filename": "base.qcow2"},
        {"format-specific": {"data": {"data-file": "external.raw"}}},
    ],
)
async def test_local_vm_ref_tracks_bytes_and_rejects_dependent_disks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dependency: dict[str, object]
) -> None:
    disk = tmp_path / "windows.qcow2"
    disk.write_bytes(b"first")
    metadata: dict[str, object] = {"format": "qcow2"}

    async def fake_run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        assert argv[0] == "qemu-img", "A local image must never reach Docker pull"
        return 0, json.dumps(metadata), ""

    monkeypatch.setattr("ale.run.images._run", fake_run)
    ref = ImageRef(kind="vm", reference=str(disk))
    first = await resolve_vm_image(ref)
    assert first.runtime_ref == str(disk)
    assert first.resolved_reference == disk.as_uri()
    assert first.prepared_identity == "sha256:" + hashlib.sha256(b"first").hexdigest()
    disk.write_bytes(b"other")
    assert (await resolve_vm_image(ref)).prepared_identity != first.prepared_identity
    metadata.update(dependency)
    with pytest.raises(ProviderStartError, match="standalone qcow2"):
        await resolve_vm_image(ref)
    disk.unlink()
    with pytest.raises(ProviderStartError, match="missing or invalid"):
        await resolve_vm_image(ref)


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


@pytest.mark.asyncio
async def test_registry_tag_resolves_metadata_without_pulling_layers(monkeypatch):
    from ale.run import images

    calls = []
    digest = "sha256:" + "b" * 64

    async def run(*argv, **kwargs):
        calls.append(argv)
        return 0, digest + "\n", ""

    monkeypatch.setattr(images, "_run", run)
    assert await images.resolve_registry_reference("registry.example:5443/team/macos:v1") == (
        "registry.example:5443/team/macos@" + digest
    )
    assert calls == [
        (
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--format",
            "{{.Manifest.Digest}}",
            "registry.example:5443/team/macos:v1",
        )
    ]
    assert await images.resolve_registry_reference("registry.example:5443/team/macos") == (
        "registry.example:5443/team/macos@" + digest
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "denied", "invalid", "timeout"])
async def test_registry_resolution_reports_safe_actionable_errors(monkeypatch, failure):
    from ale.run import images

    async def run(*argv, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("secret-path")
        if failure == "timeout":
            raise ProviderStartError("docker buildx timed out")
        if failure == "denied":
            return 1, "", "secret-token"
        return 0, "not-a-digest secret-token", ""

    monkeypatch.setattr(images, "_run", run)
    with pytest.raises(ProviderStartError) as error:
        await images.resolve_registry_reference("registry.example/macos:v1")
    assert "secret" not in str(error.value)


def _disk(path: Path, identity: str) -> PreparedTaskImage:
    return PreparedTaskImage(
        kind="vm",
        source="external-ref",
        input_identity=identity,
        runtime_ref=str(path),
        prepared_identity=identity,
        resolved_reference=path.as_uri(),
    )


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_a_base_is_copied_once_and_a_layer_is_known_by_its_whole_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qemu = FakeQemuImg()
    monkeypatch.setattr("ale.run.images._run", qemu)
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path / "cache"))
    published = "sha256:" + "b" * 64
    source = _disk(write_qcow2(tmp_path / "published.qcow2", data=b"windows"), published)

    base = await base_layer(source)
    assert base.parent == tmp_path / "cache" / "vm-builds"
    assert await base_layer(source) == base
    assert [call[1] for call in qemu.calls].count("convert") == 1
    assert "-c" not in next(call for call in qemu.calls if call[1] == "convert")

    task = write_qcow2(base.parent / "task.qcow2", backing=base.name, data=b"office")
    identity = await record_layer(task, published)
    assert identity == chain_identity(published, _digest(task))
    resolved = await resolve_prepared_vm_image(_disk(task, identity))
    assert (resolved.observed_identity, resolved.observed_ref) == (identity, str(task))
    with pytest.raises(ProviderStartError, match="changed"):
        await resolve_prepared_vm_image(_disk(task, published))

    # Another base at the same path is refused, though ALE recorded it too.
    base.with_suffix(".json").write_text(
        json.dumps({"parent": None, "identity": "sha256:" + "c" * 64})
    )
    with pytest.raises(ProviderStartError, match="was made on"):
        await layer_identity(task)


@pytest.mark.asyncio
async def test_a_chain_survives_a_move_and_containers_see_it_at_host_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ale.run.images._run", FakeQemuImg())
    published = "sha256:" + "b" * 64
    builds = tmp_path / "cache" / "vm-builds"
    base = write_qcow2(builds / "base.qcow2", data=b"windows")
    base.with_suffix(".json").write_text(json.dumps({"parent": None, "identity": published}))
    task = write_qcow2(builds / "task.qcow2", backing=base.name, data=b"office")
    task_identity = await record_layer(task, published)
    snapshot = write_qcow2(tmp_path / "cache/qemu-snapshots/e1/disk.qcow2", backing=str(task))
    await set_parent(snapshot, task)
    assert FakeQemuImg.backing(snapshot) == "../../vm-builds/task.qcow2"
    identity = await record_layer(snapshot, task_identity)

    (tmp_path / "cache").rename(tmp_path / "moved")
    moved = tmp_path / "moved/qemu-snapshots/e1/disk.qcow2"
    assert await layer_identity(moved) == identity
    assert (
        await layer_identity(moved, on=(tmp_path / "moved/vm-builds/task.qcow2", task_identity))
        == identity
    )
    with pytest.raises(ProviderStartError, match="is not built on"):
        await layer_identity(moved, on=(tmp_path / "other.qcow2", task_identity))
    mounts = await chain_mounts(moved, writable=True)
    layers = [
        moved,
        tmp_path / "moved/vm-builds/task.qcow2",
        tmp_path / "moved/vm-builds/base.qcow2",
    ]
    assert mounts == [
        item
        for index, layer in enumerate(layers)
        for item in (
            "--mount",
            f"type=bind,src={layer.resolve()},dst={layer.resolve()}"
            + (",readonly" if index else ""),
        )
    ]


@pytest.mark.asyncio
async def test_a_disk_another_cached_disk_builds_on_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr("ale.run.images._run", FakeQemuImg())
    published = "sha256:" + "b" * 64
    source = _disk(write_qcow2(tmp_path / "published.qcow2", data=b"windows"), published)
    base = await base_layer(source)
    # The base copy lost its record, so it would be made again: not while disks sit on it,
    # whether they name it relative to themselves (a snapshot) or by Host path (an overlay).
    base.with_suffix(".json").unlink()
    for child, backing in (
        (tmp_path / "qemu-snapshots/e1/disk.qcow2", f"../../vm-builds/{base.name}"),
        (tmp_path / "qemu/e2/data.qcow2", str(base)),
    ):
        write_qcow2(child, backing=backing)
        with pytest.raises(ProviderStartError, match=rf"{child.parent.name}/{child.name}"):
            await base_layer(source)
        assert base.read_bytes().endswith(b"windows")
        child.unlink()
    assert await base_layer(source) == base

    # Removing a disk takes its record with it.
    await remove_disk(base)
    assert not base.exists() and not base.with_suffix(".json").exists()
