"""Prepare solver and verifier images before an episode starts."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from orbit.core.errors import (
    OrbitError,
    ProviderCapabilityError,
    ProviderStartError,
    TaskDefinitionError,
)
from orbit.core.ids import content_hash
from orbit.core.sandbox import ImageRef, PreparedTaskImage, Provider, SandboxRequest
from orbit.core.task import Task, TaskFolder
from orbit.core.taskspec import (
    ImageKind,
    ImageSpec,
    NetworkMode,
    NetworkPolicy,
    OperatingSystem,
    Resources,
    TaskSpec,
    VerificationMode,
)
from orbit.run.content import tree_digest
from orbit.run.framework import (
    KEEP_ALIVE,
    MATERIALIZER_IMAGE,
    fleet_framework_identity,
    framed_disk,
    framework_identity,
    macos_framework_identity,
    windows_framework_identity,
    with_framework,
)
from orbit.run.images import (
    acquire_lock,
    classify_image,
    finished_layer,
    remove_disk,
    resolve_loaded_image,
)
from orbit.run.process import run
from orbit.run.providers.fleet import FleetProvider
from orbit.run.providers.orchard import OrchardProvider, OrchardSandbox
from orbit.run.providers.orchard.client import CONTROL_ACCOUNT
from orbit.run.providers.qemu.provider import (
    LINUX_DISCARD,
    NTFS_DISCARD,
    RUNNER_IMAGE,
    QemuProvider,
)
from orbit.run.providers.slurm import SlurmProvider
from orbit.run.providers.slurm.images import Recipe, pin
from orbit.run.providers.slurm.images import key as cluster_key
from orbit.run.sources import cache_root
from orbit.run.tasksets.manifest import BUILD_SCRIPTS, SETUP_DIR, describe

__all__ = [
    "ImagePreparationResult",
    "ImagePreparationStep",
    "prepare_task_image",
    "prepare_task_image_result",
    "prepare_validation_image",
    "prepare_verifier_image",
    "prepare_verifier_image_result",
    "resolve_image_kinds",
    "shares_verifier_image",
    "validates_separately",
]

#: One of a Task's images: the solver's, the verifier's of ``tests/``, or that of the verifier
#: of validation rounds.
ImageRole = Literal["solver", "verifier", "validation"]

WINDOWS_BUILD_CONTRACT = "orbit-windows-powershell/v1"
LINUX_BUILD_CONTRACT = "orbit-linux-bash/v1"
MACOS_BUILD_CONTRACT = "orbit-macos-bash/v1"


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
    except OrbitError as error:
        message = str(error).replace("\n", " ")
        if len(message) > 2000:
            message = f"{message[:1000]} ... {message[-995:]}"
        raise type(error)(f"{name}: {message}") from error


async def _run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
    return await run(*argv, timeout=timeout)


def _task_folder(task: Task) -> TaskFolder:
    if task.folder is None:
        raise TaskDefinitionError("standard image preparation requires a Task folder")
    return task.folder


async def resolve_image_kinds(tasks: Iterable[Task]) -> None:
    """Decide the kind of every image a Linux Task names by a registry reference alone, by
    reading its manifest, before anything prepares, places or records the image.

    Every other image's kind follows from the Task folder and was decided as the Task was
    loaded. A Task whose image this decides continues with a specification carrying the
    kind, checked again as a whole, so a VM image with container features is refused;
    each reference is read once, however many Tasks name it.
    """
    tasks = list(tasks)
    references = sorted(
        {
            image.ref
            for task in tasks
            for image in task.spec.images
            if image.kind is None and image.ref is not None
        }
    )
    if not references:
        return
    found = await asyncio.gather(
        *(_stage("image-classification", classify_image(ref)) for ref in references),
        return_exceptions=True,
    )
    kinds: dict[str, ImageKind] = {}
    for reference, outcome in zip(references, found, strict=True):
        if isinstance(outcome, BaseException):
            raise outcome
        kinds[reference] = outcome

    def decided(image: ImageSpec | None) -> ImageSpec | None:
        if image is None or image.kind is not None or image.ref is None:
            return image
        return image.model_copy(update={"kind": kinds[image.ref]})

    for task in tasks:
        spec = task.spec
        image, verifier = decided(spec.image), decided(spec.verify.image)
        own = spec.validation
        validation = decided(own.image) if own is not None else None
        if (
            image is not spec.image
            or verifier is not spec.verify.image
            or (own is not None and validation is not own.image)
        ):
            verify = spec.verify.model_copy(update={"image": verifier})
            placed = own.model_copy(update={"image": validation}) if own is not None else None
            try:
                task.spec = TaskSpec.model_validate(
                    {**dict(spec), "image": image, "verify": verify, "validation": placed}
                )
            except ValidationError as error:
                raise TaskDefinitionError(f"invalid Task {spec.name}: {describe(error)}") from None


def _kind(image: ImageSpec) -> ImageKind:
    """The kind of an image about to be prepared, which ``resolve_image_kinds`` decided."""
    if image.kind is None:
        raise TaskDefinitionError(f"the kind of {image.ref} was not decided before its preparation")
    return image.kind


async def prepare_task_image(task: Task, provider: Provider) -> PreparedTaskImage:
    return (await prepare_task_image_result(task, provider)).image


async def prepare_task_image_result(task: Task, provider: Provider) -> ImagePreparationResult:
    """The solver's image, prepared for ``provider``: prepared images are
    Provider-specific."""
    spec = task.spec.image
    kind = _kind(spec)
    folder = _task_folder(task)
    if isinstance(provider, FleetProvider):
        return _on_fleet(task, "solver", provider)
    if isinstance(provider, SlurmProvider):
        return await _on_cluster(task, "solver", provider)
    if spec.base_ref is not None:
        base = await _stage(
            "base-resolution",
            provider.prepare_image(ImageRef(kind=kind, reference=spec.base_ref)),
        )
        step = f"{task.spec.os.value}-build"
        candidate, reused = await _stage(
            step,
            _prepare_macos_image(task, base, provider)
            if task.spec.os is OperatingSystem.MACOS
            else _prepare_qemu_image(task, base),
        )
        image = await _stage("artifact-check", provider.prepare_image(candidate))
        return _result(
            task,
            "solver",
            image,
            ImagePreparationStep("base-resolution", "executed", base.prepared_identity),
            ImagePreparationStep(
                step,
                "reused" if reused else "executed",
                image.prepared_identity,
                image.runtime_ref,
            ),
            ImagePreparationStep("artifact-check", "executed", image.prepared_identity),
        )
    if spec.ref is not None:
        image = await _stage(
            "reference-resolution",
            provider.prepare_image(ImageRef(kind=kind, reference=spec.ref)),
        )
        steps = [
            ImagePreparationStep(
                "reference-resolution",
                "executed",
                image.prepared_identity,
                image.resolved_reference,
            )
        ]
        if kind is ImageKind.CONTAINER:
            image = await _stage(
                "framework-layer",
                _framed_reference(image, user=spec.user, keep_alive=spec.keep_alive),
            )
            steps.append(
                ImagePreparationStep("framework-layer", "executed", image.prepared_identity)
            )
        elif task.spec.os is not OperatingSystem.MACOS:
            # The Orchard Provider frames a macOS image the first time it boots it.
            image, reused = await _stage(
                "framework-layer",
                framed_disk(image, operating_system=task.spec.os, user=spec.user),
            )
            steps.append(
                ImagePreparationStep(
                    "framework-layer",
                    "reused" if reused else "executed",
                    image.prepared_identity,
                    image.runtime_ref,
                )
            )
        return _result(
            task,
            "solver",
            image,
            *steps,
            ImagePreparationStep(
                "artifact-check", "executed", image.prepared_identity, image.runtime_ref
            ),
        )

    source_identity = task.image_source_digest
    if source_identity is None:
        raise TaskDefinitionError("local solver image has no source identity")
    dockerfile = folder.dockerfile
    if dockerfile is None:
        raise TaskDefinitionError("the solver image has no Dockerfile to build")
    candidate, steps = await _prepare_local(
        dockerfile=dockerfile,
        # Setup is uploaded at setup time; it never enters an image.
        exclude=(SETUP_DIR,),
        source_digest=source_identity,
        source="solver-local",
        user=spec.user,
        keep_alive=spec.keep_alive,
        build_timeout_sec=spec.build_timeout_sec,
    )
    image = await _stage("artifact-check", provider.prepare_image(candidate))
    steps.append(
        ImagePreparationStep(
            "artifact-check", "executed", image.prepared_identity, image.runtime_ref
        )
    )
    return _result(task, "solver", image, *steps)


async def prepare_verifier_image(task: Task, provider: Provider) -> PreparedTaskImage | None:
    result = await prepare_verifier_image_result(task, provider)
    return result.image if result is not None else None


async def prepare_validation_image(task: Task, provider: Provider) -> PreparedTaskImage | None:
    """The image of a separate verifier of validation rounds: the verifier's of ``tests/``
    where it is the same, prepared before it, or else its own. A shared or snapshot one boots
    from the solver's snapshot, on the solver's leaf, so it needs none."""
    if not validates_separately(task):
        return None
    if shares_verifier_image(task):
        return task.prepared_verifier_image
    result = await prepare_verifier_image_result(task, provider, role="validation")
    return result.image if result is not None else None


def validates_separately(task: Task) -> bool:
    """Whether the Task's episodes validate in a separate verifier, of an image of its own,
    rather than in one booted from the solver's snapshot."""
    mode = task.spec.validation_spec.environment_mode
    return task.validates and mode is VerificationMode.SEPARATE


def shares_verifier_image(task: Task) -> bool:
    """Whether a separate verifier of validation rounds runs the image of ``tests/``'s
    separate one: as it inherits it, or both the solver's, prepared for the verifier's
    Provider."""
    spec = task.spec
    if spec.verify.environment_mode is not VerificationMode.SEPARATE:
        return False
    return spec.validation is None or (
        spec.validation.image == spec.verify.image and task.validation_image_source_digest is None
    )


async def prepare_verifier_image_result(
    task: Task, provider: Provider, *, role: Literal["verifier", "validation"] = "verifier"
) -> ImagePreparationResult | None:
    """The image of a verifier of its own, prepared for ``provider``: of ``tests/``'s, or of
    the verifier of validation rounds for ``role`` validation; a separate verifier's own, or
    else the solver's, which a snapshot verifier boots from the snapshot of; none when it
    runs in the solver or a Sandbox booted from its snapshot as a shared one."""
    placed = task.spec.validation_spec if role == "validation" else task.spec.verify
    if placed.environment_mode is VerificationMode.SHARED:
        return None

    if placed.image is None:
        image = task.prepared_image
        if image is None:
            raise TaskDefinitionError("solver image must be prepared before verifier image")
        if task.verifier_provider != task.provider:
            # The solver's image, for the Provider the verifier's own needs selected: the
            # very image the solver ran, checked by that Provider, unless either is the
            # Fleet, which prepares an image on its Nodes from what the Task declares.
            if isinstance(provider, FleetProvider) or task.provider == FleetProvider.name:
                result = await prepare_task_image_result(_pinned(task, image), provider)
                return dataclasses.replace(result, role=role)
            image = await _stage("artifact-check", provider.prepare_image(image))
            return _result(
                task,
                role,
                image,
                ImagePreparationStep(
                    "artifact-check", "executed", image.prepared_identity, image.runtime_ref
                ),
            )
        return _result(
            task,
            role,
            image,
            ImagePreparationStep(
                "artifact-check", "reused", image.prepared_identity, image.runtime_ref
            ),
        )

    declared = placed.image
    kind = _kind(declared)
    if isinstance(provider, FleetProvider):
        return _on_fleet(task, role, provider)
    if isinstance(provider, SlurmProvider):
        return await _on_cluster(task, role, provider)
    if declared.ref is not None:
        image = await _stage(
            "reference-resolution",
            provider.prepare_image(ImageRef(kind=kind, reference=declared.ref)),
        )
        steps = [
            ImagePreparationStep(
                "reference-resolution",
                "executed",
                image.prepared_identity,
                image.resolved_reference,
            )
        ]
        if kind is ImageKind.CONTAINER:
            image = await _stage(
                "framework-layer",
                _framed_reference(image, user=declared.user, keep_alive=declared.keep_alive),
            )
            steps.append(
                ImagePreparationStep("framework-layer", "executed", image.prepared_identity)
            )
        elif task.spec.os is not OperatingSystem.MACOS:
            image, reused = await _stage(
                "framework-layer",
                framed_disk(image, operating_system=task.spec.os, user=declared.user),
            )
            steps.append(
                ImagePreparationStep(
                    "framework-layer",
                    "reused" if reused else "executed",
                    image.prepared_identity,
                    image.runtime_ref,
                )
            )
        return _result(
            task,
            role,
            image,
            *steps,
            ImagePreparationStep(
                "artifact-check", "executed", image.prepared_identity, image.runtime_ref
            ),
        )

    folder = _task_folder(task)
    if role == "validation" and task.validation_image_source_digest is not None:
        source_identity, dockerfile = (
            task.validation_image_source_digest,
            folder.validation_dockerfile,
        )
        source = "validation-local"
    else:
        source_identity, dockerfile = task.verifier_image_source_digest, folder.verifier_dockerfile
        source = "verifier-local"
    if source_identity is None or dockerfile is None:
        raise TaskDefinitionError(f"the {role} verifier's image has no Dockerfile to build")
    candidate, steps = await _prepare_local(
        dockerfile=dockerfile,
        exclude=(),
        source_digest=source_identity,
        source=source,
        user=declared.user,
        keep_alive=declared.keep_alive,
        build_timeout_sec=declared.build_timeout_sec,
    )
    image = await _stage("artifact-check", provider.prepare_image(candidate))
    steps.append(
        ImagePreparationStep(
            "artifact-check", "executed", image.prepared_identity, image.runtime_ref
        )
    )
    return _result(task, role, image, *steps)


def _pinned(task: Task, solver: PreparedTaskImage) -> Task:
    """``task`` with its named solver image pinned to the digest it resolved to here, so
    that another Provider prepares the very image the solver ran rather than what its tag
    names now. A solver on the Fleet resolves its tag on its Node, after this Run prepared
    its images, so its image is left as declared."""
    declared = task.spec.image
    if declared.ref is None or task.provider == FleetProvider.name:
        return task
    assert solver.resolved_reference is not None, "a named image is resolved as it is prepared"
    pinned = copy.copy(task)
    image = declared.model_copy(update={"ref": solver.resolved_reference})
    pinned.spec = task.spec.model_copy(update={"image": image})
    return pinned


def _on_fleet(task: Task, role: ImageRole, provider: FleetProvider) -> ImagePreparationResult:
    """One of a Task's images as the Fleet prepares it: on each Node that runs it, from what
    this names, through this same preparation.

    Its key covers everything that preparation reads here: the image's declaration, the
    Task's storage, which a VM build sizes its disk by, the build context as
    ``_context_identity`` observes it, and the framework.
    """
    image = {
        "solver": task.spec.image,
        "verifier": task.spec.verify.image,
        "validation": task.spec.validation_spec.image,
    }[role]
    assert image is not None
    kind = _kind(image)
    folder = _task_folder(task)
    context: Path | None = None
    exclude: tuple[str, ...] = ()
    if image.ref is None:
        if role == "solver":
            context, exclude = folder.environment_dir, (SETUP_DIR,)
        elif role == "validation" and task.validation_image_source_digest is not None:
            context = folder.root / "validation"
        else:
            context = folder.root / "tests"
    identity = _context_identity(context, exclude=exclude) if context else None
    key = content_hash(
        {
            "role": role,
            "os": task.spec.os,
            "image": image.model_dump(mode="json"),
            "storage_mb": task.spec.resources.storage_mb,
            "context": identity,
            "framework": fleet_framework_identity(),
        }
    )
    prepared = provider.defer(
        key,
        kind=kind,
        task=task.spec,
        role=role,
        context=context,
        exclude=exclude,
        unchanged=(
            (lambda: _context_identity(context, exclude=exclude) == identity)
            if context is not None
            else None
        ),
    )
    return _result(
        task,
        role,
        prepared,
        ImagePreparationStep("fleet", "deferred", key, "prepared on each Node that runs it"),
    )


async def _on_cluster(
    task: Task, role: ImageRole, provider: SlurmProvider
) -> ImagePreparationResult:
    """One of a Task's images as the Slurm Provider prepares it, on the cluster, from what
    the Task declares, as no container runtime runs where Orbit does: a named image pinned by
    the digest its registry gives it now, or the Task's own build, with the framework layer.

    Its key covers what goes into it: the pinned name, or the build context as
    ``_context_identity`` observes it, and the framework for its account and command.
    """
    image = {
        "solver": task.spec.image,
        "verifier": task.spec.verify.image,
        "validation": task.spec.validation_spec.image,
    }[role]
    assert image is not None
    if _kind(image) is not ImageKind.CONTAINER or task.spec.os is not OperatingSystem.LINUX:
        raise TaskDefinitionError("Slurm runs Linux container images only")
    folder = _task_folder(task)
    reference: str | None = None
    context: Path | None = None
    exclude: tuple[str, ...] = ()
    source: Literal["solver-local", "verifier-local", "validation-local", "external-ref"]
    if image.ref is not None:
        reference = await _stage("reference-resolution", pin(image.ref))
        source, source_identity = "external-ref", None
    elif role == "solver":
        context, exclude = folder.environment_dir, (SETUP_DIR,)
        source, source_identity = "solver-local", task.image_source_digest
    elif role == "validation" and task.validation_image_source_digest is not None:
        context = folder.root / "validation"
        source, source_identity = "validation-local", task.validation_image_source_digest
    else:
        context = folder.root / "tests"
        source, source_identity = "verifier-local", task.verifier_image_source_digest
    if context is not None and (source_identity is None or not (context / "Dockerfile").is_file()):
        raise TaskDefinitionError(f"the {role} image has no Dockerfile to build")
    key = cluster_key(
        source,
        reference=reference,
        context=_context_identity(context, exclude=exclude) if context else None,
        user=image.user,
        keep_alive=image.keep_alive,
    )
    prepared, reused = await _stage(
        "slurm-preparation",
        provider.prepare_recipe(
            Recipe(
                key=key,
                source=source,
                reference=reference,
                context=context,
                exclude=exclude,
                source_identity=source_identity,
                user=image.user,
                keep_alive=image.keep_alive,
                timeout_sec=image.build_timeout_sec,
            )
        ),
    )
    steps = [
        ImagePreparationStep(
            "slurm-preparation",
            "reused" if reused else "executed",
            prepared.prepared_identity,
            reference or "built from the Task's context on the cluster",
        )
    ]
    if reference is not None:
        steps.insert(0, ImagePreparationStep("reference-resolution", "executed", None, reference))
    return _result(task, role, prepared, *steps)


def _result(
    task: Task,
    role: str,
    image: PreparedTaskImage,
    *steps: ImagePreparationStep,
) -> ImagePreparationResult:
    return ImagePreparationResult(
        task=str(task.spec.name),
        role=role,
        image=image,
        steps=(ImagePreparationStep("lint", "executed"), *steps),
    )


async def _prepare_local(
    *,
    dockerfile: Path,
    exclude: tuple[str, ...],
    source_digest: str,
    source: str,
    user: str,
    keep_alive: bool,
    build_timeout_sec: float,
) -> tuple[PreparedTaskImage, list[ImagePreparationStep]]:
    """The Task's own container build with the framework layer on top, built once per input.

    The result is recorded under everything that goes into it: the build context, its
    asset inventory and the framework. An unchanged Task finds its image there and uses it
    directly, without asking Docker to build or check anything.
    """
    context = dockerfile.parent
    key = content_hash(
        {
            "source": source,
            "context": _context_identity(context, exclude=exclude),
            "framework": framework_identity(user=user, vm=False),
            # Only when set, so every other Task finds the image it recorded.
            **({"command": KEEP_ALIVE} if keep_alive else {}),
        }
    )
    root = cache_root() / "images"
    root.mkdir(parents=True, exist_ok=True)
    record = root / f"{key.removeprefix('sha256:')}.json"
    with record.with_suffix(".lock").open("a") as lock:
        await acquire_lock(lock)
        try:
            found = await _recorded(record, source=source, source_digest=source_digest)
            reused = found is not None
            if found is None:
                build = await _stage(
                    "oci-build",
                    _build_oci(
                        dockerfile=dockerfile,
                        exclude=exclude,
                        source_digest=source_digest,
                        source=source,
                        timeout=build_timeout_sec,
                    ),
                )
                layered = await _stage(
                    "framework-layer",
                    with_framework(build.runtime_ref, user=user, keep_alive=keep_alive),
                )
                found = (
                    build.oci_identity,
                    dataclasses.replace(build, runtime_ref=layered, oci_identity=layered),
                )
                _record(record, *found)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    built, build = found
    outcome = "reused" if reused else "executed"
    steps = [
        ImagePreparationStep("oci-build", outcome, built),
        ImagePreparationStep("framework-layer", outcome, build.oci_identity),
    ]
    return _local_container(build, source=source), steps


def _context_identity(context: Path, *, exclude: tuple[str, ...] = ()) -> str:
    """What a build reads from its context, apart from the excluded directories.

    File contents are hashed. Assets are observed by path, size, modification time and
    mode, as asset dirtiness is, so finding an unchanged Task's image never re-reads them.
    """
    assets = context / "assets"
    excluded = tuple(context / name for name in exclude)
    return content_hash(
        {
            "files": tree_digest(context, exclude=("assets", *exclude)),
            "directories": sorted(
                str(entry.relative_to(context))
                for entry in context.rglob("*")
                if entry.is_dir()
                and not any(entry == item or item in entry.parents for item in excluded)
            ),
            "assets": [
                [
                    str(entry.relative_to(assets)),
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_mode & 0o111,
                ]
                for entry in sorted(assets.rglob("*"))
                if entry.is_file() and (stat := entry.stat())
            ],
        }
    )


async def _recorded(
    record: Path, *, source: str, source_digest: str
) -> tuple[str, _OciBuild] | None:
    """The Task's own build and its framed image an earlier preparation recorded, if Docker
    still has the framed image."""
    try:
        recorded = json.loads(record.read_text(encoding="utf-8"))
        built = recorded["built"]
        image = recorded["image"]
        materials = tuple(recorded["materials"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    code, observed, _ = await _run(
        "docker", "image", "inspect", "--format", "{{.Id}}", image, timeout=30
    )
    if code != 0 or observed.strip() != image:
        return None
    return built, _OciBuild(
        input_identity=_input_identity(source, source_digest),
        image_source_identity=source_digest,
        runtime_ref=image,
        oci_identity=image,
        base_materials=materials,
    )


def _record(record: Path, built: str, build: _OciBuild) -> None:
    partial = record.with_suffix(".partial")
    partial.write_text(
        json.dumps(
            {"built": built, "image": build.oci_identity, "materials": list(build.base_materials)}
        ),
        encoding="utf-8",
    )
    os.replace(partial, record)


async def _framed_reference(
    image: PreparedTaskImage, *, user: str, keep_alive: bool
) -> PreparedTaskImage:
    layered = await with_framework(image.runtime_ref, user=user, keep_alive=keep_alive)
    return image.model_copy(update={"runtime_ref": layered, "prepared_identity": layered})


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


async def _prepare_qemu_image(
    task: Task, base: PreparedTaskImage
) -> tuple[PreparedTaskImage, bool]:
    """Run the Task's build script in a QEMU VM of its base and keep the VM's overlay, the
    framework and the build in it, as the Task's layer on the base.

    Linux and Windows build alike; only the script, where it runs and how its free space is
    discarded differ. The base must already hold the Task's account.
    """
    operating_system = task.spec.os
    label = "Windows" if operating_system is OperatingSystem.WINDOWS else "Linux"
    script = BUILD_SCRIPTS[operating_system]
    folder = _task_folder(task)
    if folder.build_script(operating_system) is None or task.image_source_digest is None:
        raise TaskDefinitionError(f"a {label} image build requires environment/{script}")
    context = folder.environment_dir
    for entry in _vm_context_entries(context):
        if entry.is_symlink():
            raise TaskDefinitionError(f"{label} image inputs cannot be symlinks: {entry}")
    context_identity = await asyncio.to_thread(_vm_context_identity, context)
    runner_identity = await _docker_image_identity(RUNNER_IMAGE)
    user = task.spec.image.user
    windows = operating_system is OperatingSystem.WINDOWS
    builder_identity = content_hash(
        {
            "contract": WINDOWS_BUILD_CONTRACT if windows else LINUX_BUILD_CONTRACT,
            "runner": runner_identity,
            # Frame the build VM's disk and discard its free space.
            "vm_builder": await _docker_image_identity(MATERIALIZER_IMAGE),
            "discard": (
                hashlib.sha256(NTFS_DISCARD.read_bytes()).hexdigest()
                if windows
                else shlex.join(LINUX_DISCARD)
            ),
            "framework": (
                windows_framework_identity(user=user)
                if windows
                else framework_identity(user=user, vm=True)
            ),
        }
    )
    resources = Resources(cpus=2, memory_mb=4096, storage_mb=task.spec.resources.storage_mb)
    key = content_hash(
        {
            "base": base.prepared_identity,
            "context": context_identity,
            "builder": builder_identity,
            "resources": resources.model_dump(mode="json"),
        }
    )
    root = cache_root() / "vm-builds"
    root.mkdir(parents=True, exist_ok=True)
    # The build VM's overlay, kept as a layer on the base: the Task's layer.
    layer = root / f"{key.removeprefix('sha256:')}.qcow2"
    with layer.with_suffix(".lock").open("a+") as lock:
        await acquire_lock(lock)
        try:
            identity = await finished_layer(layer)
            reused = identity is not None
            if identity is None:
                await remove_disk(layer)
                with tempfile.TemporaryDirectory(prefix=f".{layer.stem}-", dir=root) as temporary:
                    staged = Path(temporary) / "context"
                    _stage_vm_context(context, staged)
                    identity = await _build_qemu_image(
                        base,
                        staged,
                        layer,
                        resources,
                        runner_identity,
                        operating_system=operating_system,
                        user=user,
                        timeout_sec=task.spec.image.build_timeout_sec,
                        changed=lambda: _vm_context_identity(context) != context_identity,
                    )
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return PreparedTaskImage(
        kind=ImageKind.VM,
        source="solver-local",
        input_identity=key,
        image_source_identity=task.image_source_digest,
        runtime_ref=str(layer.resolve()),
        prepared_identity=identity,
        base_materials=(base.prepared_identity,),
        builder_identity=builder_identity,
    ), reused


async def _prepare_macos_image(
    task: Task, base: PreparedTaskImage, provider: Provider
) -> tuple[PreparedTaskImage, bool]:
    """Run ``environment/build.sh`` as root in a VM of the framed base and keep its disk.

    The result is a local image on the worker; a sandbox frames it for the Task's account
    when it first uses it.
    """
    folder = _task_folder(task)
    if folder.build_script(OperatingSystem.MACOS) is None or task.image_source_digest is None:
        raise TaskDefinitionError("a macOS image build requires environment/build.sh")
    if not isinstance(provider, OrchardProvider):
        raise ProviderStartError("macOS image builds run on the local Orchard")
    context = folder.environment_dir
    for entry in _vm_context_entries(context):
        if entry.is_symlink():
            raise TaskDefinitionError(f"macOS image inputs cannot be symlinks: {entry}")
    context_identity = await asyncio.to_thread(_vm_context_identity, context)
    user = task.spec.image.user
    # build.sh runs as root, in a VM of the base framed for the account Orbit controls it
    # through, so it may create the Task's account; a sandbox frames the result for that.
    builder_identity = content_hash(
        {
            "contract": MACOS_BUILD_CONTRACT,
            "framework": macos_framework_identity(user=CONTROL_ACCOUNT),
            "user": user,
        }
    )
    resources = Resources(cpus=2, memory_mb=4096, storage_mb=task.spec.resources.storage_mb)
    identity = content_hash(
        {
            "base": base.prepared_identity,
            "context": context_identity,
            "builder": builder_identity,
            "resources": resources.model_dump(mode="json"),
        }
    )
    image = PreparedTaskImage(
        kind=ImageKind.VM,
        source="solver-local",
        input_identity=identity,
        image_source_identity=task.image_source_digest,
        runtime_ref="orbit-built-" + identity.removeprefix("sha256:")[:32],
        prepared_identity=identity,
        base_materials=(base.prepared_identity,),
        builder_identity=builder_identity,
    )
    root = cache_root() / "macos-builds"
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{image.runtime_ref}.lock").open("a+") as lock:
        await acquire_lock(lock)
        try:
            try:
                return await provider.prepare_image(image), True
            except ProviderCapabilityError:
                pass
            with tempfile.TemporaryDirectory(prefix=f".{image.runtime_ref}-", dir=root) as staged:
                _stage_vm_context(context, Path(staged) / "context")
                sandbox = await provider.create_builder(
                    SandboxRequest(
                        episode_id=f"image-{image.runtime_ref}",
                        os=OperatingSystem.MACOS,
                        prepared_image=base,
                        resources=resources,
                        network=NetworkPolicy(mode=NetworkMode.OPEN),
                        sudo=True,
                        user=CONTROL_ACCOUNT,
                    )
                )
                try:
                    await _build_macos_image(
                        sandbox,
                        Path(staged) / "context",
                        user=user,
                        timeout_sec=task.spec.image.build_timeout_sec,
                    )
                    if await asyncio.to_thread(_vm_context_identity, context) != context_identity:
                        raise TaskDefinitionError("macOS image inputs changed during build")
                    await sandbox.save_image(image)
                finally:
                    await asyncio.shield(sandbox.destroy())
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return image, False


async def _build_macos_image(
    sandbox: OrchardSandbox, context: Path, *, user: str, timeout_sec: float
) -> None:
    """Run ``build.sh`` with ``ORBIT_HOME`` set to the home of the Task's account, as on Windows.

    That is the home the base gives the account, or ``/Users/<user>`` for an account the
    build creates, which must give it that home.
    """
    home = await sandbox.account_home(user) or f"/Users/{user}"
    target = "/opt/orbit/environment"
    await sandbox.upload_dir(str(context), target)
    result = await sandbox.exec(
        ["bash", "build.sh"],
        cwd=target,
        env={"ORBIT_HOME": home},
        timeout_sec=timeout_sec,
    )
    if not result.ok:
        raise TaskDefinitionError(
            f"environment/build.sh failed (exit={result.exit_code}, timeout={result.timed_out}): "
            f"{result.stderr or result.stdout}"
        )
    built = await sandbox.account_home(user)
    if built is None:
        raise TaskDefinitionError(f"the image environment/build.sh built has no account {user!r}")
    if built != home:
        raise TaskDefinitionError(
            f"environment/build.sh gave the account {user!r} the home {built}, "
            f"not ORBIT_HOME ({home})"
        )
    result = await sandbox.exec(["rm", "-rf", target], timeout_sec=60)
    if not result.ok:
        raise ProviderStartError(f"could not remove image build inputs: {result.stderr}")


def _vm_context_entries(context: Path) -> list[Path]:
    """A VM build's context: ``environment/`` less ``setup/``, as for a Dockerfile."""
    return [
        entry for entry in context.rglob("*") if entry.relative_to(context).parts[0] != SETUP_DIR
    ]


def _vm_context_identity(context: Path) -> str:
    return content_hash(
        {
            "files": tree_digest(context, exclude=(SETUP_DIR,)),
            "directories": sorted(
                str(entry.relative_to(context))
                for entry in _vm_context_entries(context)
                if entry.is_dir()
            ),
        }
    )


def _stage_vm_context(context: Path, staged: Path) -> None:
    """Link the build context into ``staged``, copying where a link cannot be made."""

    def link(source: str, target: str) -> None:
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)

    shutil.copytree(
        context,
        staged,
        ignore=lambda directory, _: [SETUP_DIR] if Path(directory) == context else [],
        copy_function=link,
    )


async def _build_qemu_image(
    base: PreparedTaskImage,
    context: Path,
    layer: Path,
    resources: Resources,
    runner_identity: str,
    *,
    operating_system: OperatingSystem,
    user: str,
    timeout_sec: float,
    changed: Callable[[], bool],
) -> str:
    """Run the build script in a VM whose overlay on the base is framed before it boots, and
    keep that overlay as ``layer``; the layer's identity. ``changed`` says whether the
    build's inputs changed meanwhile.

    The script runs as the framework's own account (root, or an administrator on Windows)
    in a copy of ``environment/``, with ``ORBIT_HOME`` set to the agent's home.
    """
    provider = QemuProvider(overlay_dir=context.parent / "vm", runner_image=runner_identity)
    sandbox = await provider.create_builder(
        SandboxRequest(
            episode_id=f"image-{context.parent.name}",
            os=operating_system,
            prepared_image=base,
            resources=resources,
            network=NetworkPolicy(mode=NetworkMode.OPEN),
            sudo=True,
            user=user,
        )
    )
    script = BUILD_SCRIPTS[operating_system]
    if operating_system is OperatingSystem.WINDOWS:
        target = r"C:\ProgramData\Orbit\environment"
        build = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "$ErrorActionPreference = 'Stop'; & .\\build.ps1; "
            "if (-not $?) { exit 1 }; if ($LASTEXITCODE) { exit $LASTEXITCODE }",
        ]
        remove = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Remove-Item -LiteralPath '{target}' -Recurse -Force -ErrorAction Stop",
        ]
    else:
        # Under /opt/orbit, which only root may read.
        target = "/opt/orbit/environment"
        build = ["bash", script]
        remove = ["rm", "-rf", target]
    try:
        await sandbox.upload_dir(str(context), target)
        result = await sandbox.exec(
            build,
            cwd=target,
            env={"ORBIT_HOME": sandbox.agent_home},
            timeout_sec=timeout_sec,
        )
        if not result.ok:
            raise TaskDefinitionError(
                f"environment/{script} failed (exit={result.exit_code}, "
                f"timeout={result.timed_out}): "
                f"{result.stderr or result.stdout}"
            )
        result = await sandbox.exec(remove, timeout_sec=60)
        if not result.ok:
            raise ProviderStartError(f"could not remove image build inputs: {result.stderr}")
        if await asyncio.to_thread(changed):
            raise TaskDefinitionError("image build inputs changed during the build")
        return await sandbox.save_image(layer)
    finally:
        await asyncio.shield(sandbox.destroy())


async def _build_oci(
    *,
    dockerfile: Path,
    exclude: tuple[str, ...],
    source_digest: str,
    source: str,
    timeout: float,
) -> _OciBuild:
    """Build ``dockerfile`` with its directory as context, less the excluded directories.

    Exclusion uses a Dockerfile-specific ignore file beside a copy of the Dockerfile, which
    BuildKit reads instead of the context's own ``.dockerignore``; that one's rules come
    first, so the Task cannot bring an excluded directory back.
    """
    code, _, stderr = await _run("docker", "buildx", "version", timeout=30)
    if code != 0:
        raise ProviderStartError(f"Docker Buildx is unavailable: {stderr.strip()}")

    context = dockerfile.parent
    input_identity = _input_identity(source, source_digest)
    tag = f"orbit-{source}:{input_identity.removeprefix('sha256:')}"
    with tempfile.TemporaryDirectory(prefix="orbit-image-") as temporary:
        metadata_file = Path(temporary) / "metadata.json"
        file_arguments: tuple[str, ...] = ()
        if exclude:
            copied = Path(temporary) / "Dockerfile"
            shutil.copyfile(dockerfile, copied)
            own = context / ".dockerignore"
            rules = own.read_text(encoding="utf-8").splitlines() if own.is_file() else []
            rules += [pattern for name in exclude for pattern in (name, f"{name}/**")]
            copied.with_name("Dockerfile.dockerignore").write_text(
                "\n".join(rules) + "\n", encoding="utf-8"
            )
            file_arguments = ("--file", str(copied))
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
            *file_arguments,
            str(context),
            timeout=timeout,
        )
        if code != 0:
            raise TaskDefinitionError(f"Task image build failed: {stderr.strip()}")
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderStartError(f"Buildx wrote invalid metadata: {exc}") from exc

    image_id = await resolve_loaded_image(metadata, run=_run)
    return _OciBuild(
        input_identity=input_identity,
        image_source_identity=source_digest,
        runtime_ref=image_id,
        oci_identity=image_id,
        base_materials=_materials(metadata),
    )


def _input_identity(source: str, source_digest: str) -> str:
    return content_hash(
        {"source": source, "source_identity": source_digest, "build": {"load": True}}
    )


async def _docker_image_identity(reference: str) -> str:
    code, image_id, _ = await _run(
        "docker", "image", "inspect", "--format", "{{.Id}}", reference, timeout=30
    )
    if code != 0:
        code, _, stderr = await _run("docker", "pull", reference, timeout=900)
        if code != 0:
            raise ProviderStartError(f"could not acquire image {reference}: {stderr.strip()}")
        code, image_id, stderr = await _run(
            "docker", "image", "inspect", "--format", "{{.Id}}", reference, timeout=30
        )
        if code != 0:
            raise ProviderStartError(f"could not inspect image {reference}: {stderr.strip()}")
    image_id = image_id.strip()
    if not image_id.startswith("sha256:"):
        raise ProviderStartError(f"image {reference} has no immutable local identity")
    return image_id


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
