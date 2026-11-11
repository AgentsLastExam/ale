"""Where a Sandbox goes: filter the Nodes that can take it now, then score them.

Pure functions over a snapshot of the ledger, so the rules can be read and tested apart
from the server. The score is the estimated time until the Sandbox is ready. A Node that
lacks the image prepares it first, which takes minutes where creating a Sandbox takes
seconds, so a Node that already holds the image is usually the faster one, and each Task's
episodes stay on the few Nodes that prepared its image.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from ale.run.fleet.protocol import NodeDeclaration, SandboxDemand, SandboxProfile
from ale.run.gpu import accepts_model

__all__ = ["Choice", "Holds", "NodeView", "Weights", "choose", "unservable"]


class Holds(BaseModel):
    """What one placed Sandbox takes from its Node until the Node confirms it gone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpus: int = Field(ge=0)
    memory_mb: int = Field(ge=0)
    macos_vm_slots: int = Field(default=0, ge=0)
    gpus: dict[str, int] = Field(default_factory=dict)
    image: str = ""
    """The key of the image it boots, which its Node holds or is about to."""


@dataclass(frozen=True)
class NodeView:
    """One Node as placement sees it."""

    declaration: NodeDeclaration
    online: bool = True
    draining: bool = False
    images: frozenset[str] = frozenset()
    """The keys of the images the Node prepared and keeps."""
    holds: tuple[Holds, ...] = ()
    startup_sec: dict[str, float] = field(default_factory=dict)
    """Measured seconds from create to ready, per Provider, when the ledger has any."""

    @property
    def name(self) -> str:
        return self.declaration.name

    def free(self) -> tuple[int, int, int, dict[str, int]]:
        resources = self.declaration.resources
        gpus: dict[str, int] = {}
        for pool in resources.gpus:
            gpus[pool.model] = gpus.get(pool.model, 0) + pool.count
        cpus, memory, slots = resources.cpus, resources.memory_mb, resources.macos_vm_slots
        for held in self.holds:
            cpus -= held.cpus
            memory -= held.memory_mb
            slots -= held.macos_vm_slots
            for model, count in held.gpus.items():
                gpus[model] = gpus.get(model, 0) - count
        return cpus, memory, slots, gpus

    def will_hold(self, image: str) -> bool:
        return image in self.images or any(held.image == image for held in self.holds)


@dataclass(frozen=True)
class Weights:
    prepare_sec: float = 300.0
    """Preparing an image on a Node that lacks it, assumed while no Node has reported doing
    so; container Tasks take about five minutes to build, and so does a Windows Task's layer."""
    startup_sec: float = 30.0
    """Create-to-ready time assumed for a Provider the ledger has not measured on a Node."""


@dataclass(frozen=True)
class Choice:
    node: str
    profile: SandboxProfile
    holds: Holds
    score: float


def _profile(declaration: NodeDeclaration, demand: SandboxDemand) -> SandboxProfile | None:
    if declaration.framework != demand.framework:
        return None
    resources = demand.resources
    for profile in declaration.profiles:
        if (
            profile.kind is demand.kind
            and profile.os is demand.os
            and profile.arch is demand.arch
            and demand.network_mode in profile.network_modes
            and (profile.gpus or not resources.gpus)
            and (profile.max_cpus is None or resources.cpus <= profile.max_cpus)
            and (profile.max_memory_mb is None or resources.memory_mb <= profile.max_memory_mb)
        ):
            return profile
    return None


def _gpus(available: dict[str, int], demand: SandboxDemand) -> dict[str, int] | None:
    """Which of a Node's free GPUs the demand would take, or None when too few fit it."""
    wanted = demand.resources.gpus
    taken: dict[str, int] = {}
    for model, free in available.items():
        if wanted == 0:
            break
        if free > 0 and accepts_model(demand.resources.gpu_types, model):
            count = min(free, wanted)
            taken[model] = count
            wanted -= count
    return taken if wanted == 0 else None


def _shortage(node: NodeView, demand: SandboxDemand, profile: SandboxProfile) -> str | None:
    """Why the Node cannot take the demand now, or None when it can."""
    cpus, memory, slots, gpus = node.free()
    resources = demand.resources
    if resources.cpus > cpus:
        return f"{node.name} has {max(cpus, 0)} free CPUs"
    if resources.memory_mb > memory:
        return f"{node.name} has {max(memory, 0)} MB free memory"
    if profile.macos_vm_slots > slots:
        return f"{node.name} has no free macOS VM slot"
    if resources.gpus and _gpus(gpus, demand) is None:
        return f"{node.name} has too few free GPUs of an accepted model"
    return None


def _rendezvous(image: str, node: str) -> int:
    """A stable per-image order of Nodes, so ties keep each image on the same few."""
    return int.from_bytes(hashlib.sha256(f"{image}\0{node}".encode()).digest()[:8], "big")


def choose(
    demand: SandboxDemand,
    nodes: Iterable[NodeView],
    *,
    refused: Sequence[str] = (),
    assigned: Sequence[str] = (),
    prepare_sec: Mapping[str, float] | None = None,
    weights: Weights = Weights(),
) -> tuple[Choice | None, str]:
    """The best Node that can take ``demand`` now, or None and why none can.

    ``prepare_sec`` is how long Nodes took to prepare each image, by key, which a Node that
    lacks the image is expected to take too. ``assigned`` are the Nodes the Run's plan chose
    for this image: the plan has them prepare it, so their score counts no preparation.
    """
    best: tuple[float, int, Choice] | None = None
    reasons: list[str] = []
    for node in nodes:
        if not node.online or node.draining or node.name in refused:
            continue
        profile = _profile(node.declaration, demand)
        if profile is None:
            continue
        if (why := _shortage(node, demand, profile)) is not None:
            reasons.append(why)
            continue
        image = demand.image
        preparing = (
            0.0
            if node.will_hold(image) or node.name in assigned
            else (prepare_sec or {}).get(image, weights.prepare_sec)
        )
        score = preparing + node.startup_sec.get(profile.provider, weights.startup_sec)
        _, _, _, free_gpus = node.free()
        holds = Holds(
            cpus=demand.resources.cpus,
            memory_mb=demand.resources.memory_mb,
            macos_vm_slots=profile.macos_vm_slots,
            gpus=(_gpus(free_gpus, demand) or {}) if demand.resources.gpus else {},
            image=image,
        )
        key = (score, _rendezvous(image, node.name))
        if best is None or key < best[:2]:
            best = (*key, Choice(node.name, profile, holds, score))
    if best is not None:
        return best[2], ""
    if reasons:
        return None, "waiting for capacity: " + "; ".join(sorted(reasons))
    return None, "waiting for a Node that can serve it to come online or stop draining"


def unservable(demand: SandboxDemand, nodes: Iterable[NodeView]) -> str | None:
    """Why no registered Node could ever serve ``demand``, or None when one could.

    Offline and draining Nodes count: they may return. A demand larger than every
    matching Node's whole capacity fails now rather than waiting forever.
    """
    nodes = list(nodes)
    matching = [
        (node, profile)
        for node in nodes
        if (profile := _profile(node.declaration, demand)) is not None
    ]
    resources = demand.resources
    if not matching:
        elsewhere = [
            node.name
            for node in nodes
            if _profile(node.declaration.model_copy(update={"framework": demand.framework}), demand)
            is not None
        ]
        detail = (
            f" with this engine's framework; {', '.join(elsewhere)} run another engine's "
            "framework, so update their node agents or this Run's engine"
            if elsewhere
            else ""
        )
        return (
            f"no Node offers a {demand.kind} {demand.os}/{demand.arch} Sandbox with "
            f"network {demand.network_mode}"
            + (f" and {resources.gpus} GPUs" if resources.gpus else "")
            + detail
        )
    for node, profile in matching:
        total = node.declaration.resources
        gpus = {pool.model: pool.count for pool in total.gpus}
        if (
            resources.cpus <= total.cpus
            and resources.memory_mb <= total.memory_mb
            and profile.macos_vm_slots <= total.macos_vm_slots
            and (not resources.gpus or _gpus(gpus, demand) is not None)
        ):
            return None
    return (
        f"no Node that offers a {demand.kind} {demand.os}/{demand.arch} Sandbox has "
        f"{resources.cpus} CPUs, {resources.memory_mb} MB"
        + (f" and {resources.gpus} accepted GPUs" if resources.gpus else "")
        + " in all"
    )
