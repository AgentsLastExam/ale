"""Provider-owned immutable image acquisition and offline validation.

A VM disk ALE prepares from another disk is a chain of qcow2 layers: a base, then
overlays, each holding only what changed on the one below it. Beside every layer ALE makes
is its record: the identity of the layer it sits on and the SHA-256 of its own file, from
which its identity follows as an OCI ChainID does. A base's identity is the one it was
resolved under. A layer names its parent by a path relative to itself, so a chain stays
whole when the cache around it moves, and a container that is to open it gets each layer
at its Host path.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO

import aiohttp
import zstandard

from ale.core.errors import ProviderStartError
from ale.core.ids import content_hash
from ale.core.sandbox import ImageRef, PreparedTaskImage, ResolvedImage
from ale.core.taskspec import ImageKind
from ale.run.oci import MANIFEST, Reference, Registry, retrying
from ale.run.sources import cache_root

__all__ = [
    "base_layer",
    "chain_identity",
    "chain_mounts",
    "finished_layer",
    "layer_identity",
    "layer_parent",
    "publish_vm_image",
    "pull_vm_image",
    "read_layer_record",
    "record_layer",
    "remove_disk",
    "resolve_container_image",
    "resolve_container_reference",
    "resolve_loaded_image",
    "resolve_local_vm_fixture",
    "resolve_prepared_container_image",
    "resolve_prepared_vm_image",
    "resolve_registry_reference",
    "resolve_vm_image",
    "set_parent",
]

#: A VM disk in a registry is an OCI artifact of this type. Its layers are the standalone
#: qcow2 file cut into consecutive chunks, each compressed alone with zstd, so no layer
#: nears a registry's layer limit, a pull reads several chunks at once and resumes chunk
#: by chunk, and every chunk is written at its offset into the one disk the runtime uses.
VM_DISK = "application/vnd.ale.vm-disk.v1"
VM_DISK_CHUNK = "application/vnd.ale.vm-disk.chunk.v1+zstd"
#: Manifest annotations: the disk file's size, and the size of every chunk but the last.
_DISK_SIZE = "ale.vm-disk.size"
_DISK_CHUNK_SIZE = "ale.vm-disk.chunk-size"
#: Layer annotation: the SHA-256 of the chunk's uncompressed bytes.
_CHUNK_DIGEST = "ale.vm-disk.chunk.digest"
_EMPTY_CONFIG = {
    "mediaType": "application/vnd.oci.empty.v1+json",
    "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "size": 2,
}
#: Chunk size, compression level and parallelism, measured in the providers README.
CHUNK_SIZE = 512 << 20
_ZSTD_LEVEL = 3
TRANSFERS = 8
#: Fewer uploads than downloads: each must finish within GHCR's ten minutes.
_UPLOADS = 4
_BLOCK = 8 << 20
#: Zero runs this long, the default qcow2 cluster size, are left as holes when pulled.
_SPARSE = 64 << 10
_ZEROS = bytes(_SPARSE)


def _select_repo_digest(declared: str, repo_digests: list[str]) -> str:
    def canonical(repository: str) -> str:
        for prefix in ("docker.io/", "index.docker.io/"):
            if repository.startswith(prefix):
                repository = repository.removeprefix(prefix)
                break
        return repository.removeprefix("library/")

    repository = canonical(declared.rsplit("@", 1)[0].rsplit(":", 1)[0])
    for reference in repo_digests:
        if canonical(reference.partition("@")[0]) == repository:
            return reference
    raise ProviderStartError(f"docker returned no immutable identity for {declared}")


async def _acquire_lock(handle: TextIO) -> None:
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            await asyncio.sleep(0.05)


async def _run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        raise ProviderStartError(f"{' '.join(argv[:2])} timed out after {timeout:g}s") from None
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def resolve_loaded_image(
    metadata: Mapping[str, object],
    *,
    run: Callable[..., Awaitable[tuple[int, str, str]]] | None = None,
) -> str:
    """The local image ID of the image a Buildx ``--load`` build just produced.

    Docker's classic image store names an image by its config digest and the containerd
    store (the default on new Docker installs) by its manifest digest. Buildx reports
    both, so whichever the daemon resolves is the image.
    """
    run = run or _run
    candidates = [
        digest
        for digest in (
            metadata.get("containerimage.config.digest"),
            metadata.get("containerimage.digest"),
        )
        if isinstance(digest, str) and digest.startswith("sha256:")
    ]
    if not candidates:
        raise ProviderStartError("Buildx metadata has no immutable image digest")
    for digest in candidates:
        code, image_id, stderr = await run(
            "docker", "image", "inspect", "--format", "{{.Id}}", digest, timeout=30
        )
        image_id = image_id.strip()
        if code == 0 and image_id.startswith("sha256:"):
            return image_id
    raise ProviderStartError(
        f"Docker did not load the built Task image: {stderr.strip() or image_id}"
    )


async def resolve_registry_reference(reference: str) -> str:
    """Pin a registry tag using manifest metadata, without pulling image layers.

    Buildx uses the Host's Docker registry credentials and needs no Docker daemon.
    Keep raw CLI diagnostics out of errors: credential helpers may print secrets.
    """
    try:
        code, output, _ = await _run(
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--format",
            "{{.Manifest.Digest}}",
            reference,
            timeout=60,
        )
    except OSError:
        raise ProviderStartError(
            "Resolving an OCI tag requires Docker CLI with Buildx on the ALE Host; "
            "install them or supply a digest-pinned image reference"
        ) from None
    if code != 0:
        raise ProviderStartError(
            "Could not resolve OCI image metadata; check Docker Buildx availability, "
            "the image reference and Host registry access (docker login)"
        )
    digest = output.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ProviderStartError("Registry metadata did not supply a valid SHA-256 image digest")
    # A port belongs to the registry, while the optional tag follows the last slash.
    registry, _, name = reference.rpartition("/")
    repository = name.partition("@")[0].partition(":")[0]
    return f"{registry}/{repository}@{digest}"


async def resolve_container_image(image: ImageRef) -> PreparedTaskImage:
    if image.kind is not ImageKind.CONTAINER:
        raise ProviderStartError("container image resolution requires kind=container")
    return await resolve_container_reference(image.reference)


async def resolve_container_reference(reference: str) -> PreparedTaskImage:
    """Acquire an authored container ref and record its immutable runnable identity."""
    code, _, stderr = await _run("docker", "pull", reference)
    if code != 0:
        raise ProviderStartError(f"could not pull {reference}: {stderr.strip()}")
    code, output, stderr = await _run(
        "docker", "image", "inspect", "--format", "{{json .RepoDigests}}|{{.Id}}", reference
    )
    if code != 0:
        raise ProviderStartError(f"could not inspect {reference}: {stderr.strip()}")
    raw_digests, separator, image_id = output.strip().rpartition("|")
    try:
        resolved = _select_repo_digest(reference, json.loads(raw_digests))
    except (json.JSONDecodeError, TypeError):
        resolved = ""
    source_identity = resolved.partition("@")[2]
    if (
        not separator
        or not source_identity.startswith("sha256:")
        or not image_id.startswith("sha256:")
    ):
        raise ProviderStartError(f"docker returned no immutable identity for {reference}")
    return PreparedTaskImage(
        kind=ImageKind.CONTAINER,
        source="external-ref",
        input_identity=source_identity,
        runtime_ref=resolved,
        prepared_identity=image_id,
        resolved_reference=resolved,
    )


async def resolve_prepared_container_image(image: PreparedTaskImage) -> ResolvedImage:
    if image.kind is not ImageKind.CONTAINER:
        raise ProviderStartError("DockerProvider requires a prepared container image")
    code, output, stderr = await _run(
        "docker", "image", "inspect", "--format", "{{.Id}}", image.runtime_ref
    )
    observed = output.strip()
    if code != 0:
        raise ProviderStartError(
            f"prepared Task image {image.runtime_ref} is unavailable: {stderr.strip()}"
        )
    if observed != image.prepared_identity:
        raise ProviderStartError(
            f"prepared Task image changed: expected {image.prepared_identity}, observed {observed}"
        )
    return ResolvedImage(
        kind=ImageKind.CONTAINER,
        prepared_identity=image.prepared_identity,
        observed_identity=observed,
        observed_ref=image.runtime_ref,
    )


def resolve_local_vm_fixture(image: ImageRef, path: Path) -> PreparedTaskImage:
    """Create a test-only prepared identity without reading the full fixture disk."""
    if image.kind is not ImageKind.VM:
        raise ProviderStartError("VM fixture resolution requires kind=vm")
    resolved = path.resolve()
    stat = resolved.stat()
    identity = content_hash(
        {"fixture": str(resolved), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    )
    reference = f"local://{resolved}"
    return PreparedTaskImage(
        kind=ImageKind.VM,
        source="external-ref",
        input_identity=identity,
        runtime_ref=str(resolved),
        prepared_identity=identity,
        resolved_reference=reference,
    )


async def resolve_vm_image(
    image: ImageRef,
    *,
    cache_dir: Path | None = None,
) -> PreparedTaskImage:
    """Resolve a standalone local qcow2 or acquire a disk published with ``publish_vm_image``."""
    if image.kind is not ImageKind.VM:
        raise ProviderStartError("VM image resolution requires kind=vm")
    declared = image.reference
    if Path(declared).is_absolute():
        disk = Path(declared).resolve()
        await _check_standalone_qcow2(disk, "local VM disk")
        identity = await asyncio.to_thread(_local_disk_identity, disk)
        return PreparedTaskImage(
            kind=ImageKind.VM,
            source="external-ref",
            input_identity=identity,
            runtime_ref=str(disk),
            prepared_identity=identity,
            resolved_reference=disk.as_uri(),
        )

    reference = Reference.parse(declared)
    root = cache_dir or cache_root() / "vm-images"
    if reference.digest is not None:
        # A cache entry is named by the manifest digest, so a pinned disk needs no registry.
        disk = root / reference.digest.removeprefix("sha256:") / "disk.qcow2"
        if await _valid_qcow2(disk):
            return _registry_disk(reference, reference.digest, disk)

    async with Registry(reference, connections=TRANSFERS) as registry:
        manifest = await _read_manifest(registry, reference)
        entry = root / manifest.digest.removeprefix("sha256:")
        disk = entry / "disk.qcow2"
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / f"{entry.name}.lock"
        lock_path.touch(exist_ok=True)
        with lock_path.open("r+") as lock:
            await _acquire_lock(lock)
            try:
                if not await _valid_qcow2(disk):
                    await remove_disk(disk)
                    await _download(registry, manifest, disk)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    return _registry_disk(reference, manifest.digest, disk)


async def pull_vm_image(reference: str, destination: Path) -> str:
    """Download a published disk straight to ``destination``; return its pinned reference.

    A pull that stops leaves ``<destination>.partial``, which the next pull resumes.
    """
    source = Reference.parse(reference)
    async with Registry(source, connections=TRANSFERS) as registry:
        manifest = await _read_manifest(registry, source)
        await _download(registry, manifest, destination.absolute())
    return source.pinned(manifest.digest)


async def publish_vm_image(disk: Path, reference: str) -> str:
    """Publish a standalone qcow2 as a chunked VM disk under a new tag.

    The disk is cut into ``CHUNK_SIZE`` pieces, each compressed alone and uploaded as one
    layer, several at once. Chunks the repository already has are not sent again, so a
    publish that stopped can simply be repeated; a tag that exists is never replaced.
    """
    target = Reference.parse(reference)
    if target.tag is None or target.digest is not None:
        raise ProviderStartError(f"publish a VM disk under a tag, without a digest: {reference}")
    source = disk.resolve()
    await _check_standalone_qcow2(source, "VM disk")
    fd = os.open(source, os.O_RDONLY)
    try:
        before = os.fstat(fd)
        async with Registry(target, push=True, connections=_UPLOADS) as registry:
            if await registry.has_manifest(target.tag):
                raise ProviderStartError(
                    f"{reference} is already published; a published version never changes"
                )
            layers = await _push_chunks(registry, fd, before.st_size, source.parent)
            config = json.dumps({}).encode()
            await registry.push_blob(_EMPTY_CONFIG["digest"], len(config), lambda: config)
            manifest = json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": MANIFEST,
                    "artifactType": VM_DISK,
                    "config": _EMPTY_CONFIG,
                    "layers": layers,
                    "annotations": {
                        _DISK_SIZE: str(before.st_size),
                        _DISK_CHUNK_SIZE: str(CHUNK_SIZE),
                    },
                },
                indent=2,
            ).encode()
            digest = f"sha256:{hashlib.sha256(manifest).hexdigest()}"
            if not _same_file(before, os.stat(source)):
                raise ProviderStartError(f"VM disk changed while publishing it: {source}")
            stored = await registry.put_manifest(target.tag, manifest, MANIFEST)
    except OSError as error:
        raise ProviderStartError(f"could not publish {source}: {error}") from error
    finally:
        os.close(fd)
    if stored and stored != digest:
        raise ProviderStartError(f"{reference} stored manifest {stored}, expected {digest}")
    return f"{reference}@{digest}"


def _registry_disk(reference: Reference, digest: str, disk: Path) -> PreparedTaskImage:
    return PreparedTaskImage(
        kind=ImageKind.VM,
        source="external-ref",
        input_identity=digest,
        runtime_ref=str(disk.resolve()),
        prepared_identity=digest,
        resolved_reference=reference.pinned(digest),
    )


def _local_disk_identity(disk: Path) -> str:
    before = disk.stat()
    with disk.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if not _same_file(before, disk.stat()):
        raise ProviderStartError(f"VM disk {disk} changed while it was hashed")
    return f"sha256:{digest}"


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


async def resolve_prepared_vm_image(image: PreparedTaskImage) -> ResolvedImage:
    """The prepared disk as it is now: a standalone qcow2, or a chain ALE layered whose
    layers still add up to the identity it was prepared under."""
    if image.kind is not ImageKind.VM:
        raise ProviderStartError("QemuProvider requires a prepared VM image")
    disk = Path(image.runtime_ref)
    if not await _valid_qcow2(disk):
        raise ProviderStartError(f"prepared VM disk is missing or invalid: {disk}")
    if await layer_parent(disk) is not None:
        observed = await layer_identity(disk)
        if observed != image.prepared_identity:
            raise ProviderStartError(
                f"prepared VM disk {disk} changed: expected {image.prepared_identity}, "
                f"observed {observed}"
            )
    return ResolvedImage(
        kind=ImageKind.VM,
        prepared_identity=image.prepared_identity,
        observed_identity=image.prepared_identity,
        observed_ref=str(disk.resolve()),
    )


def chain_identity(parent: str, digest: str) -> str:
    """A layer's identity: its parent's and its own file's SHA-256, as an OCI ChainID."""
    return "sha256:" + hashlib.sha256(f"{parent} {digest}".encode()).hexdigest()


async def record_layer(disk: Path, parent: str) -> str:
    """Record ``disk`` as a layer on the one whose identity is ``parent``; its identity."""
    digest = await asyncio.to_thread(_local_disk_identity, disk)
    identity = chain_identity(parent, digest)
    _write_record(disk, {"parent": parent, "digest": digest, "identity": identity})
    return identity


async def layer_identity(disk: Path, *, on: tuple[Path, str] | None = None) -> str:
    """The identity of a disk ALE layered, once every layer of it is shown to sit on the
    parent its record names. Hashing is done when a layer is made, not here.

    ``on`` names a disk below it and that disk's identity, where the check stops: a
    snapshot sits on whatever disk its Task resolved to, which need not be ALE's.
    """
    chain = await _chain(disk)
    identity: str | None = None
    if on is not None:
        below, identity = on
        found = [layer.resolve() for layer in chain]
        if below.resolve() not in found:
            raise ProviderStartError(f"{disk} is not built on {below}")
        chain = chain[: found.index(below.resolve())]
    for layer in reversed(chain):
        record = read_layer_record(layer)
        if record is None:
            raise ProviderStartError(f"{layer} is not a VM disk layer ALE made")
        if record.get("parent") != identity:
            raise ProviderStartError(
                f"{layer} was made on {record.get('parent')}, but sits on {identity}"
            )
        expected = (
            record.get("identity")
            if identity is None
            else chain_identity(identity, str(record.get("digest")))
        )
        if not isinstance(expected, str) or record.get("identity") != expected:
            raise ProviderStartError(f"{layer} has an inconsistent layer record")
        identity = expected
    assert identity is not None
    return identity


async def layer_parent(disk: Path) -> Path | None:
    """The disk ``disk`` backs onto, as a Host path, or None for a standalone disk."""
    code, output, stderr = await _run("qemu-img", "info", "-U", "--output=json", str(disk))
    if code != 0:
        raise ProviderStartError(f"could not inspect VM disk {disk}: {stderr.strip()}")
    parent = json.loads(output).get("full-backing-filename")
    return Path(parent) if parent else None


async def set_parent(disk: Path, parent: Path) -> None:
    """Point ``disk`` at ``parent`` by a path relative to itself, without touching its data,
    so the pair survives a move of the directory that holds both."""
    relative = os.path.relpath(parent.resolve(), disk.resolve().parent)
    code, _, stderr = await _run(
        "qemu-img", "rebase", "-u", "-F", "qcow2", "-b", relative, str(disk), timeout=60
    )
    if code != 0:
        raise ProviderStartError(f"could not point {disk} at {parent}: {stderr.strip()}")


async def chain_mounts(disk: Path, *, writable: bool = False) -> list[str]:
    """``docker run`` arguments that show a container ``disk`` and every layer below it at
    their Host paths, where the chain's relative names resolve as they do here. Only
    ``disk`` itself may be writable."""
    mounts: list[str] = []
    for index, layer in enumerate(await _chain(disk)):
        path = layer.resolve()
        mode = "" if writable and index == 0 else ",readonly"
        mounts += ["--mount", f"type=bind,src={path},dst={path}{mode}"]
    return mounts


async def base_layer(image: PreparedTaskImage) -> Path:
    """The disk of ``image`` as the base of a chain: an uncompressed copy, made once per
    Host and recorded under the image's own identity.

    ALE makes no layer on the disk itself, which may be compressed or not ALE's to keep.
    The copy lives in ``vm-builds`` beside the layers made on it.
    """
    root = cache_root() / "vm-builds"
    root.mkdir(parents=True, exist_ok=True)
    key = content_hash({"base": image.prepared_identity})
    disk = root / f"{key.removeprefix('sha256:')}.qcow2"
    with disk.with_suffix(".lock").open("a") as lock:
        await _acquire_lock(lock)
        try:
            if await finished_layer(disk) == image.prepared_identity:
                return disk
            await remove_disk(disk)
            partial = disk.with_suffix(".partial")
            try:
                code, _, stderr = await _run(
                    "qemu-img", "convert", "-O", "qcow2", image.runtime_ref, str(partial),
                    timeout=1800,
                )  # fmt: skip
                if code != 0:
                    raise ProviderStartError(f"could not copy the VM disk: {stderr.strip()}")
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
            os.replace(partial, disk)
            _write_record(disk, {"parent": None, "identity": image.prepared_identity})
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return disk


async def finished_layer(disk: Path) -> str | None:
    """The identity of a layer ALE finished making at ``disk``, or None when there is none."""
    if not _record_path(disk).is_file() or not await _valid_qcow2(disk):
        return None
    try:
        return await layer_identity(disk)
    except ProviderStartError:
        return None


async def remove_disk(disk: Path) -> None:
    """Delete a cached disk ALE is to make again, and its layer record, unless another
    disk in the ALE cache backs onto it.

    A layer holds only what differs from the exact parent it was written on, so a new
    parent at the same path would change what every disk above it reads. The record goes
    with the disk, so a disk made at this path later is never taken for the old one.
    """
    if disk.exists():
        target = disk.resolve()
        for child in await asyncio.to_thread(lambda: sorted(cache_root().rglob("*.qcow2"))):
            if child.resolve() != target and _backing_of(child) == target:
                raise ProviderStartError(
                    f"the cached VM disk {disk} must be made again, but {child} is built on "
                    "it; remove that first (a snapshot with `ale sandbox destroy`)"
                )
    _record_path(disk).unlink(missing_ok=True)
    disk.unlink(missing_ok=True)


def _backing_of(disk: Path) -> Path | None:
    """The disk a qcow2 names as its backing file, read from its header."""
    try:
        with disk.open("rb") as handle:
            header = handle.read(20)
            if len(header) < 20 or header[:4] != b"QFI\xfb":
                return None
            offset, size = int.from_bytes(header[8:16], "big"), int.from_bytes(header[16:20], "big")
            if not offset:
                return None
            handle.seek(offset)
            name = handle.read(size).decode("utf-8", "replace")
    except OSError:
        return None
    return (disk.parent / name).resolve()


async def _chain(disk: Path) -> list[Path]:
    """``disk`` and every disk below it, top first, by the paths qemu opens them at."""
    code, output, stderr = await _run(
        "qemu-img", "info", "-U", "--backing-chain", "--output=json", str(disk)
    )
    if code != 0:
        raise ProviderStartError(f"could not open the VM disk chain of {disk}: {stderr.strip()}")
    return [Path(entry["filename"]) for entry in json.loads(output)]


def _record_path(disk: Path) -> Path:
    return disk.with_suffix(".json")


def read_layer_record(disk: Path) -> dict[str, object] | None:
    """The record ALE keeps beside a layer it made, or None."""
    try:
        record = json.loads(_record_path(disk).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _write_record(disk: Path, record: dict[str, object]) -> None:
    target = _record_path(disk)
    partial = target.with_suffix(".json.partial")
    partial.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, target)


async def _valid_qcow2(path: Path) -> bool:
    if not path.is_file():
        return False
    code, _, _ = await _run("qemu-img", "check", "-q", str(path), timeout=300)
    return code == 0


async def _check_standalone_qcow2(disk: Path, label: str) -> None:
    """A qcow2 whose identity covers the whole disk: no backing or external data file."""
    if not await _valid_qcow2(disk):
        raise ProviderStartError(f"{label} is missing or invalid: {disk}")
    code, output, stderr = await _run("qemu-img", "info", "--output=json", str(disk))
    if code != 0:
        raise ProviderStartError(f"could not inspect {label} {disk}: {stderr.strip()}")
    try:
        info = json.loads(output)
        external_data = info.get("format-specific", {}).get("data", {}).get("data-file")
        standalone = (
            info.get("format") == "qcow2" and not info.get("backing-filename") and not external_data
        )
    except (ValueError, AttributeError) as exc:
        raise ProviderStartError(f"invalid {label} metadata: {disk}") from exc
    if not standalone:
        raise ProviderStartError(
            f"{label} must be a standalone qcow2 without backing or external data files: {disk}"
        )


# --- chunked VM disks ---


@dataclass(frozen=True)
class _Chunk:
    offset: int
    length: int
    #: The compressed layer, as the registry stores it.
    blob: str
    blob_size: int
    #: SHA-256 of the chunk's own bytes, which identify it whoever serves or compresses it.
    digest: str


@dataclass(frozen=True)
class _DiskManifest:
    digest: str
    size: int
    chunks: tuple[_Chunk, ...]


async def _read_manifest(registry: Registry, reference: Reference) -> _DiskManifest:
    data, media_type = await registry.manifest(reference.digest or reference.tag or "latest")
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    if reference.digest is not None and digest != reference.digest:
        raise ProviderStartError(f"{reference} answered with a different manifest ({digest})")
    try:
        manifest = json.loads(data)
    except ValueError:
        manifest = None
    kind = manifest.get("artifactType") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or manifest.get("mediaType") != MANIFEST or kind != VM_DISK:
        shown = (kind if isinstance(kind, str) else media_type)[:100] or "unknown type"
        raise ProviderStartError(
            f"{reference} is not an ALE VM disk ({shown}); "
            "publish its qcow2 with `ale vm-image push`"
        )
    try:
        annotations = manifest["annotations"]
        size = int(annotations[_DISK_SIZE])
        chunk_size = int(annotations[_DISK_CHUNK_SIZE])
        layers = manifest["layers"]
        if size <= 0 or chunk_size <= 0 or len(layers) != -(-size // chunk_size):
            raise ValueError("the chunks do not cover the disk")
        chunks = []
        for index, layer in enumerate(layers):
            if layer["mediaType"] != VM_DISK_CHUNK or int(layer["size"]) < 0:
                raise ValueError("not a disk chunk")
            offset = index * chunk_size
            chunks.append(
                _Chunk(
                    offset=offset,
                    length=min(chunk_size, size - offset),
                    blob=_sha256(layer["digest"]),
                    blob_size=int(layer["size"]),
                    digest=_sha256(layer["annotations"][_CHUNK_DIGEST]),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderStartError(
            f"{reference} has a malformed VM disk manifest: {str(exc)[:200]}"
        ) from None
    return _DiskManifest(digest=digest, size=size, chunks=tuple(chunks))


def _sha256(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError(f"not a SHA-256 digest: {value!r}")
    return value


async def _push_chunks(
    registry: Registry, fd: int, size: int, scratch: Path
) -> list[dict[str, object]]:
    loop = asyncio.get_running_loop()
    slots = asyncio.Semaphore(_UPLOADS)
    executor = ThreadPoolExecutor(_UPLOADS, thread_name_prefix="ale-vm-push")

    async def push(offset: int) -> dict[str, object]:
        async with slots:
            # A chunk is compressed in full before its upload, which must name its digest.
            with tempfile.TemporaryFile(dir=scratch, prefix=".ale-vm-chunk-") as spool:
                length = min(CHUNK_SIZE, size - offset)
                blob, blob_size, digest = await loop.run_in_executor(
                    executor, _compress_chunk, fd, offset, length, spool
                )
                spooled = spool.fileno()
                await registry.push_blob(blob, blob_size, lambda: _read_file(spooled, blob_size))
        return {
            "mediaType": VM_DISK_CHUNK,
            "digest": blob,
            "size": blob_size,
            "annotations": {_CHUNK_DIGEST: digest},
        }

    try:
        return await _all([push(offset) for offset in range(0, size, CHUNK_SIZE)])
    finally:
        executor.shutdown(wait=True)


def _compress_chunk(fd: int, offset: int, length: int, spool: BinaryIO) -> tuple[str, int, str]:
    content = hashlib.sha256()
    output = _HashingWriter(spool)
    stream = zstandard.ZstdCompressor(level=_ZSTD_LEVEL).stream_writer(
        output, size=length, closefd=False
    )
    position = 0
    while position < length:
        data = os.pread(fd, min(_BLOCK, length - position), offset + position)
        if not data:
            raise ProviderStartError("VM disk shrank while publishing it")
        content.update(data)
        stream.write(data)
        position += len(data)
    stream.close()
    spool.flush()
    return output.digest(), output.size, f"sha256:{content.hexdigest()}"


class _HashingWriter:
    def __init__(self, target: BinaryIO) -> None:
        self.size = 0
        self._target = target
        self._digest = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self._target.write(data)
        self._digest.update(data)
        self.size += len(data)
        return len(data)

    def digest(self) -> str:
        return f"sha256:{self._digest.hexdigest()}"


async def _read_file(fd: int, size: int) -> AsyncIterator[bytes]:
    position = 0
    while position < size:
        data = await asyncio.to_thread(os.pread, fd, min(_BLOCK, size - position), position)
        if not data:
            raise ProviderStartError("a compressed chunk was truncated before its upload")
        position += len(data)
        yield data


async def _download(registry: Registry, manifest: _DiskManifest, disk: Path) -> None:
    """Assemble ``disk``, keeping the chunks an earlier attempt already wrote."""
    partial = disk.with_name(f"{disk.name}.partial")
    try:
        disk.parent.mkdir(parents=True, exist_ok=True)
        await _write_disk(registry, manifest, partial)
        await _check_standalone_qcow2(partial, "pulled VM disk")
        os.replace(partial, disk)
    except OSError as error:
        raise ProviderStartError(f"could not write the VM disk {disk}: {error}") from error


async def _write_disk(registry: Registry, manifest: _DiskManifest, partial: Path) -> None:
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(TRANSFERS, thread_name_prefix="ale-vm-pull")
    fd = os.open(partial, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.fstat(fd).st_size == manifest.size:
            held = await asyncio.gather(
                *(loop.run_in_executor(executor, _holds, fd, chunk) for chunk in manifest.chunks)
            )
        else:
            os.ftruncate(fd, 0)
            os.ftruncate(fd, manifest.size)
            held = [False] * len(manifest.chunks)
        slots = asyncio.Semaphore(TRANSFERS)

        async def fetch(chunk: _Chunk) -> None:
            async with slots:
                await _fetch_chunk(registry, chunk, fd, executor)

        missing = [chunk for chunk, done in zip(manifest.chunks, held, strict=True) if not done]
        await _all([fetch(chunk) for chunk in missing])
        await loop.run_in_executor(executor, os.fsync, fd)
    finally:
        # No worker may still write once the descriptor number can be reused.
        executor.shutdown(wait=True)
        os.close(fd)


async def _fetch_chunk(
    registry: Registry, chunk: _Chunk, fd: int, executor: ThreadPoolExecutor
) -> None:
    """Stream one chunk into place, continuing an interrupted read where it stopped."""
    loop = asyncio.get_running_loop()
    # Asking a filesystem where data is can wait for it to sync, so not on the event loop.
    writer = await loop.run_in_executor(executor, _ChunkWriter, fd, chunk)

    async def attempt() -> None:
        nonlocal writer
        if writer.received == chunk.blob_size:
            return  # the connection broke after the last byte
        async with registry.blob(chunk.blob, writer.received) as response:
            if writer.received and response.status != 206:
                # The range was ignored, so start over.
                writer = await loop.run_in_executor(executor, _ChunkWriter, fd, chunk)
            async for batch in _batches(response.content):
                await loop.run_in_executor(executor, writer.feed, batch)

    await retrying(attempt, f"pulling the chunk at {chunk.offset} of {registry.reference}")
    await loop.run_in_executor(executor, writer.finish)


async def _batches(content: aiohttp.StreamReader) -> AsyncIterator[bytes]:
    pending = bytearray()
    async for data in content.iter_chunked(1 << 20):
        pending += data
        if len(pending) >= _BLOCK:
            yield bytes(pending)
            pending.clear()
    if pending:
        yield bytes(pending)


class _ChunkWriter:
    """Decompress one chunk into its place in the disk, checking both of its digests."""

    def __init__(self, fd: int, chunk: _Chunk) -> None:
        self.received = 0
        self._fd = fd
        self._chunk = chunk
        # Zero blocks may stay holes only where nothing was written before.
        self._sparse = not _has_data(fd, chunk.offset, chunk.length)
        self._written = 0
        self._blob = hashlib.sha256()
        self._content = hashlib.sha256()
        self._stream = zstandard.ZstdDecompressor().stream_writer(
            self, write_size=1 << 20, closefd=False
        )

    def feed(self, data: bytes) -> None:
        if self.received + len(data) > self._chunk.blob_size:
            raise ProviderStartError(f"the chunk at {self._chunk.offset} is larger than declared")
        self.received += len(data)
        self._blob.update(data)
        try:
            self._stream.write(data)
        except zstandard.ZstdError as error:
            raise ProviderStartError(
                f"the chunk at {self._chunk.offset} is not valid zstd: {error}"
            ) from None

    def write(self, data: bytes) -> int:
        """Called by the decompressor with each piece of output."""
        end = self._written + len(data)
        if end > self._chunk.length:
            raise ProviderStartError(f"the chunk at {self._chunk.offset} decompresses too long")
        self._content.update(data)
        offset = self._chunk.offset + self._written
        if self._sparse:
            _write_sparse(self._fd, data, offset)
        else:
            _write_all(self._fd, memoryview(data), offset)
        self._written = end
        return len(data)

    def finish(self) -> None:
        self._stream.flush()
        if (
            self.received != self._chunk.blob_size
            or f"sha256:{self._blob.hexdigest()}" != self._chunk.blob
            or self._written != self._chunk.length
            or f"sha256:{self._content.hexdigest()}" != self._chunk.digest
        ):
            raise ProviderStartError(f"the chunk at {self._chunk.offset} failed verification")


def _holds(fd: int, chunk: _Chunk) -> bool:
    """Whether the disk already holds this chunk, as a pull that stopped may have left it."""
    if not _has_data(fd, chunk.offset, chunk.length):
        return False
    digest = hashlib.sha256()
    for position in range(chunk.offset, chunk.offset + chunk.length, _BLOCK):
        digest.update(os.pread(fd, min(_BLOCK, chunk.offset + chunk.length - position), position))
    return f"sha256:{digest.hexdigest()}" == chunk.digest


def _has_data(fd: int, offset: int, length: int) -> bool:
    """Whether anything was written in the range; true where the filesystem cannot tell."""
    try:
        return os.lseek(fd, offset, os.SEEK_DATA) < offset + length
    except OSError as error:
        return error.errno != errno.ENXIO


def _write_sparse(fd: int, data: bytes, offset: int) -> None:
    """Write ``data`` at ``offset``, skipping its zero parts of each aligned 64 KiB block."""
    view = memoryview(data)
    start: int | None = None
    index = 0
    while index < len(data):
        end = min(len(data), index + _SPARSE - (offset + index) % _SPARSE)
        if data[index:end] == _ZEROS[: end - index]:
            if start is not None:
                _write_all(fd, view[start:index], offset + start)
                start = None
        elif start is None:
            start = index
        index = end
    if start is not None:
        _write_all(fd, view[start:], offset + start)


def _write_all(fd: int, data: memoryview, offset: int) -> None:
    while data:
        written = os.pwrite(fd, data, offset)
        data = data[written:]
        offset += written


async def _all[T](operations: list[Coroutine[object, object, T]]) -> list[T]:
    """Run operations concurrently; if one fails, cancel the rest before raising."""
    tasks = [asyncio.ensure_future(operation) for operation in operations]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
