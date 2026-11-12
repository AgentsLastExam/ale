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
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TextIO

from ale.core.errors import ProviderStartError
from ale.core.ids import content_hash
from ale.core.sandbox import ImageRef, PreparedTaskImage, ResolvedImage
from ale.core.taskspec import ImageKind
from ale.run.sources import cache_root

__all__ = [
    "base_layer",
    "chain_identity",
    "chain_mounts",
    "finished_layer",
    "layer_identity",
    "layer_parent",
    "publish_vm_image",
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
    """Resolve a standalone local qcow2 or acquire a registry-carried disk."""
    if image.kind is not ImageKind.VM:
        raise ProviderStartError("VM image resolution requires kind=vm")
    declared = image.reference
    if Path(declared).is_absolute():
        disk = Path(declared).resolve()
        if not await _valid_qcow2(disk):
            raise ProviderStartError(f"local VM disk is missing or invalid: {disk}")
        code, output, stderr = await _run("qemu-img", "info", "--output=json", str(disk))
        if code != 0:
            raise ProviderStartError(f"could not inspect local VM disk: {stderr.strip()}")
        try:
            info = json.loads(output)
            external_data = info.get("format-specific", {}).get("data", {}).get("data-file")
            if info.get("format") != "qcow2" or info.get("backing-filename") or external_data:
                raise ProviderStartError(
                    "local VM image must be a standalone qcow2 "
                    "without backing or external data files"
                )
        except (ValueError, AttributeError) as exc:
            raise ProviderStartError(f"invalid local VM disk metadata: {disk}") from exc
        identity = await asyncio.to_thread(_local_disk_identity, disk)
        return PreparedTaskImage(
            kind=ImageKind.VM,
            source="external-ref",
            input_identity=identity,
            runtime_ref=str(disk),
            prepared_identity=identity,
            resolved_reference=disk.as_uri(),
        )
    code, _, stderr = await _run("docker", "pull", declared, timeout=900)
    if code != 0:
        raise ProviderStartError(f"could not pull VM image {declared}: {stderr.strip()}")
    code, output, stderr = await _run(
        "docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", declared
    )
    if code != 0:
        raise ProviderStartError(f"could not inspect VM image {declared}: {stderr.strip()}")
    resolved_reference = output.strip()
    identity = resolved_reference.partition("@")[2]
    if not identity.startswith("sha256:"):
        raise ProviderStartError(f"registry returned no immutable digest for {declared}")

    root = cache_dir or cache_root() / "vm-images"
    root.mkdir(parents=True, exist_ok=True)
    entry = root / identity.removeprefix("sha256:")
    disk = entry / "disk.qcow2"
    lock_path = root / f"{entry.name}.lock"
    lock_path.touch(exist_ok=True)

    with lock_path.open("r+") as lock:
        await _acquire_lock(lock)
        try:
            if not await _valid_qcow2(disk):
                await remove_disk(disk)
                await _extract_vm_disk(resolved_reference, entry, disk)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

    return PreparedTaskImage(
        kind=ImageKind.VM,
        source="external-ref",
        input_identity=identity,
        runtime_ref=str(disk.resolve()),
        prepared_identity=identity,
        resolved_reference=resolved_reference,
    )


def _local_disk_identity(disk: Path) -> str:
    before = disk.stat()
    with disk.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = disk.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ProviderStartError(f"VM disk {disk} changed while it was hashed")
    return f"sha256:{digest}"


async def publish_vm_image(disk: Path, reference: str) -> str:
    """Publish a standalone qcow2 as the single layer of the OCI image ``resolve_vm_image``
    consumes; a disk with a backing file would arrive without it."""
    source = disk.resolve()
    if not await _valid_qcow2(source):
        raise ProviderStartError(f"VM disk is missing or invalid: {source}")
    if await layer_parent(source) is not None:
        raise ProviderStartError(
            f"{source} has a backing file; flatten it with `qemu-img convert` to publish it"
        )
    if not reference.strip():
        raise ProviderStartError("VM image reference must not be blank")

    with tempfile.TemporaryDirectory(prefix=".ale-vm-publish-", dir=source.parent) as temporary:
        context = Path(temporary)
        os.link(source, context / "disk.qcow2")
        (context / "Dockerfile").write_text(
            "FROM scratch\nCOPY disk.qcow2 /disk.qcow2\n",
            encoding="utf-8",
        )
        metadata_file = context / "metadata.json"
        code, _, stderr = await _run(
            "docker",
            "buildx",
            "build",
            "--push",
            "--provenance=false",
            "--sbom=false",
            "--tag",
            reference,
            "--metadata-file",
            str(metadata_file),
            str(context),
            timeout=7200,
        )
        if code != 0:
            raise ProviderStartError(f"could not publish VM image {reference}: {stderr.strip()}")
        try:
            digest = json.loads(metadata_file.read_text(encoding="utf-8"))["containerimage.digest"]
        except (KeyError, OSError, json.JSONDecodeError) as exc:
            raise ProviderStartError(f"Docker returned no digest for {reference}: {exc}") from exc
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ProviderStartError(f"Docker returned an invalid digest for {reference}: {digest}")
    return f"{reference}@{digest}"


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


async def _extract_vm_disk(resolved_reference: str, entry: Path, disk: Path) -> None:
    staging = Path(tempfile.mkdtemp(prefix=f".{entry.name}-", dir=entry.parent))
    container = ""
    try:
        code, output, stderr = await _run(
            "docker", "create", "--entrypoint", "/disk.qcow2", resolved_reference
        )
        if code != 0:
            raise ProviderStartError(
                f"could not stage VM image {resolved_reference}: {stderr.strip()}"
            )
        container = output.strip()
        candidate = staging / "disk.qcow2"
        code, _, stderr = await _run(
            "docker", "cp", f"{container}:/disk.qcow2", str(candidate), timeout=900
        )
        if code != 0:
            raise ProviderStartError(f"could not extract /disk.qcow2: {stderr.strip()}")
        if not await _valid_qcow2(candidate):
            raise ProviderStartError(f"invalid qcow2 in {resolved_reference}")
        entry.mkdir(parents=True, exist_ok=True)
        os.replace(candidate, disk)
    finally:
        if container:
            await _run("docker", "rm", "-f", container, timeout=60)
        shutil.rmtree(staging, ignore_errors=True)
