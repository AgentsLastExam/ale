"""Prepare solver and verifier images before an episode starts."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import tarfile
import tempfile
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from ale.core.errors import AleError, ProviderStartError, TaskDefinitionError
from ale.core.ids import content_hash
from ale.core.sandbox import ImageRef, PreparedTaskImage
from ale.core.taskspec import ImageKind, VerificationMode
from ale.run.providers import ProviderRegistry
from ale.run.sources import cache_root

__all__ = [
    "ImagePreparationResult",
    "ImagePreparationStep",
    "prepare_task_image",
    "prepare_task_image_result",
    "prepare_verifier_image",
    "prepare_verifier_image_result",
    "vm_materialization_identity",
]

MATERIALIZER_IMAGE = os.environ.get(
    "ALE_VM_MATERIALIZER",
    "ghcr.io/agentslastexam/ale-vm-materializer:0.1.0",
)
VM_OUTPUT_CONTRACT = "ale-vm-qcow2/v1"


@dataclass(frozen=True)
class _OciBuild:
    input_identity: str
    image_source_identity: str
    runtime_ref: str
    oci_identity: str
    base_materials: tuple[str, ...]


@dataclass(frozen=True)
class ImagePreparationStep:
    name: str
    outcome: str
    identity: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ImagePreparationResult:
    task: str
    role: str
    image: PreparedTaskImage
    steps: tuple[ImagePreparationStep, ...]


async def _stage[T](name: str, operation: Awaitable[T]) -> T:
    try:
        return await operation
    except AleError as error:
        message = str(error).replace("\n", " ")
        if len(message) > 2000:
            message = f"{message[:1000]} ... {message[-995:]}"
        raise type(error)(f"{name}: {message}") from error


async def _run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        raise ProviderStartError(f"{' '.join(argv[:3])} timed out after {timeout:g}s") from None
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def prepare_task_image(task: object, providers: ProviderRegistry) -> PreparedTaskImage:
    return (await prepare_task_image_result(task, providers)).image


async def prepare_task_image_result(
    task: object, providers: ProviderRegistry
) -> ImagePreparationResult:
    spec = task.spec.image  # type: ignore[attr-defined]
    provider = providers.get(spec.kind)
    dockerfile = task.folder.image_dockerfile  # type: ignore[attr-defined]
    if dockerfile is None:
        if spec.ref is None:
            raise TaskDefinitionError("solver requires image/Dockerfile or image.ref")
        image = await _stage(
            "reference-resolution",
            provider.prepare_image(ImageRef(kind=spec.kind, reference=spec.ref)),
        )
        return _result(
            task,
            "solver",
            image,
            ImagePreparationStep(
                "reference-resolution",
                "executed",
                image.prepared_identity,
                image.resolved_reference,
            ),
            ImagePreparationStep(
                "artifact-check", "executed", image.prepared_identity, image.runtime_ref
            ),
        )

    source_identity = task.image_source_digest  # type: ignore[attr-defined]
    if source_identity is None:
        raise TaskDefinitionError("local solver image has no source identity")
    build = await _stage(
        "oci-build",
        _build_oci(
            context=dockerfile.parent,
            source_digest=source_identity,
            source="solver-local",
        ),
    )
    steps = [ImagePreparationStep("oci-build", "executed", build.oci_identity)]
    if spec.kind is ImageKind.CONTAINER:
        candidate = _local_container(build, source="solver-local")
    else:
        candidate, reused = await _stage(
            "vm-materialization",
            _materialize_vm_with_status(build, source="solver-local"),
        )
        steps.append(
            ImagePreparationStep(
                "vm-materialization",
                "reused" if reused else "executed",
                candidate.prepared_identity,
                f"materializer={candidate.materializer_identity} disk={candidate.runtime_ref}",
            )
        )
    image = await _stage("artifact-check", provider.prepare_image(candidate))
    steps.append(
        ImagePreparationStep(
            "artifact-check", "executed", image.prepared_identity, image.runtime_ref
        )
    )
    return _result(task, "solver", image, *steps)


async def prepare_verifier_image(
    task: object, providers: ProviderRegistry
) -> PreparedTaskImage | None:
    result = await prepare_verifier_image_result(task, providers)
    return result.image if result is not None else None


async def prepare_verifier_image_result(
    task: object, providers: ProviderRegistry
) -> ImagePreparationResult | None:
    verify = task.spec.verify  # type: ignore[attr-defined]
    if verify.environment_mode is VerificationMode.SHARED:
        return None

    dockerfile = task.folder.verifier_dockerfile  # type: ignore[attr-defined]
    if dockerfile is None and verify.image is None:
        image = task.prepared_image  # type: ignore[attr-defined]
        return _result(
            task,
            "verifier",
            image,
            ImagePreparationStep(
                "artifact-check", "reused", image.prepared_identity, image.runtime_ref
            ),
        )
    if verify.image is None:
        raise TaskDefinitionError("verify/Dockerfile requires verify.image.kind")

    provider = providers.get(verify.image.kind)
    if dockerfile is None:
        if verify.image.ref is None:
            raise TaskDefinitionError("external verifier requires verify.image.ref")
        image = await _stage(
            "reference-resolution",
            provider.prepare_image(ImageRef(kind=verify.image.kind, reference=verify.image.ref)),
        )
        return _result(
            task,
            "verifier",
            image,
            ImagePreparationStep(
                "reference-resolution",
                "executed",
                image.prepared_identity,
                image.resolved_reference,
            ),
            ImagePreparationStep(
                "artifact-check", "executed", image.prepared_identity, image.runtime_ref
            ),
        )

    source_identity = task.verifier_image_source_digest  # type: ignore[attr-defined]
    if source_identity is None:
        raise TaskDefinitionError("local verifier image has no source identity")
    build = await _stage(
        "oci-build",
        _build_oci(
            context=dockerfile.parent,
            source_digest=source_identity,
            source="verifier-local",
        ),
    )
    steps = [ImagePreparationStep("oci-build", "executed", build.oci_identity)]
    if verify.image.kind is ImageKind.CONTAINER:
        candidate = _local_container(build, source="verifier-local")
    else:
        candidate, reused = await _stage(
            "vm-materialization",
            _materialize_vm_with_status(build, source="verifier-local"),
        )
        steps.append(
            ImagePreparationStep(
                "vm-materialization",
                "reused" if reused else "executed",
                candidate.prepared_identity,
                f"materializer={candidate.materializer_identity} disk={candidate.runtime_ref}",
            )
        )
    image = await _stage("artifact-check", provider.prepare_image(candidate))
    steps.append(
        ImagePreparationStep(
            "artifact-check", "executed", image.prepared_identity, image.runtime_ref
        )
    )
    return _result(task, "verifier", image, *steps)


def _result(
    task: object,
    role: str,
    image: PreparedTaskImage,
    *steps: ImagePreparationStep,
) -> ImagePreparationResult:
    spec = task.spec  # type: ignore[attr-defined]
    return ImagePreparationResult(
        task=f"{spec.name}@{spec.variant}",
        role=role,
        image=image,
        steps=(ImagePreparationStep("lint", "executed"), *steps),
    )


def _local_container(build: _OciBuild, *, source: str) -> PreparedTaskImage:
    return PreparedTaskImage(
        kind=ImageKind.CONTAINER,
        source=source,
        input_identity=build.input_identity,
        image_source_identity=build.image_source_identity,
        runtime_ref=build.runtime_ref,
        prepared_identity=build.oci_identity,
        base_materials=build.base_materials,
    )


async def _build_oci(*, context: Path, source_digest: str, source: str) -> _OciBuild:
    code, _, stderr = await _run("docker", "buildx", "version", timeout=30)
    if code != 0:
        raise ProviderStartError(f"Docker Buildx is unavailable: {stderr.strip()}")

    input_identity = content_hash(
        {"source": source, "source_identity": source_digest, "build": {"load": True}}
    )
    tag = f"ale-{source}:{input_identity.removeprefix('sha256:')}"
    with tempfile.TemporaryDirectory(prefix="ale-image-") as temporary:
        metadata_file = Path(temporary) / "metadata.json"
        code, _, stderr = await _run(
            "docker",
            "buildx",
            "build",
            "--load",
            "--provenance=false",
            "--sbom=false",
            "--tag",
            tag,
            "--metadata-file",
            str(metadata_file),
            str(context),
        )
        if code != 0:
            raise TaskDefinitionError(f"Task image build failed: {stderr.strip()}")
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderStartError(f"Buildx wrote invalid metadata: {exc}") from exc

    code, image_id, stderr = await _run(
        "docker", "image", "inspect", "--format", "{{.Id}}", tag, timeout=30
    )
    image_id = image_id.strip()
    if code != 0 or not image_id.startswith("sha256:"):
        raise ProviderStartError(
            f"Docker did not load the built Task image: {stderr.strip() or image_id}"
        )
    return _OciBuild(
        input_identity=input_identity,
        image_source_identity=source_digest,
        runtime_ref=tag,
        oci_identity=image_id,
        base_materials=_materials(metadata),
    )


def vm_materialization_identity(oci_identity: str, materializer_identity: str) -> str:
    return content_hash(
        {
            "output_contract": VM_OUTPUT_CONTRACT,
            "final_oci_identity": oci_identity,
            "materializer_identity": materializer_identity,
        }
    )


async def _materialize_vm(build: _OciBuild, *, source: str) -> PreparedTaskImage:
    return (await _materialize_vm_with_status(build, source=source))[0]


async def _materialize_vm_with_status(
    build: _OciBuild, *, source: str
) -> tuple[PreparedTaskImage, bool]:
    materializer_identity = await _materializer_identity()
    identity = vm_materialization_identity(build.oci_identity, materializer_identity)
    root = cache_root() / "vm-builds"
    root.mkdir(parents=True, exist_ok=True)
    disk = root / f"{identity.removeprefix('sha256:')}.qcow2"
    lock_path = disk.with_suffix(".lock")
    lock_path.touch(exist_ok=True)

    with lock_path.open("r+") as lock:
        await _acquire_lock(lock)
        try:
            reused = await _valid_qcow2(disk)
            if not reused:
                disk.unlink(missing_ok=True)
                await _materialize(build.runtime_ref, disk)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

    return (
        PreparedTaskImage(
            kind=ImageKind.VM,
            source=source,
            input_identity=build.input_identity,
            image_source_identity=build.image_source_identity,
            runtime_ref=str(disk.resolve()),
            prepared_identity=identity,
            base_materials=build.base_materials,
            oci_identity=build.oci_identity,
            materializer_identity=materializer_identity,
        ),
        reused,
    )


async def _materializer_identity() -> str:
    code, image_id, _ = await _run(
        "docker", "image", "inspect", "--format", "{{.Id}}", MATERIALIZER_IMAGE, timeout=30
    )
    if code != 0:
        code, _, stderr = await _run("docker", "pull", MATERIALIZER_IMAGE, timeout=900)
        if code != 0:
            raise ProviderStartError(
                f"could not acquire VM materializer {MATERIALIZER_IMAGE}: {stderr.strip()}"
            )
        code, image_id, stderr = await _run(
            "docker", "image", "inspect", "--format", "{{.Id}}", MATERIALIZER_IMAGE, timeout=30
        )
        if code != 0:
            raise ProviderStartError(f"could not inspect VM materializer: {stderr.strip()}")
    image_id = image_id.strip()
    if not image_id.startswith("sha256:"):
        raise ProviderStartError("VM materializer has no immutable local image identity")
    return content_hash(
        {
            "image_identity": image_id,
            "output_contract": VM_OUTPUT_CONTRACT,
        }
    )


async def _materialize(runtime_ref: str, disk: Path) -> None:
    temporary = Path(tempfile.mkdtemp(prefix=f".{disk.stem}-", dir=disk.parent))
    input_dir = temporary / "input"
    output_dir = temporary / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    container = ""
    try:
        code, output, stderr = await _run("docker", "create", runtime_ref, timeout=60)
        if code != 0:
            raise ProviderStartError(f"could not export local VM OCI rootfs: {stderr.strip()}")
        container = output.strip()
        code, _, stderr = await _run(
            "docker", "export", "--output", str(input_dir / "rootfs.tar"), container, timeout=900
        )
        if code != 0:
            raise ProviderStartError(f"could not export local VM OCI rootfs: {stderr.strip()}")
        await asyncio.to_thread(
            _extract_latest_initrd,
            input_dir / "rootfs.tar",
            input_dir / "initrd.img",
        )

        code, _, stderr = await _run(
            "docker",
            "run",
            "--rm",
            "--privileged",
            "--mount",
            f"type=bind,src={input_dir},dst=/input,readonly",
            "--mount",
            f"type=bind,src={output_dir},dst=/output",
            MATERIALIZER_IMAGE,
            timeout=1800,
        )
        if code != 0:
            raise ProviderStartError(f"VM materialization failed: {stderr.strip()}")
        candidate = output_dir / "disk.qcow2"
        if not await _valid_qcow2(candidate):
            raise ProviderStartError("VM materializer produced no valid qcow2")
        os.replace(candidate, disk)
    finally:
        if container:
            await _run("docker", "rm", "-f", container, timeout=60)
        shutil.rmtree(temporary, ignore_errors=True)


async def _valid_qcow2(path: Path) -> bool:
    if not path.is_file():
        return False
    code, _, _ = await _run("qemu-img", "check", "-q", str(path), timeout=300)
    return code == 0


async def _acquire_lock(handle: TextIO) -> None:
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            await asyncio.sleep(0.05)


def _materials(value: object) -> tuple[str, ...]:
    found: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            uri = item.get("uri")
            digest = item.get("digest")
            if isinstance(uri, str):
                found.add(uri)
            if isinstance(digest, dict):
                found.update(str(part) for part in digest.values())
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(sorted(found))


def _extract_latest_initrd(archive: Path, destination: Path) -> None:
    with tarfile.open(archive) as rootfs:
        candidates = sorted(
            (
                member
                for member in rootfs.getmembers()
                if member.isfile() and member.name.lstrip("/").startswith("boot/initrd.img-")
            ),
            key=lambda member: member.name,
        )
        if not candidates:
            raise ProviderStartError("local VM OCI image has no initramfs")
        source = rootfs.extractfile(candidates[-1])
        if source is None:
            raise ProviderStartError("could not read local VM initramfs")
        with source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)
