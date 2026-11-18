"""A Sandbox's Docker volumes, as a helper container of its daemon sees them.

A Sandbox that runs a container engine keeps the engine's storage on volumes of its own
(``gvisor.ENGINE_STORAGE``), which neither ``SizeRw`` nor ``docker commit`` sees. ALE runs
unprivileged: it cannot read a rootful daemon's volume directories, and under rootless
Docker the files of the engine's containers belong to sub-UIDs it cannot read. So a runc
helper with no network mounts them by name, never anything in the Sandbox, whose root could
hide what it wrote under a mount: one per watched Sandbox measures them, read-only, for the
storage watch (:class:`Meter`), and one copies them into a snapshot's own volumes, and those
into a Sandbox booted from it (:func:`copy`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence

from ale.core.errors import ProviderStartError
from ale.run import HELPER_RUNTIME, ale_image
from ale.run.process import run

__all__ = [
    "IMAGE",
    "PATH_LABEL",
    "SNAPSHOT_LABEL",
    "Meter",
    "copy",
    "counts_blocks",
    "gone",
    "meter_name",
    "mounted",
    "of",
    "remove",
    "save",
    "saved",
]

#: GNU coreutils' ``du`` and ``cp`` and GNU findutils' ``find``, which ALE's QEMU runner, the
#: image of the Provider's other helpers, has.
IMAGE = ale_image("ale-qemu-runner")
#: The labels of a snapshot's volume: the Sandbox it was taken of, and the directory of that
#: Sandbox it holds.
SNAPSHOT_LABEL = "ale.snapshot"
PATH_LABEL = "ale.path"
#: How long a helper may take to start, and Docker to remove one, on a loaded daemon, as any
#: container of the Provider's; and a copy, as a commit.
_START_SEC = 300.0
_REMOVE_SEC = 300.0
_COPY_SEC = 1800.0
#: How long one measurement may take once its helper runs, as ``SizeRw``'s.
_MEASURE_SEC = 60.0
#: Run by ``sh`` in the measuring helper with each volume's mount: answers each line on its
#: stdin with what they all take of the Host's disk, in bytes, in one walk, so that a hard link
#: counts once, as the daemon counts ``SizeRw``: the blocks of every entry under the containerd
#: image store, the bytes of every file but a directory under a classic graph driver. A file
#: removed during the walk is counted as gone.
_MEASURE = {
    True: (
        'exec 2>/dev/null; while read -r _; do du -sxc --block-size=1 -- "$@" '
        "| tail -n 1 | cut -f 1; done"
    ),
    False: (
        "exec 2>/dev/null; while read -r _; do find \"$@\" -xdev ! -type d -printf '%D:%i %s\\n' "
        "| awk '!seen[$1]++ { total += $2 } END { printf \"%.0f\\n\", total }'; done"
    ),
}
#: Run by ``sh`` in the copying helper, with the index of each pair: copies ``/from/<i>`` into
#: ``/to/<i>`` with all a container engine's storage holds: owners, modes (setuid among them),
#: timestamps, hard and symbolic links, the 0:0 character devices by which an overlay marks
#: what a layer deletes, the holes of sparse files, and every xattr, ``user.overlay.opaque``
#: among them, an xattr it cannot copy failing the copy.
_COPY = (
    'set -e; for i in "$@"; do cp -a --reflink=auto --preserve=xattr "/from/$i/." "/to/$i/"; done'
)


async def counts_blocks() -> bool:
    """Whether the daemon counts ``SizeRw`` in blocks: under its containerd image store, whose
    snapshotter counts the blocks of a layer's entries, its directories included; a classic
    graph driver counts the bytes of its files."""
    code, raw, stderr = await run(
        "docker", "info", "--format", "{{json .DriverStatus}}", timeout=30
    )
    if code != 0:
        raise ProviderStartError(f"could not read Docker's image store: {stderr.strip()}")
    return ["driver-type", "io.containerd.snapshotter.v1"] in (json.loads(raw) or [])


def of(mounts: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """The Docker volumes among a container's ``Mounts``, by the directory each is at."""
    return {
        str(mount["Destination"]): str(mount["Name"])
        for mount in mounts
        if mount.get("Type") == "volume"
    }


async def mounted(container: str) -> dict[str, str]:
    """The Docker volumes ``container`` mounts, by the directory each is at."""
    code, raw, stderr = await run(
        "docker", "inspect", "--format", "{{json .Mounts}}", container, timeout=30
    )
    if code != 0:
        raise ProviderStartError(f"could not read {container}'s volumes: {stderr.strip()}")
    return of(json.loads(raw) or [])


def meter_name(container: str) -> str:
    """The name of the helper that measures ``container``'s volumes."""
    return f"{container}-storage"


async def gone(container: str) -> None:
    """Remove a helper and wait until Docker has, which ``--rm`` may be doing at once: until
    then it holds the volumes it mounts, which Docker keeps when their Sandbox is removed with
    them."""
    code, _, stderr = await run("docker", "rm", "-f", container, timeout=_REMOVE_SEC)
    if code == 0 or "no such container" in stderr.lower():
        return
    if "already in progress" not in stderr:
        raise ProviderStartError(f"could not remove {container}: {stderr.strip()}")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _REMOVE_SEC
    while loop.time() < deadline:
        code, _, stderr = await run("docker", "container", "inspect", container, timeout=30)
        if code != 0 and "no such" in stderr.lower():
            return
        await asyncio.sleep(0.1)
    raise ProviderStartError(f"Docker did not remove {container} within {_REMOVE_SEC:g}s")


class Meter:
    """A helper that mounts a Sandbox's volumes read-only and measures them whenever asked.

    It runs from the first measurement until :meth:`close`, answering a line on its stdin
    with one, and ends when its stdin does, so with ALE's process; Docker then removes it.
    One measurement of an engine's storage of about 20,800 entries took 0.05 to 0.13 s, where
    a helper started for each took 0.25 to 0.40 s and 0.08 to 0.22 s of the daemons' CPU (the
    Providers README has the measures).
    """

    def __init__(self, container: str, volumes: Iterable[str], *, blocks: bool) -> None:
        #: The helper's container.
        self.name = meter_name(container)
        mounts: list[str] = []
        roots: list[str] = []
        for index, volume in enumerate(volumes):
            mounts += ["--mount", f"type=volume,src={volume},dst=/v/{index},readonly"]
            roots.append(f"/v/{index}")
        #: The ``docker`` command that starts it.
        self.argv = [
            "docker", "run", "-i", "--rm", "--name", self.name, *HELPER_RUNTIME,
            "--network", "none", *mounts, "--entrypoint", "sh", IMAGE,
            "-c", _MEASURE[blocks], "sh", *roots,
        ]  # fmt: skip
        self._process: asyncio.subprocess.Process | None = None

    async def measure(self) -> int:
        """What the volumes take of the Host's disk now, in bytes; the helper is started
        first if it does not run, and ended if it does not answer in time."""
        timeout = _MEASURE_SEC
        if self._process is None or self._process.returncode is not None:
            await self._end()
            self._process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            timeout = _START_SEC
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(b"\n")
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), timeout)
        except (TimeoutError, ConnectionError) as error:
            await self._end()
            raise ProviderStartError(
                f"{self.name} did not measure the volumes: {error!r}"
            ) from None
        if not line.strip().isdigit():
            await self._end()
            stderr = (await process.stderr.read()).decode() if process.stderr else ""
            raise ProviderStartError(f"{self.name} measured nothing: {stderr.strip() or line!r}")
        return int(line)

    async def close(self) -> None:
        """End the helper and wait until Docker has removed it."""
        await self._end()

    async def _end(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            await gone(self.name)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def copy(pairs: Sequence[tuple[str, str]]) -> None:
    """Copy each volume of ``pairs``, ``(source, target)``, into the other, all in one runc
    helper with no network, as root with Docker's default capabilities, which create every
    kind of entry an engine's storage holds there: gVisor stores no xattr outside the
    ``user`` namespace on a volume. The sources are mounted read-only. The helper is removed
    even when the copy is cancelled or times out."""
    helper = f"ale-copy-{uuid.uuid4().hex[:12]}"
    mounts: list[str] = []
    for index, (source, target) in enumerate(pairs):
        mounts += [
            "--mount", f"type=volume,src={source},dst=/from/{index},readonly",
            "--mount", f"type=volume,src={target},dst=/to/{index}",
        ]  # fmt: skip
    try:
        code, _, stderr = await run(
            "docker", "run", "--rm", "--name", helper, *HELPER_RUNTIME, "--network", "none",
            *mounts, "--entrypoint", "sh", IMAGE,
            "-c", _COPY, "sh", *(str(index) for index in range(len(pairs))),
            timeout=_COPY_SEC,
        )  # fmt: skip
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.shield(gone(helper))
    if code != 0:
        raise ProviderStartError(f"could not copy Docker volumes: {stderr.strip()[-2000:]}")


def _snapshot_volume(sandbox_id: str, path: str) -> str:
    """The name of the volume that keeps directory ``path`` of Sandbox ``sandbox_id`` in its
    snapshot."""
    return f"ale-snapshot-{sandbox_id}{path.replace('/', '-')}"


async def save(sandbox_id: str, volumes: Mapping[str, str], labels: Mapping[str, str]) -> None:
    """Copy a stopped Sandbox's ``volumes``, by the directory each is at, into volumes of its
    snapshot, labelled with ``labels``, its id and the directory each belongs at. One that
    cannot be made leaves none behind."""
    names = {path: _snapshot_volume(sandbox_id, path) for path in volumes}
    try:
        for path, name in names.items():
            label = {**labels, SNAPSHOT_LABEL: sandbox_id, PATH_LABEL: path}
            flags = [flag for item in label.items() for flag in ("--label", "=".join(item))]
            code, _, stderr = await run(
                "docker", "volume", "create", *flags, name, timeout=_START_SEC
            )
            if code != 0:
                raise ProviderStartError(f"could not create volume {name}: {stderr.strip()}")
        await copy([(volumes[path], name) for path, name in names.items()])
    except BaseException:
        with contextlib.suppress(BaseException):
            await asyncio.shield(remove(names.values()))
        raise


async def saved(sandbox_id: str, labels: Mapping[str, str]) -> dict[str, str]:
    """The volumes of the snapshot of Sandbox ``sandbox_id``, by the directory each belongs
    at; only those that carry ``labels`` too."""
    label = {**labels, SNAPSHOT_LABEL: sandbox_id}
    filters = [flag for item in label.items() for flag in ("--filter", "label=" + "=".join(item))]
    code, out, stderr = await run(
        "docker", "volume", "ls", *filters,
        "--format", f'{{{{.Name}}}} {{{{.Label "{PATH_LABEL}"}}}}',
        timeout=30,
    )  # fmt: skip
    if code != 0:
        raise ProviderStartError(f"could not list the snapshot's volumes: {stderr.strip()}")
    found: dict[str, str] = {}
    for line in out.splitlines():
        name, _, path = line.strip().partition(" ")
        if name and path:
            found[path] = name
    return found


async def remove(names: Iterable[str]) -> None:
    """Remove the volumes ``names``; one already gone needs nothing."""
    if not (listed := list(names)):
        return
    code, _, stderr = await run("docker", "volume", "rm", *listed, timeout=_REMOVE_SEC)
    errors = [line for line in stderr.splitlines() if line.strip()]
    if code != 0 and not all("no such volume" in line.lower() for line in errors):
        raise ProviderStartError(f"could not remove volumes {', '.join(listed)}: {stderr.strip()}")
