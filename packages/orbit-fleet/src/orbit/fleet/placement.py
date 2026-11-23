"""Where a Sandbox goes: score every Node that could serve it, and take the lowest score.

Pure functions over a snapshot of the ledger, so the rules can be read and tested apart
from the server. The score is the estimated time until the Sandbox is ready:

* a free Node that holds the image starts it;
* a free Node that lacks it prepares the image first, which takes minutes where creating a
  Sandbox takes seconds, and pays for the copy it adds to its disk;
* a full Node that holds the image, or is assigned it, waits for room, as its Sandboxes end
  when the measured lifetimes of earlier Sandboxes of the same image and role suggest.

So each Task's episodes stay on the few Nodes that prepared its image while those free up
soon, and spread to another Node when waiting would take longer than building there.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from orbit.core.capability import CapabilityProfile, GpuHold, accepts_model, closest, unmet
from orbit.core.fleet import NodeDeclaration, SandboxDemand
from orbit.core.taskspec import ImageKind, OperatingSystem, Resources

__all__ = [
    "Busy",
    "Choice",
    "Holds",
    "NodeView",
    "Waiting",
    "Weights",
    "assign",
    "choose",
    "remaining_sec",
    "unservable",
]


class Holds(BaseModel):
    """What one placed Sandbox takes from its Node until the Node confirms it gone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpus: int = Field(ge=0)
    memory_mb: int = Field(ge=0)
    macos_vm_slots: int = Field(default=0, ge=0)
    gpus: dict[str, int] = Field(default_factory=dict)
    """The whole GPUs it holds, by model; none for one that shares its GPUs, which its Node's
    leaf admits by the memory free on them."""
    image: str = ""
    """The key of the image it boots, which its Node holds or is about to."""


@dataclass(frozen=True)
class Busy:
    """What one Sandbox on a Node holds, and in how many seconds it is expected to free it:
    infinite for a retained Sandbox, or when nothing measured says."""

    holds: Holds
    frees_in_sec: float


#: A request already waiting for a Node: from when, until when, and what it expects to hold.
Waiting = tuple[float, float, Holds]


def remaining_sec(lifetimes: Sequence[float], age: float) -> float:
    """The expected rest of a Sandbox's life at ``age`` seconds, from the measured lifetimes
    of earlier Sandboxes of its image and role: the mean of what those that lived longer had
    left. One older than all of them may run for any time, so it counts as never ending."""
    longer = [lifetime - age for lifetime in lifetimes if lifetime > age]
    return sum(longer) / len(longer) if longer else math.inf


@dataclass(frozen=True)
class NodeView:
    """One Node as placement sees it."""

    declaration: NodeDeclaration
    online: bool = True
    draining: bool = False
    images: frozenset[str] = frozenset()
    """The keys of the images the Node prepared and keeps."""
    snapshots: Mapping[str, str] = field(default_factory=dict)
    """The snapshots the Node keeps, which only it can boot from, each with the provider id
    of the leaf that saved it, which alone boots from it."""
    holds: tuple[Holds, ...] = ()
    busy: tuple[Busy, ...] = ()
    """When each of ``holds``, in order, is expected to be freed; one past their end never is."""
    startup_sec: dict[str, float] = field(default_factory=dict)
    """Measured seconds from create to ready, per Provider, when the ledger has any."""

    @property
    def name(self) -> str:
        return self.declaration.name

    def free(self) -> tuple[int, int, int, dict[str, int]]:
        return _free(self.declaration, self.holds)

    def will_hold(self, image: str) -> bool:
        return image in self.images or any(held.image == image for held in self.holds)


@dataclass(frozen=True)
class Weights:
    prepare_sec: float = 300.0
    """Preparing an image on a Node that lacks it, assumed while no Node has reported doing
    so; container Tasks take about five minutes to build, and so does a Windows Task's layer."""
    startup_sec: float = 30.0
    """Create-to-ready time assumed for a Provider the ledger has not measured on a Node."""
    copy_sec_per_gb: float = 10.0
    """The wait one more GB of copies is worth. A copy stays on its Node's disk, and at 10 s
    per GB a Task image of a few GB is worth waiting about a minute for: enough to settle
    close calls, too little to queue behind a long episode."""


@dataclass(frozen=True)
class Choice:
    node: str
    profile: CapabilityProfile
    holds: Holds
    score: float
    ready_sec: float
    """The expected seconds to a ready Sandbox, which the score adds the copy's cost to."""
    wait_sec: float | None = None
    """None when the Node can take it now. Otherwise the Node is full and the request is to
    wait for it, about this long: 0.0 when what it waits for is being freed already."""


def _free(
    declaration: NodeDeclaration, holds: Iterable[Holds]
) -> tuple[int, int, int, dict[str, int]]:
    resources = declaration.resources
    gpus: dict[str, int] = {}
    for pool in resources.gpus:
        gpus[pool.model] = gpus.get(pool.model, 0) + pool.count
    cpus, memory, slots = resources.cpus, resources.memory_mb, resources.macos_vm_slots
    for held in holds:
        cpus -= held.cpus
        memory -= held.memory_mb
        slots -= held.macos_vm_slots
        for model, count in held.gpus.items():
            gpus[model] = gpus.get(model, 0) - count
    return cpus, memory, slots, gpus


def _profile(
    declaration: NodeDeclaration, demand: SandboxDemand, *, leaf: str | None = None
) -> CapabilityProfile | None:
    """The Node's first profile, in the order it prefers its leaves, that serves
    ``demand``; with ``leaf``, the first of that leaf's."""
    if declaration.framework != demand.framework:
        return None
    needs = demand.needs
    return next(
        (
            p
            for p in declaration.profiles
            if (leaf is None or p.provider == leaf) and not unmet(p, needs)
        ),
        None,
    )


def _slots(profile: CapabilityProfile) -> int:
    """The Node's macOS VM slots one Sandbox of ``profile`` holds."""
    return int(profile.kind is ImageKind.VM and profile.os is OperatingSystem.MACOS)


def _whole(demand: SandboxDemand, profile: CapabilityProfile) -> bool:
    """Whether a Sandbox of ``demand`` holds its GPUs whole on a leaf of ``profile``. One that
    shares them takes none whole: the Node's leaf admits it by the memory free on them, and
    its refusal there is the Node refusing the grant for now."""
    return demand.resources.gpu_memory_mb is None or profile.gpu_hold is GpuHold.WHOLE


def _gpus(available: dict[str, int], resources: Resources) -> dict[str, int] | None:
    """Which of a Node's GPUs that no Sandbox holds whole a Sandbox of ``resources`` would
    take, or None when too few fit it."""
    wanted = resources.gpus
    taken: dict[str, int] = {}
    for model, free in available.items():
        if wanted == 0:
            break
        if free > 0 and accepts_model(resources.gpu_types, model):
            count = min(free, wanted)
            taken[model] = count
            wanted -= count
    return taken if wanted == 0 else None


def _shortage(
    name: str,
    free: tuple[int, int, int, dict[str, int]],
    demand: SandboxDemand,
    profile: CapabilityProfile,
) -> str | None:
    """Why a Node with ``free`` cannot take the demand, or None when it can."""
    cpus, memory, slots, gpus = free
    resources = demand.resources
    if resources.cpus > cpus:
        return f"{name} has {max(cpus, 0)} free CPUs"
    if resources.memory_mb > memory:
        return f"{name} has {max(memory, 0)} MB free memory"
    if _slots(profile) > slots:
        return f"{name} has no free macOS VM slot"
    if resources.gpus and _gpus(gpus, resources) is None:
        return f"{name} has too few free GPUs of an accepted model"
    return None


def short_in_all(
    declaration: NodeDeclaration, resources: Resources, profile: CapabilityProfile
) -> str | None:
    """Why a Node never holds a Sandbox of ``resources`` on a leaf of ``profile``, even when
    it holds nothing else, or None when it does."""
    total = declaration.resources
    name = declaration.name
    if resources.cpus > total.cpus:
        return f"{name} has {total.cpus} CPUs in all, and it asks for {resources.cpus}"
    if resources.memory_mb > total.memory_mb:
        return (
            f"{name} has {total.memory_mb} MB of memory in all, and it asks for "
            f"{resources.memory_mb} MB"
        )
    if _slots(profile) > total.macos_vm_slots:
        return f"{name} has no macOS VM slot"
    gpus = {pool.model: pool.count for pool in total.gpus}
    if resources.gpus and _gpus(gpus, resources) is None:
        return f"{name} has fewer than {resources.gpus} GPUs of an accepted model in all"
    return None


def _wait(
    node: NodeView, demand: SandboxDemand, profile: CapabilityProfile, ahead: Sequence[Waiting]
) -> tuple[float, tuple[int, int, int, dict[str, int]]] | None:
    """In how many seconds a full Node is expected to have room for ``demand``, after the
    requests already waiting for it, and what is free then; None when no estimate says it
    ever does."""
    spans: list[Waiting] = [(0.0, busy.frees_in_sec, busy.holds) for busy in node.busy]
    spans.extend(ahead)
    # A hold the estimate knows nothing about is taken to stay.
    spans.extend((0.0, math.inf, held) for held in node.holds[len(node.busy) :])
    for moment in sorted({0.0, *(end for _, end, _ in spans if math.isfinite(end))}):
        held = [holds for start, end, holds in spans if start <= moment < end]
        free = _free(node.declaration, held)
        if _shortage(node.name, free, demand, profile) is None:
            return moment, free
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
    image_bytes: Mapping[str, int] | None = None,
    ahead: Mapping[str, Sequence[Waiting]] | None = None,
    weights: Weights = Weights(),
) -> tuple[Choice | None, str]:
    """The Node with the lowest score for ``demand``, or None and why none can take it.

    A Node's score is the expected seconds to a ready Sandbox there, plus the cost of the copy
    of the image it adds, if any.

    ``prepare_sec`` is how long Nodes took to prepare each image, by key, which a Node that
    lacks the image is expected to take too, and ``image_bytes`` the size of each copy.
    ``assigned`` are the Nodes the Run's plan chose for this image: the plan has them prepare
    it, so their score counts neither preparation nor copy, and they are worth waiting for as
    the Nodes holding it are. ``ahead`` are the requests already waiting for each Node.
    A choice whose ``wait_sec`` is not None is a full Node the request is to wait for.
    """
    best: tuple[float, int, Choice] | None = None
    reasons: list[str] = []
    for node in nodes:
        if not node.online or node.draining or node.name in refused:
            continue
        saved = demand.snapshot or demand.volumes_from
        if saved is not None and saved not in node.snapshots:
            continue
        # A Sandbox booted from a snapshot, or mounting views of its volumes, may need other
        # resources than the one saved, which the Node's first leaf for them need not be; it
        # starts on the leaf that saved it.
        leaf = node.snapshots[saved] if saved is not None else None
        profile = _profile(node.declaration, demand, leaf=leaf)
        if profile is None:
            continue
        if demand.verifier is not None and (
            why := short_in_all(node.declaration, demand.verifier, profile)
        ):
            # Its snapshot verifier boots there too, once it is saved.
            reasons.append(f"its snapshot verifier would not fit: {why}")
            continue
        image = demand.image
        holder = node.will_hold(image) or node.name in assigned
        startup = node.startup_sec.get(profile.provider, weights.startup_sec)
        free = node.free()
        wait: float | None = None
        copy = 0.0
        if (why := _shortage(node.name, free, demand, profile)) is not None:
            room = (
                _wait(node, demand, profile, (ahead or {}).get(node.name, ())) if holder else None
            )
            if room is None:
                reasons.append(why)
                continue
            # It takes its room from what is free then: those GPUs, not the ones free now.
            wait, free = room
            ready = wait + startup
        elif holder:
            ready = startup
        else:
            ready = (prepare_sec or {}).get(image, weights.prepare_sec) + startup
            copy = weights.copy_sec_per_gb * (image_bytes or {}).get(image, 0) / 1e9
        score = ready + copy
        _, _, _, free_gpus = free
        whole = demand.resources.gpus and _whole(demand, profile)
        holds = Holds(
            cpus=demand.resources.cpus,
            memory_mb=demand.resources.memory_mb,
            macos_vm_slots=_slots(profile),
            gpus=(_gpus(free_gpus, demand.resources) or {}) if whole else {},
            image=image,
        )
        key = (score, _rendezvous(image, node.name))
        if best is None or key < best[:2]:
            best = (*key, Choice(node.name, profile, holds, score, ready, wait))
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
    sandbox = f"{demand.kind} {demand.os}/{demand.arch} Sandbox"
    if not matching:
        elsewhere = [
            node.name
            for node in nodes
            if _profile(node.declaration.model_copy(update={"framework": demand.framework}), demand)
            is not None
        ]
        if elsewhere:
            return (
                f"no Node with this engine's framework serves this {sandbox}; "
                f"{', '.join(elsewhere)} run another engine's framework, so update their node "
                "agents or this Run's engine"
            )
        offered = [
            profile
            for node in nodes
            if node.declaration.framework == demand.framework
            for profile in node.declaration.profiles
        ]
        # In the words a Run's own selection uses for it.
        return f"no Node serves this {sandbox}: " + "; ".join(closest(offered, demand.needs))
    for node, profile in matching:
        if short_in_all(node.declaration, resources, profile) is None and (
            demand.verifier is None
            or short_in_all(node.declaration, demand.verifier, profile) is None
        ):
            return None
    verifier = demand.verifier
    return (
        f"no Node that offers a {sandbox} has {resources.cpus} CPUs, {resources.memory_mb} MB"
        + (f" and {resources.gpus} accepted GPUs" if resources.gpus else "")
        + " in all"
        + (
            ""
            if verifier is None
            else f", and room for its snapshot verifier's {verifier.cpus} CPUs, "
            f"{verifier.memory_mb} MB"
            + (f" and {verifier.gpus} accepted GPUs" if verifier.gpus else "")
        )
    )


def _capacity(node: NodeView, demand: SandboxDemand, profile: CapabilityProfile) -> int:
    """How many Sandboxes of ``demand`` the Node holds when it holds nothing else."""
    total = node.declaration.resources
    limits = [
        total.cpus // max(demand.resources.cpus, 1),
        total.memory_mb // max(demand.resources.memory_mb, 1),
    ]
    if _slots(profile):
        limits.append(total.macos_vm_slots // _slots(profile))
    if demand.resources.gpus:
        accepted = sum(
            pool.count
            for pool in total.gpus
            if accepts_model(demand.resources.gpu_types, pool.model)
        )
        # Sandboxes that share their GPUs are limited by the memory free on them, which only
        # the Node's leaf sees, not by their count.
        if _whole(demand, profile) or accepted < demand.resources.gpus:
            limits.append(accepted // demand.resources.gpus)
    return max(min(limits), 0)


def assign(
    demands: Sequence[tuple[SandboxDemand, int]], nodes: Iterable[NodeView], *, concurrency: int
) -> dict[str, tuple[str, ...]]:
    """Which Nodes each image of a Run's plan goes to, from its demand and episode count.

    An image needs as many Nodes as hold the episodes that can run at once, no more:
    ten Windows Tasks of five episodes on three machines of eight VMs each need one machine
    per Task, ten copies, where placing each episode wherever is free soon spreads every
    Task to all three, thirty copies. Nodes that already hold the image come first, then
    each image's fixed order of Nodes.
    """
    live = [node for node in nodes if node.online and not node.draining]
    episodes: dict[str, int] = {}
    wanted: dict[str, SandboxDemand] = {}
    for demand, count in demands:
        episodes[demand.image] = episodes.get(demand.image, 0) + count
        wanted.setdefault(demand.image, demand)
    assignments: dict[str, tuple[str, ...]] = {}
    for image, demand in wanted.items():
        matching = [
            (node, profile)
            for node in live
            if (profile := _profile(node.declaration, demand)) is not None
            and _capacity(node, demand, profile) > 0
        ]
        if not matching:
            continue
        per_node = max(_capacity(node, demand, profile) for node, profile in matching)
        running = min(episodes[image], concurrency)
        replicas = min(len(matching), math.ceil(running / per_node))
        ordered = sorted(
            matching,
            key=lambda pair: (not pair[0].will_hold(image), _rendezvous(image, pair[0].name)),
        )
        assignments[image] = tuple(node.name for node, _ in ordered[:replicas])
    return assignments
