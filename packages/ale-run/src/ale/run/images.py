"""Provider-owned immutable image acquisition and offline validation."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import TextIO

from ale.core.errors import ProviderStartError
from ale.core.ids import content_hash
from ale.core.sandbox import ImageRef, PreparedTaskImage, ResolvedImage
from ale.core.taskspec import ImageKind
from ale.run.sources import cache_root

__all__ = [
    "publish_vm_image",
    "resolve_container_image",
    "resolve_container_reference",
    "resolve_local_vm_fixture",
    "resolve_prepared_container_image",
    "resolve_prepared_vm_image",
    "resolve_vm_image",
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
    """Acquire an OCI-carried qcow2 into a digest-keyed local cache."""
    if image.kind is not ImageKind.VM:
        raise ProviderStartError("VM image resolution requires kind=vm")
    declared = image.reference
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
                disk.unlink(missing_ok=True)
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


async def publish_vm_image(disk: Path, reference: str) -> str:
    """Publish a qcow2 as the disk layer consumed by ``resolve_vm_image``."""
    source = disk.resolve()
    if not await _valid_qcow2(source):
        raise ProviderStartError(f"VM disk is missing or invalid: {source}")
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
    if image.kind is not ImageKind.VM:
        raise ProviderStartError("QemuProvider requires a prepared VM image")
    disk = Path(image.runtime_ref)
    if not await _valid_qcow2(disk):
        raise ProviderStartError(f"prepared VM disk is missing or invalid: {disk}")
    return ResolvedImage(
        kind=ImageKind.VM,
        prepared_identity=image.prepared_identity,
        observed_identity=image.prepared_identity,
        observed_ref=str(disk.resolve()),
    )


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
