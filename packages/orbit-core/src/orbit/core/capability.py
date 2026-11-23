"""Capability profiles, and the one rule that decides whether a profile serves a Sandbox.

A leaf, a Provider that starts and drives the final Sandbox, declares each real
combination it can start as a :class:`CapabilityProfile`. A Run choosing a Provider and a
Fleet placing a Sandbox on a Node both compare the Sandbox's :class:`SandboxNeeds` with
profiles through :func:`unmet`, so the same Sandbox is accepted or refused the same way
wherever it is asked for.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from orbit.core.taskspec import (
    ContainerFeature,
    ImageKind,
    NetworkMode,
    OperatingSystem,
    Resources,
    TaskSpec,
    VerificationMode,
)

__all__ = [
    "Arch",
    "CapabilityProfile",
    "GpuHold",
    "GpuModel",
    "SandboxNeeds",
    "accepts_model",
    "closest",
    "unmet",
]

_STRICT = ConfigDict(frozen=True, extra="forbid")


class Arch(StrEnum):
    """The instruction set a Sandbox runs, which its image fixes."""

    X86_64 = "x86_64"
    ARM64 = "arm64"

    @classmethod
    def of(cls, operating_system: OperatingSystem) -> Arch:
        """What a Task's Sandbox runs. A Task names no architecture: Orbit's images and the
        Tasks built on them are x86-64, apart from macOS, which only Apple silicon runs."""
        return cls.ARM64 if operating_system is OperatingSystem.MACOS else cls.X86_64


class GpuHold(StrEnum):
    """How a Sandbox holds a GPU."""

    WHOLE = "whole"
    """To itself: no other Sandbox shares it."""
    LIMIT = "limit"
    """Shared by ``gpu_memory_mb``, which a limit on the device memory it allocates enforces."""
    WATCHED = "watched"
    """Shared by ``gpu_memory_mb``, which nothing enforces as it allocates: its Provider watches
    what it uses, and acts once it uses more."""


class GpuModel(BaseModel):
    """A GPU model a leaf attaches."""

    model_config = _STRICT

    name: str = Field(min_length=1)
    """As ``nvidia-smi`` names it, which a Task's ``gpu_types`` accept."""
    memory_mb: int | None = Field(default=None, ge=1)
    """The most device memory, in MiB, a Sandbox may need on one GPU of this model: on a leaf
    that shares GPUs, the most it admits on an empty one, its ``memory.total`` less what the
    driver reserves and the leaf's margin; on one that gives them whole, its memory. None
    where the leaf does not know it."""


class CapabilityProfile(BaseModel):
    """One real combination of what a leaf can start.

    A leaf lists combinations rather than independent lists of what it offers: a Mac that
    runs macOS and arm64 Linux does not run x86-64 Windows, and a runtime that gives a
    container ``perf`` may attach no GPU.
    """

    model_config = _STRICT

    provider: str = Field(min_length=1)
    """The provider id of the leaf that starts it, such as ``docker:runc``."""
    kind: ImageKind
    os: OperatingSystem
    arch: Arch
    gui: bool
    """Whether a desktop can be shown. Whether a Sandbox has one is its image's business, so
    no need asks for it."""
    network_modes: frozenset[NetworkMode]
    snapshot: bool = False
    """Whether it saves snapshots and boots Sandboxes from them."""
    max_saves: int | None = Field(default=None, ge=1)
    """How many snapshots in a row it saves, of a Sandbox booted from an image and of each
    one booted from the snapshot before; ``None`` when it saves any number."""
    max_duration_sec: float | None = Field(default=None, gt=0)
    """How long it holds one Sandbox at most, as a scheduler holds an allocation for its wall
    time; ``None`` when it holds one as long as asked."""
    gpus: bool = False
    """Whether it attaches NVIDIA GPUs."""
    gpu_models: tuple[GpuModel, ...] = ()
    """The models of the GPUs it attaches; empty when it learns them only as it attaches them."""
    gpu_hold: GpuHold = GpuHold.WHOLE
    """How a Sandbox that names ``gpu_memory_mb`` holds its GPUs: shared with others, limited
    or watched, where the leaf shares them; whole where it does not. A Sandbox that names none
    always holds them whole."""
    max_cpus: int | None = Field(default=None, ge=1)
    max_memory_mb: int | None = Field(default=None, ge=1)
    max_nodes: int = Field(default=1, ge=1)
    """How many machines one Sandbox may span, each of ``max_cpus`` and ``max_memory_mb``."""
    storage_limit: bool = False
    """Whether it holds a Sandbox that names ``storage_mb`` to it, rather than let it fill the
    Host's disk: by writable storage of that size at least, past which a write fails as on a
    full disk, or by measuring what the Sandbox writes and ending it past the limit."""
    container_features: frozenset[ContainerFeature] = frozenset()
    """What it gives a container Sandbox that asks for it."""
    volumes: bool = False
    """Whether it mounts a Sandbox's volume candidates that are directories in its image as
    volumes of their own, which its snapshots keep, and mounts views of a snapshot's volumes
    in a Sandbox of another image that asks for them. One that does not leaves them on the
    Sandbox's disk, from which they are copied."""


class SandboxNeeds(BaseModel):
    """What one Sandbox needs of the leaf that starts it, from its request or Fleet
    demand."""

    model_config = _STRICT

    kind: ImageKind
    os: OperatingSystem
    arch: Arch
    network_mode: NetworkMode
    resources: Resources
    snapshot: bool = False
    """Whether it saves a snapshot at its end or boots from one."""
    saves: int = Field(default=0, ge=0)
    """How many snapshots in a row its episode may save, of it and of each Sandbox booted from
    the snapshot before, as :func:`solver_saves` counts them."""
    verifier: SandboxNeeds | None = None
    """What the verifier that boots from its snapshot needs: a snapshot verifier, or a shared
    validation round's. Only the leaf that saved a snapshot boots from it, so one profile
    serves both."""
    duration_sec: float | None = Field(default=None, gt=0)
    """How long the leaf may have to hold one Sandbox for it at once: a verifier's limit, a
    shared verifier's in the solver it runs in, or the longest of the verifiers that boot from
    a solver's snapshot, on the leaf that saved it."""


def solver_saves(validations: int, *, snapshot_verified: bool, kept_as_snapshot: bool) -> int:
    """How many snapshots in a row a solver's episode saves, of it and of each solver booted
    from the snapshot before: one for each of the ``validations`` still to come, and one at
    its end when it is saved for a snapshot verifier or kept as a snapshot, which is then the
    same save. A shared solver is never saved at its end for its verifier, which runs in it."""
    return validations + int(snapshot_verified or kept_as_snapshot)


def round_verifier_resources(spec: TaskSpec, validations: int = 1) -> Resources | None:
    """The resources of the verifier a validation round boots from a snapshot of a solver of
    ``spec``, on the leaf that saved it, with ``validations`` still to come: the solver's,
    never elevated, where rounds validate shared, and its own where they validate in a
    snapshot verifier. None otherwise: a separate one runs on a leaf of its own."""
    validation = spec.validation_spec
    if validations <= 0 or validation.environment_mode is VerificationMode.SEPARATE:
        return None
    if validation.environment_mode is VerificationMode.SHARED:
        return spec.resources.model_copy(update={"sudo": False})
    assert validation.resources is not None, "a snapshot verifier has resources"
    return validation.resources.as_resources()


#: Harbor's names whose words are not the model's, by the model they stand for: GKE
#: accelerator labels, which Harbor takes on GKE and Daytona, and its ``h100-mega``.
_ALIASES = {
    "nvidia-tesla-t4": "T4",
    "nvidia-tesla-a100": "A100 40GB",
    "h100-mega": "H100 80GB",
    "nvidia-h100-mega-80gb": "H100 80GB",
    "nvidia-h200-141gb": "H200",
}
_WORD = re.compile(r"[a-z0-9]+")


def accepts_model(gpu_types: tuple[str, ...], model: str) -> bool:
    """Whether a Task accepting ``gpu_types`` accepts the GPU nvidia-smi names ``model``.

    The rule is the Task authoring spec's: a name accepts a model whose words include all of
    its own, in order; no names accept every model.
    """
    words = _WORD.findall(model.lower())
    for gpu_type in gpu_types:
        name = gpu_type.strip().lower()
        remaining = iter(words)
        wanted = _WORD.findall(_ALIASES.get(name, name).lower())
        if wanted and all(word in remaining for word in wanted):
            return True
    return not gpu_types


def _listed(values: Iterable[str]) -> str:
    return ", ".join(sorted(values)) or "none"


def _holds(model: GpuModel, resources: Resources) -> bool:
    """Whether a GPU of ``model`` serves a Sandbox of ``resources``: of a model it accepts, with
    the memory it needs on it, unless the leaf does not know the model's memory."""
    return accepts_model(resources.gpu_types, model.name) and (
        resources.gpu_memory_mb is None
        or model.memory_mb is None
        or model.memory_mb >= resources.gpu_memory_mb
    )


def _model(model: GpuModel) -> str:
    return model.name if model.memory_mb is None else f"{model.name} ({model.memory_mb} MiB)"


def unmet(profile: CapabilityProfile, needs: SandboxNeeds) -> tuple[str, ...]:
    """Why ``profile`` does not serve ``needs``, one reason each; none when it does.

    Only what a profile declares is compared. What changes from moment to moment, such as
    the CPUs or GPUs free now, is for whoever starts or places the Sandbox to check.
    """
    resources = needs.resources
    reasons: list[str] = []
    if profile.kind is not needs.kind:
        reasons.append(f"starts {profile.kind} Sandboxes, not {needs.kind}")
    if profile.os is not needs.os:
        reasons.append(f"runs {profile.os}, not {needs.os}")
    if profile.arch is not needs.arch:
        reasons.append(f"runs {profile.arch}, not {needs.arch}")
    if needs.network_mode not in profile.network_modes:
        reasons.append(
            f"network {needs.network_mode} is not one it applies ({_listed(profile.network_modes)})"
        )
    if resources.nodes > profile.max_nodes:
        reasons.append(f"spans {profile.max_nodes} node(s) at most, not {resources.nodes}")
    if profile.max_cpus is not None and resources.cpus > profile.max_cpus:
        reasons.append(f"{resources.cpus} CPUs exceed its {profile.max_cpus}")
    if profile.max_memory_mb is not None and resources.memory_mb > profile.max_memory_mb:
        reasons.append(f"{resources.memory_mb} MB of memory exceed its {profile.max_memory_mb} MB")
    if resources.storage_mb is not None and not profile.storage_limit:
        reasons.append(f"limits no writable storage (storage_mb {resources.storage_mb})")
    if resources.gpus and not profile.gpus:
        reasons.append("attaches no GPUs")
    elif (
        resources.gpus
        and profile.gpu_models
        and not any(_holds(model, resources) for model in profile.gpu_models)
    ):
        wanted = [f"of gpu_types {list(resources.gpu_types)}"] if resources.gpu_types else []
        if resources.gpu_memory_mb is not None:
            wanted.append(f"with gpu_memory_mb {resources.gpu_memory_mb}")
        reasons.append(
            f"attaches {_listed(map(_model, profile.gpu_models))}, none {' '.join(wanted)}"
        )
    if missing := set(resources.container_features) - profile.container_features:
        reasons.append(
            f"gives no container features {_listed(missing)} "
            f"(gives {_listed(profile.container_features)})"
        )
    if needs.snapshot and not profile.snapshot:
        reasons.append("saves and boots no snapshots")
    elif profile.max_saves is not None and needs.saves > profile.max_saves:
        reasons.append(f"saves {needs.saves} snapshots in a row, more than its {profile.max_saves}")
    if (
        profile.max_duration_sec is not None
        and needs.duration_sec is not None
        and needs.duration_sec > profile.max_duration_sec
    ):
        reasons.append(
            f"holds a Sandbox for {profile.max_duration_sec:g} s at most, not the "
            f"{needs.duration_sec:g} s a verifier may take"
        )
    if needs.verifier is not None:
        reasons.extend(
            f"the verifier booted from its snapshot: {why}"
            for why in unmet(profile, needs.verifier)
        )
    return tuple(reasons)


def closest(profiles: Iterable[CapabilityProfile], needs: SandboxNeeds) -> tuple[str, ...]:
    """Why the profile that misses the fewest of ``needs`` misses them; none when one of
    ``profiles`` serves them all."""
    return min(
        (unmet(profile, needs) for profile in profiles), key=len, default=("offers no profile",)
    )
