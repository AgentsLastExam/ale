from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
import zstandard
from aiohttp import web
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
    record_layer,
    remove_disk,
    resolve_prepared_vm_image,
    resolve_vm_image,
    set_parent,
)

pytestmark = pytest.mark.unit


def test_vm_image_commands_fail_with_the_reference_exit_code(tmp_path: Path) -> None:
    for command in (["pull", "acme/disk:1", str(tmp_path / "d")], ["push", "d", "acme/disk:1"]):
        result = CliRunner().invoke(app, ["vm-image", *command])
        assert result.exit_code == 3, result.output
        assert "registry host" in result.output


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
        assert argv[0] == "qemu-img", "A local image must never reach a registry"
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


# --- chunked VM disks through an OCI registry ---

_PATH = re.compile(r"/v2/(?P<name>.+)/(?P<kind>blobs/uploads|blobs|manifests)/(?P<ref>[^/]*)")


class _Registry:
    """The OCI distribution endpoints a VM disk push and pull use, with token auth."""

    def __init__(self, *, redirect: bool = False) -> None:
        self.redirect = redirect
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, bytes] = {}
        self.reads: list[tuple[str, int]] = []
        self.token = "episode-free-token"
        self.token_requests: list[str | None] = []
        self.uploads = 0
        self.storage_authorization: list[str | None] = []
        #: Blobs whose next read stops halfway through, as a dropped connection does.
        self.drop: set[str] = set()
        self.host = ""
        self._runner: web.AppRunner | None = None

    async def __aenter__(self) -> _Registry:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.host = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # type: ignore[union-attr]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._runner is not None
        await self._runner.cleanup()

    def manifest(self, tag: str) -> dict[str, Any]:
        return json.loads(self.manifests[tag])

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        if path == "/token":
            self.token_requests.append(request.headers.get("Authorization"))
            return web.json_response({"token": self.token})
        if path.startswith("/storage/"):
            self.storage_authorization.append(request.headers.get("Authorization"))
            return await self._serve(request, path.removeprefix("/storage/"))
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            challenge = f'Bearer realm="http://{self.host}/token",service="fake"'
            return web.Response(status=401, headers={"WWW-Authenticate": challenge})
        if path == "/v2/":
            return web.Response()
        match = _PATH.fullmatch(path)
        assert match is not None, path
        kind, ref = match["kind"], match["ref"]
        if kind == "blobs/uploads":
            if request.method == "POST":
                self.uploads += 1
                return web.Response(status=202, headers={"Location": f"{path}{ref}x?_state=a%3D"})
            data = await request.read()
            digest = request.query["digest"]
            assert request.query["_state"] == "a="
            assert digest == f"sha256:{hashlib.sha256(data).hexdigest()}"
            self.blobs[digest] = data
            return web.Response(status=201)
        if kind == "blobs":
            if ref not in self.blobs:
                return web.Response(status=404)
            if self.redirect:  # as a registry keeping blobs in object storage does, HEAD too
                return web.Response(status=307, headers={"Location": f"/storage/{ref}"})
            if request.method == "HEAD":
                return web.Response()
            return await self._serve(request, ref)
        if request.method == "PUT":
            data = await request.read()
            digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
            self.manifests[ref] = self.manifests[digest] = data
            return web.Response(status=201, headers={"Docker-Content-Digest": digest})
        if ref not in self.manifests:
            return web.Response(status=404)
        body = b"" if request.method == "HEAD" else self.manifests[ref]
        return web.Response(body=body, content_type=json.loads(self.manifests[ref])["mediaType"])

    async def _serve(self, request: web.Request, digest: str) -> web.StreamResponse:
        data = self.blobs[digest]
        start = int(request.headers.get("Range", "bytes=0-")[6:].rstrip("-"))
        self.reads.append((digest, start))
        response = web.StreamResponse(status=206 if start else 200)
        response.content_length = len(data) - start
        await response.prepare(request)
        if digest in self.drop:
            self.drop.discard(digest)
            await response.write(data[start : start + (len(data) - start) // 2])
            assert request.transport is not None
            request.transport.close()
            return response
        await response.write(data[start:])
        return response


@pytest.fixture
def chunked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """Four 64 KiB blocks per chunk read in 16 KiB pieces, a fake qemu-img, no login."""
    from ale.run import images

    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "no-docker-config"))
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(images, "_run", FakeQemuImg())
    monkeypatch.setattr(images, "CHUNK_SIZE", 4 * images._SPARSE)
    monkeypatch.setattr(images, "_BLOCK", 16 << 10)
    return 4 * images._SPARSE


def _chunked_disk(path: Path, chunk: int) -> bytes:
    """A qcow2 file of three and a half chunks; the second half of the second is zeros."""
    data = os.urandom(chunk - 32) + os.urandom(chunk // 2) + bytes(chunk // 2)
    data += os.urandom(chunk) + os.urandom(chunk // 2)
    return write_qcow2(path, data=data).read_bytes()


@pytest.mark.asyncio
async def test_vm_disk_round_trip_is_chunked_verified_and_sparse(
    tmp_path: Path, chunked: int
) -> None:
    from ale.run import images

    source = tmp_path / "base.qcow2"
    data = _chunked_disk(source, chunked)
    async with _Registry(redirect=True) as registry:
        reference = f"{registry.host}/acme/windows:1.0.0"
        published = await images.publish_vm_image(source, reference)
        digest = published.rpartition("@")[2]
        manifest = registry.manifest("1.0.0")
        assert manifest["artifactType"] == images.VM_DISK
        assert manifest["annotations"] == {
            "ale.vm-disk.size": str(len(data)),
            "ale.vm-disk.chunk-size": str(chunked),
        }
        layers = manifest["layers"]
        assert [layer["mediaType"] for layer in layers] == [images.VM_DISK_CHUNK] * 4
        for index, layer in enumerate(layers):
            piece = data[index * chunked : (index + 1) * chunked]
            content = layer["annotations"]["ale.vm-disk.chunk.digest"]
            assert content == f"sha256:{hashlib.sha256(piece).hexdigest()}"
            assert zstandard.ZstdDecompressor().decompress(registry.blobs[layer["digest"]]) == piece

        with pytest.raises(ProviderStartError, match="already published"):
            await images.publish_vm_image(source, reference)
        with pytest.raises(ProviderStartError, match="under a tag"):
            await images.publish_vm_image(source, f"{registry.host}/acme/windows@{digest}")
        uploads = registry.uploads
        await images.publish_vm_image(source, f"{registry.host}/acme/windows:1.0.1")
        assert registry.uploads == uploads, "chunks the repository has are not sent again"

        target = tmp_path / "pulled" / "disk.qcow2"
        assert await images.pull_vm_image(reference, target) == (
            f"{registry.host}/acme/windows@{digest}"
        )
        # Blob reads followed the redirect to storage without the registry's token.
        assert registry.storage_authorization == [None] * 4
    assert target.read_bytes() == data
    with target.open("rb") as stream:
        # The zeros stay a hole, however the decompressor happened to split its output.
        zeros = chunked + chunked // 2
        assert os.lseek(stream.fileno(), chunked, os.SEEK_HOLE) == zeros
        assert os.lseek(stream.fileno(), zeros, os.SEEK_DATA) == 2 * chunked
    assert not target.with_name("disk.qcow2.partial").exists()


@pytest.mark.asyncio
async def test_vm_disk_pull_resumes_kept_chunks_and_interrupted_reads(
    tmp_path: Path, chunked: int
) -> None:
    from ale.run import images

    source = tmp_path / "base.qcow2"
    data = _chunked_disk(source, chunked)
    async with _Registry() as registry:
        reference = f"{registry.host}/acme/windows:1.0.0"
        await images.publish_vm_image(source, reference)
        blobs = [layer["digest"] for layer in registry.manifest("1.0.0")["layers"]]
        # An earlier pull wrote the first chunk, and garbage where the second chunk's zeros
        # belong; the rest of the disk was never written.
        target = tmp_path / "disk.qcow2"
        partial = tmp_path / "disk.qcow2.partial"
        with partial.open("wb") as stream:
            stream.write(data[:chunked] + os.urandom(chunked))
            stream.truncate(len(data))
        registry.drop.add(blobs[2])
        await images.pull_vm_image(reference, target)
    assert target.read_bytes() == data
    reads = sorted(registry.reads)
    assert blobs[0] not in {digest for digest, _ in reads}, "a kept chunk is not fetched"
    assert [start for digest, start in reads if digest == blobs[2]][-1] > 0, "read resumed"


@pytest.mark.asyncio
async def test_vm_disk_pull_rejects_wrong_bytes_and_foreign_manifests(
    tmp_path: Path, chunked: int
) -> None:
    from ale.run import images

    source = tmp_path / "base.qcow2"
    _chunked_disk(source, chunked)
    async with _Registry() as registry:
        reference = f"{registry.host}/acme/windows:1.0.0"
        digest = (await images.publish_vm_image(source, reference)).rpartition("@")[2]
        layer = registry.manifest("1.0.0")["layers"][1]["digest"]
        original = registry.blobs[layer]
        registry.blobs[layer] = zstandard.ZstdCompressor().compress(b"tampered")
        with pytest.raises(ProviderStartError, match="failed verification"):
            await images.pull_vm_image(reference, tmp_path / "disk.qcow2")
        registry.blobs[layer] = os.urandom(len(original))
        with pytest.raises(ProviderStartError, match="not valid zstd"):
            await images.pull_vm_image(reference, tmp_path / "disk.qcow2")
        registry.blobs[layer] = original
        (tmp_path / "directory").mkdir()
        with pytest.raises(ProviderStartError, match="could not write"):
            await images.pull_vm_image(reference, tmp_path / "directory")
        altered = registry.manifest("1.0.0")
        altered["layers"][1]["annotations"]["ale.vm-disk.chunk.digest"] = f"sha256:{'1' * 64}"
        registry.manifests["altered"] = json.dumps(altered).encode()
        with pytest.raises(ProviderStartError, match="failed verification"):
            await images.pull_vm_image(
                f"{registry.host}/acme/windows:altered", tmp_path / "disk.qcow2"
            )

        wrong = f"{registry.host}/acme/windows@sha256:{'0' * 64}"
        registry.manifests[f"sha256:{'0' * 64}"] = registry.manifests[digest]
        with pytest.raises(ProviderStartError, match="different manifest"):
            await images.pull_vm_image(wrong, tmp_path / "disk.qcow2")

        registry.manifests["docker-image"] = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                "layers": [],
            }
        ).encode()
        with pytest.raises(ProviderStartError, match="not an ALE VM disk"):
            await images.pull_vm_image(f"{registry.host}/acme/windows:docker-image", tmp_path / "x")
    assert not (tmp_path / "disk.qcow2").exists()


@pytest.mark.asyncio
async def test_vm_disk_resolution_caches_by_manifest_digest(tmp_path: Path, chunked: int) -> None:
    from ale.run import images

    source = tmp_path / "base.qcow2"
    data = _chunked_disk(source, chunked)
    cache = tmp_path / "cache"
    async with _Registry() as registry:
        tag = f"{registry.host}/acme/windows:1.0.0"
        digest = (await images.publish_vm_image(source, tag)).rpartition("@")[2]
        image = ImageRef(kind="vm", reference=tag)
        first, second = await asyncio.gather(
            images.resolve_vm_image(image, cache_dir=cache),
            images.resolve_vm_image(image, cache_dir=cache),
        )
        assert len(registry.reads) == 4, "concurrent resolutions download once"
    assert first == second
    assert first.kind is ImageKind.VM
    assert first.prepared_identity == first.input_identity == digest
    assert first.resolved_reference == f"{registry.host}/acme/windows@{digest}"
    assert Path(first.runtime_ref) == cache / digest.removeprefix("sha256:") / "disk.qcow2"
    assert Path(first.runtime_ref).read_bytes() == data
    # The registry is gone: a digest-pinned disk already in the cache needs none.
    pinned = ImageRef(kind="vm", reference=first.resolved_reference)
    assert await images.resolve_vm_image(pinned, cache_dir=cache) == first

    # The pulled disk is the base of the layers built on it, not copied.
    qemu = images._run
    assert isinstance(qemu, FakeQemuImg)
    disk = Path(first.runtime_ref)
    assert await base_layer(first) == disk
    assert not any(call[1] == "convert" for call in qemu.calls)
    # A layer sits on it, so it is never replaced, even when it would be pulled again.
    write_qcow2(tmp_path / "cache" / "vm-builds" / "task.qcow2", backing=str(disk))
    disk.write_bytes(b"damaged")
    async with _Registry() as again:
        again.blobs, again.manifests = registry.blobs, registry.manifests
        pinned = ImageRef(kind="vm", reference=f"{again.host}/acme/windows@{digest}")
        with pytest.raises(ProviderStartError, match="is built on it"):
            await images.resolve_vm_image(pinned, cache_dir=cache)
    assert disk.read_bytes() == b"damaged"


@pytest.mark.asyncio
async def test_vm_disk_publish_refuses_layers_and_compressed_disks(
    tmp_path: Path, chunked: int
) -> None:
    from ale.run import images

    qemu = images._run
    assert isinstance(qemu, FakeQemuImg)
    base = write_qcow2(tmp_path / "base.qcow2", data=b"windows")
    layer = write_qcow2(tmp_path / "layer.qcow2", backing="base.qcow2")
    qemu.compressed.add(base)
    async with _Registry() as registry:
        reference = f"{registry.host}/acme/windows:1.0.0"
        # A layer would arrive without the disk below it.
        with pytest.raises(ProviderStartError, match="standalone qcow2"):
            await images.publish_vm_image(layer, reference)
        with pytest.raises(ProviderStartError, match="compressed clusters"):
            await images.publish_vm_image(base, reference)
    assert registry.uploads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["auths", "helper", "anonymous"])
async def test_registry_uses_docker_credentials_for_its_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: str
) -> None:
    from ale.run.oci import Reference, Registry

    async with _Registry() as registry:
        login = "Basic " + base64.b64encode(b"robot:secret").decode()
        config: dict[str, object] = {}
        if store == "auths":
            config = {
                "auths": {registry.host: {"auth": base64.b64encode(b"robot:secret").decode()}}
            }
        elif store == "helper":
            config = {"credHelpers": {registry.host: "fake"}}
            helper = tmp_path / "bin" / "docker-credential-fake"
            helper.parent.mkdir()
            helper.write_text(
                "#!/bin/sh\nread host\n"
                f'[ "$host" = "{registry.host}" ] || exit 1\n'
                'echo \'{"Username": "robot", "Secret": "secret"}\'\n'
            )
            helper.chmod(0o755)
            monkeypatch.setenv("PATH", f"{helper.parent}:{os.environ['PATH']}")
        (tmp_path / "config.json").write_text(json.dumps(config))
        monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
        reference = Reference.parse(f"{registry.host}/acme/windows:1.0.0")
        async with Registry(reference) as client:
            assert not await client.has_manifest("1.0.0")
            registry.token = "renewed-token"  # the first token expired
            assert not await client.has_manifest("1.0.0")
    assert registry.token_requests == [None if store == "anonymous" else login] * 2


def test_vm_disk_references_name_their_registry() -> None:
    from ale.run.oci import Reference, _on_this_machine

    parsed = Reference.parse("localhost:5000/team/disk:1.0@sha256:" + "a" * 64)
    assert (parsed.registry, parsed.repository, parsed.tag) == (
        "localhost:5000",
        "team/disk",
        "1.0",
    )
    assert parsed.pinned("sha256:" + "b" * 64) == "localhost:5000/team/disk@sha256:" + "b" * 64
    for invalid in ("acme/disk:1", "ghcr.io/Acme/disk:1", "ghcr.io/acme/disk:1@md5:00"):
        with pytest.raises(ProviderStartError, match="registry host"):
            Reference.parse(invalid)
    local = ("localhost:5000", "127.0.0.1:5000", "[::1]:5000", "127.example.com", "ghcr.io")
    assert [_on_this_machine(host) for host in local] == [True, True, True, False, False]
