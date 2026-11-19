"""Volumes of Docker Sandboxes.

A Sandbox's volumes are Docker volumes of their own: each of its volume candidates that is a
directory in its image, which Docker fills with what the image holds there as the container
first mounts it, and, in a Sandbox that runs a container engine, the engine's storage. A
snapshot keeps each as the top of the stack of layers it records for its path. A Sandbox the
snapshot serves mounts, of a candidate, an overlay view of that stack under an empty layer of
its own, so that what it writes reaches no other Sandbox, and saving it puts that layer on
top; of an engine's storage, a copy, as an overlay view hides the whiteouts of the engine's
own layers and refuses new ones. A stack about to grow past :data:`MAX_LAYERS` is copied into
one layer instead. A layer goes once no snapshot and no view stacks it. What a Sandbox writes
to its volumes is measured from outside it, by a helper that mounts them.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.run import HELPER_RUNTIME
from ale.run.process import run

__all__ = [
    "MAX_LAYERS",
    "Meter",
    "Mount",
    "Root",
    "Stack",
    "collect",
    "decode",
    "directories",
    "freeze",
    "mount",
    "remove",
]

#: On every volume made for a Sandbox: the Sandbox it was made for.
VOLUME_LABEL = "ale.volume"
#: On a view: the layers it stacks, which stay while it does.
LOWERS_LABEL = "ale.volume-lowers"
#: On a snapshot: each path's stack, topmost layer first, and its root's owner and mode.
LAYERS_LABEL = "ale.volumes"
#: On a container: its mounts, by which its volumes go with it.
MOUNTS_LABEL = "ale.volume-mounts"
#: The most layers a view stacks: the overlay's options, which name the mount point of each,
#: must fit in one page.
MAX_LAYERS = 60
#: Prints which of the directories it is given are directories, no link on their way.
_DIRECTORIES = (
    "import json,os,sys; print(json.dumps([p for p in sys.argv[1:] "
    "if os.path.realpath(p) == p and os.path.isdir(p)]))"
)
#: Copies one volume into another as a whole, its root's owner and mode included: reflinked
#: where the filesystem can, so that it takes no data's time.
_COPY = "cp -a --reflink=auto /s/. /d/ && chown --reference=/s /d && chmod --reference=/s /d"


@dataclass(frozen=True)
class Root:
    """The owner and mode of a volume's root, which an overlay takes from its own empty layer
    rather than from the stack below it."""

    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class Stack:
    """What a snapshot holds at one path."""

    layers: tuple[str, ...]
    root: Root
    copied: bool = False
    """An engine's storage, which a Sandbox the snapshot serves gets a copy of."""


@dataclass(frozen=True)
class Mount:
    """One volume of a Sandbox: a volume of its own, a copy of a snapshot's, or a view of a
    snapshot's stack."""

    path: str
    volume: str
    lowers: tuple[str, ...] = ()
    upper: str = ""
    work: str = ""
    root: Root | None = None
    """For a view, what its root must be."""
    copied: bool = False
    """An engine's storage, which a snapshot keeps whole for a copy."""
    restored: bool = False
    """It holds a snapshot's layer already, which Docker must not fill from the image."""

    @property
    def top(self) -> str:
        """The volume that holds what it writes."""
        return self.upper or self.volume

    @property
    def stack(self) -> tuple[str, ...]:
        """The layers a snapshot of it keeps, topmost first."""
        return (self.top, *self.lowers)

    @property
    def argument(self) -> str:
        """Its ``docker run --mount``: a view or a copy holds what the snapshot does, never
        what another image has at its path."""
        spec = f"type=volume,src={self.volume},dst={self.path}"
        return f"{spec},volume-nocopy" if self.lowers or self.restored else spec

    @property
    def scratch(self) -> tuple[str, ...]:
        """What only it uses, which goes with it whatever a snapshot keeps."""
        return (self.volume, self.work) if self.lowers else ()


def encode(value: object) -> str:
    """A label value: what a label holds is a string without the quotes and spaces of JSON."""
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True).encode()).decode()


def _decoded(label: str | None) -> object:
    return json.loads(base64.urlsafe_b64decode(label.encode())) if label else None


def decode(label: str | None) -> dict[str, Stack]:
    """A snapshot's stacks, by path, from its label."""
    value = _decoded(label) or {}
    assert isinstance(value, dict)
    return {
        path: Stack(tuple(entry["layers"]), Root(*entry["root"]), bool(entry.get("copied")))
        for path, entry in value.items()
    }


def _label(stacks: Mapping[str, Stack]) -> str:
    return encode(
        {
            path: {
                "layers": list(stack.layers),
                "root": [stack.root.uid, stack.root.gid, stack.root.mode],
                "copied": stack.copied,
            }
            for path, stack in stacks.items()
        }
    )


def mounts_label(mounts: Sequence[Mount]) -> str:
    return encode(
        [[m.path, m.volume, list(m.lowers), m.upper, m.work, m.copied, m.restored] for m in mounts]
    )


def from_label(label: str | None) -> tuple[Mount, ...]:
    """A container's mounts, from its label."""
    value = _decoded(label) or []
    assert isinstance(value, list)
    return tuple(
        Mount(path, volume, tuple(lowers), upper, work, None, copied, restored)
        for path, volume, lowers, upper, work, copied, restored in value
    )


async def _docker(*args: str, timeout: float = 120) -> str:
    code, out, err = await run("docker", *args, timeout=timeout)
    if code != 0:
        raise ProviderStartError(f"docker {args[0]} {args[1]}: {err.strip() or out.strip()}")
    return out.strip()


async def directories(image: str, python: str, paths: Sequence[str]) -> tuple[str, ...]:
    """Which of ``paths`` are directories in ``image``, no link on their way, by its own
    framework's ``python`` in a helper container of it that starts nothing else."""
    if not paths:
        return ()
    found = await _docker(
        "run", "--rm", *HELPER_RUNTIME, "--network", "none", "--entrypoint", python,
        image, "-I", "-c", _DIRECTORIES, *paths,
    )  # fmt: skip
    listed = json.loads(found.splitlines()[-1])
    return tuple(path for path in paths if path in listed)


async def _create(owner: str, created: list[str], *options: str) -> str:
    name = f"ale-v-{uuid.uuid4().hex[:12]}"
    await _docker(
        "volume", "create", "--label", "ale.managed=true", "--label", f"{VOLUME_LABEL}={owner}",
        *options, name,
    )  # fmt: skip
    created.append(name)
    return name


async def _copy(source: str, target: str, helper: str) -> None:
    await _docker(
        "run", "--rm", *HELPER_RUNTIME, "--network", "none", "-v", f"{source}:/s:ro",
        "-v", f"{target}:/d", "--entrypoint", "sh", helper, "-c", _COPY,
        timeout=3600,
    )  # fmt: skip


async def mount(
    owner: str,
    *,
    fresh: Sequence[str] = (),
    engines: Sequence[str] = (),
    stacks: Mapping[str, Stack] | None = None,
    helper: str,
) -> tuple[Mount, ...]:
    """A Sandbox's volumes: of its own at the candidates ``fresh`` and the engine paths
    ``engines``, or, from a snapshot's ``stacks``, a view of each candidate's and a copy of
    each engine's, an engine path the snapshot lacks being of its own too."""
    created: list[str] = []
    mounts: list[Mount] = []
    try:
        for path, stack in (stacks or {}).items():
            if stack.copied:
                if path not in engines:
                    continue
                volume = await _create(owner, created)
                await _copy(stack.layers[0], volume, helper)
                mounts.append(Mount(path, volume, copied=True, restored=True))
                continue
            if len(stack.layers) >= MAX_LAYERS:
                raise ProviderCapabilityError(
                    f"the volume at {path} stacks {len(stack.layers)} layers, "
                    "more than a view takes"
                )
            upper = await _create(owner, created)
            work = await _create(owner, created)
            points = [await _mountpoint(volume) for volume in (*stack.layers, upper, work)]
            options = f"lowerdir={':'.join(points[:-2])},upperdir={points[-2]},workdir={points[-1]}"
            view = await _create(
                owner, created,
                "--driver", "local", "--opt", "type=overlay", "--opt", "device=overlay",
                "--opt", f"o={options}", "--label", f"{LOWERS_LABEL}={encode(list(stack.layers))}",
            )  # fmt: skip
            mounts.append(Mount(path, view, stack.layers, upper, work, stack.root))
        mounted = {m.path for m in mounts}
        for path in fresh if stacks is None else ():
            mounts.append(Mount(path, await _create(owner, created)))
        for path in engines:
            if path not in mounted:
                mounts.append(Mount(path, await _create(owner, created), copied=True))
    except BaseException:
        await _remove(reversed(created))
        raise
    return tuple(mounts)


async def freeze(
    owner: str, mounts: Sequence[Mount], roots: Mapping[str, Root], helper: str
) -> tuple[str, frozenset[str]]:
    """The label of a snapshot of a stopped Sandbox's ``mounts``, and the volumes it keeps:
    each mount's top layer on its stack, or, for a stack that would pass
    :data:`MAX_LAYERS`, a copy of what its view holds as one layer."""
    created: list[str] = []
    stacks: dict[str, Stack] = {}
    try:
        for m in mounts:
            if m.copied:
                stacks[m.path] = Stack((m.top,), roots[m.path], copied=True)
            elif len(m.stack) >= MAX_LAYERS:
                merged = await _create(owner, created)
                await _copy(m.volume, merged, helper)
                stacks[m.path] = Stack((merged,), roots[m.path])
            else:
                stacks[m.path] = Stack(m.stack, roots[m.path])
    except BaseException:
        await _remove(reversed(created))
        raise
    return _label(stacks), frozenset(stack.layers[0] for stack in stacks.values())


async def _mountpoint(volume: str) -> str:
    return await _docker("volume", "inspect", "--format", "{{.Mountpoint}}", volume)


async def _remove(volumes: Iterable[str]) -> list[str]:
    """Remove each of ``volumes`` that exists; return those Docker would not remove."""
    left = []
    for volume in volumes:
        code, _, err = await run("docker", "volume", "rm", volume, timeout=120)
        if code != 0 and "no such volume" not in err.lower():
            left.append(f"{volume}: {err.strip()}")
    return left


async def remove(mounts: Sequence[Mount], *, kept: frozenset[str] = frozenset()) -> None:
    """Remove a Sandbox's volumes once its container is gone: all but the layers a snapshot
    of it ``kept``."""
    left = await _remove(
        [
            *(volume for m in mounts for volume in m.scratch),
            *(m.top for m in mounts if m.top not in kept),
        ]
    )
    if left:
        raise ProviderStartError(f"could not remove volumes: {'; '.join(left)}")


async def _inspected(kind: str, names: Sequence[str]) -> list[dict[str, object]]:
    """What ``docker <kind> inspect`` finds of ``names``, less those another process removed
    since they were listed."""
    if not names:
        return []
    code, out, err = await run("docker", kind, "inspect", *names, timeout=120)
    errors = [line for line in err.splitlines() if line.strip()]
    if code != 0 and not all("no such" in line.lower() for line in errors):
        raise ProviderStartError(f"docker {kind} inspect: {err.strip()}")
    found = json.loads(out) if out.strip() else []
    assert isinstance(found, list)
    return found


async def collect(layers: Iterable[str]) -> None:
    """Remove each of ``layers`` that no snapshot and no view stacks any more."""
    used: set[str] = set()
    images = await _docker("image", "ls", "-q", "--no-trunc", "--filter", f"label={LAYERS_LABEL}")
    for image in await _inspected("image", list(dict.fromkeys(images.split()))):
        labels = (image.get("Config") or {}).get("Labels") or {}  # type: ignore[union-attr]
        used.update(
            layer for stack in decode(labels.get(LAYERS_LABEL)).values() for layer in stack.layers
        )
    views = await _docker("volume", "ls", "-q", "--filter", f"label={LOWERS_LABEL}")
    for view in await _inspected("volume", views.split()):
        lowers = _decoded((view.get("Labels") or {}).get(LOWERS_LABEL)) or []  # type: ignore[union-attr]
        assert isinstance(lowers, list)
        used.update(lowers)
    left = await _remove(layer for layer in dict.fromkeys(layers) if layer not in used)
    if left:
        raise ProviderStartError(f"could not remove volume layers: {'; '.join(left)}")


class Meter:
    """Measures what a Sandbox has written to its volumes, from outside it: a helper that
    mounts the volume holding each one's writes read-only, whose blocks it counts as Docker
    counts a writable layer's, less what they held as it started, which it records itself, so that
    a run that drives the Sandbox again measures alike."""

    def __init__(self, owner: str) -> None:
        self.name = f"ale-meter-{owner}"

    @classmethod
    async def start(cls, owner: str, mounts: Sequence[Mount], helper: str) -> Meter:
        meter = cls(owner)
        binds = [f"-v={m.top}:/m/{index}:ro" for index, m in enumerate(mounts)]
        await _docker(
            "run", "-d", "--rm", "--name", meter.name, *HELPER_RUNTIME, "--network", "none",
            "--label", "ale.managed=true", "--label", f"{VOLUME_LABEL}={owner}",
            *binds, "--entrypoint", "sh", helper, "-c",
            "du -sB1 /m | cut -f1 > /baseline.tmp && mv /baseline.tmp /baseline "
            "&& exec sleep infinity",
        )  # fmt: skip
        try:
            await meter.written()
        except BaseException:
            await meter.stop()
            raise
        return meter

    async def written(self) -> int:
        """The bytes the Sandbox has written to its volumes since it started."""
        out = await _docker(
            "exec", self.name, "sh", "-c",
            "until test -f /baseline; do sleep 0.1; done; cat /baseline; du -sB1 /m | cut -f1",
            timeout=600,
        )  # fmt: skip
        baseline, total = (int(line) for line in out.split())
        return max(0, total - baseline)

    async def stop(self) -> None:
        """Remove it, if it still runs."""
        code, _, err = await run("docker", "rm", "-f", self.name, timeout=120)
        if code != 0 and "no such container" not in err.lower():
            raise ProviderStartError(f"could not remove {self.name}: {err.strip()}")
