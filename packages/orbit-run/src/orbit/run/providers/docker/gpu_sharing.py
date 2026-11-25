"""How a Docker leaf shares the Host's GPUs among its Sandboxes by device memory.

A Sandbox without ``gpu_memory_mb`` takes its GPUs whole. One with it shares each GPU with
others that name it too: the Provider admits it where the memory free on the GPU, less what
Orbit's Sandboxes there reserved and do not use yet, leaves its need and a margin for other
tenants' growth, and watches what it uses while a Run holds it, since under runc nothing holds
a process to its share, and under gVisor a failure of nvproxy's accounting could let it pass.
The running containers' labels are the record of what Orbit holds. The Host's ``nvidia-smi``
lists every process that uses a GPU by its Host PID, and a PID's cgroup names the container
that holds it. This module reads and decides; the Provider acts.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from orbit.core.errors import ProviderCapacityError, ProviderStartError
from orbit.core.sandbox import GpuDevice
from orbit.run.gpu import normalize_nvidia_uuid
from orbit.run.process import run

__all__ = [
    "KILL",
    "WATCH_SEC",
    "GpuProcess",
    "Holder",
    "Overrun",
    "Watchdog",
    "Watched",
    "admit",
    "host_process",
    "overruns",
    "read_processes",
]

_log = logging.getLogger(__name__)

PROC = Path("/proc")
#: How often the watchdog reads what each process uses of each GPU: NVML has no event for it.
WATCH_SEC = 5.0
#: Run by ``sh`` in a helper container that joins a runc Sandbox's PID namespace: kills the
#: process ``$1`` of that namespace, unless it started at another time than ``$2``
#: (``/proc/<pid>/stat`` field 22, in clock ticks since boot) and so is another process. Its
#: ``/proc`` is that namespace's; nothing in the Sandbox runs, so nothing there can stop it.
KILL = """
stat=$(cat "/proc/$1/stat") || exit 3
pid=$1 started=$2
set -- ${stat##*")"}
if [ "${20}" != "$started" ]; then
    echo "process $pid is no longer the one that used the GPU" >&2
    exit 4
fi
kill -KILL "$pid"
"""
#: A process's cgroup under Docker: ``docker-<id>.scope`` with the systemd cgroup driver,
#: rootful or rootless, and ``/docker/<id>`` with cgroupfs.
_CONTAINER = re.compile(r"/docker[-/]([0-9a-f]{64})(?:\.scope)?(?=/|$)", re.MULTILINE)


@dataclass(frozen=True)
class Holder:
    """A running Orbit Sandbox that holds GPUs, as its container's labels say."""

    container: str
    """Its container's ID."""
    name: str
    gpus: tuple[str, ...]
    memory_mb: int | None
    """What it reserved on each GPU, which it shares; None when it holds them whole."""


@dataclass(frozen=True)
class GpuProcess:
    """A process that uses a GPU, as the Host's ``nvidia-smi`` and ``/proc`` show it: under
    runc an application's, under gVisor a whole sandbox's Sentry."""

    pid: int
    """Its PID on the Host."""
    gpu: str
    used_mb: int
    container: str
    """The ID of the container whose cgroup holds it."""
    started: str
    """When it started, in clock ticks since boot: a PID reused later names another."""
    inner_pid: int
    """Its PID in the container's PID namespace."""
    name: str


@dataclass(frozen=True)
class Overrun:
    """A Sandbox that uses more device memory on a GPU than it reserved."""

    gpu: str
    used_mb: int
    processes: tuple[GpuProcess, ...]
    """Its processes on that GPU."""


@dataclass(frozen=True)
class Watched:
    """A Sandbox the watchdog watches."""

    container: str
    """Its container's ID."""
    memory_mb: int
    """What it reserved on each GPU."""
    act: Callable[[Overrun], Awaitable[None]]
    """Acts on an overrun of it."""


def host_process(pid: int, gpu: str, used_mb: int, proc: Path = PROC) -> GpuProcess | None:
    """Process ``pid`` as ``/proc`` shows it now, when a Docker container holds it; None for a
    process outside every container, or one that has ended."""
    directory = proc / str(pid)
    try:
        cgroup = (directory / "cgroup").read_text()
        stat = (directory / "stat").read_text()
        status = (directory / "status").read_text()
        name = (directory / "comm").read_text().strip()
    except OSError:
        return None
    container = _CONTAINER.search(cgroup)
    namespaced = [line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:")]
    if container is None or not namespaced or not namespaced[0]:
        return None
    return GpuProcess(
        pid=pid,
        gpu=gpu,
        used_mb=used_mb,
        container=container[1],
        started=stat[stat.rindex(")") + 2 :].split()[19],
        # A runc Sandbox cannot nest a PID namespace of its own, so the innermost is its own.
        inner_pid=int(namespaced[0][-1]),
        name=name,
    )


async def read_processes(proc: Path = PROC) -> list[GpuProcess]:
    """Every process in a Docker container that uses a GPU, by the Host's ``nvidia-smi``, which
    lists every user's, one row per process and GPU. A row whose memory it does not report is
    left out."""
    code, output, stderr = await run(
        "nvidia-smi",
        "--query-compute-apps=pid,used_memory,gpu_uuid",
        "--format=csv,noheader,nounits",
        timeout=30,
    )
    if code != 0:
        raise ProviderStartError(f"nvidia-smi could not list the GPUs' processes: {stderr}")
    found: list[GpuProcess] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        pid, used, gpu = (field.strip() for field in line.split(","))
        if used.isdigit() and (
            process := host_process(int(pid), normalize_nvidia_uuid(gpu), int(used), proc)
        ):
            found.append(process)
    return found


def admit(
    devices: Sequence[tuple[int, GpuDevice]],
    holders: Sequence[Holder],
    processes: Sequence[GpuProcess],
    *,
    count: int,
    memory_mb: int | None,
    margin_mb: int,
) -> tuple[GpuDevice, ...]:
    """The ``count`` GPUs of ``devices``, each given by index as ``nvidia-smi`` reports it
    now, that a Sandbox needing ``memory_mb`` on each takes, those with the most headroom
    first.

    A Sandbox that needs no memory takes a GPU no Orbit Sandbox holds. One that does takes a GPU
    no Orbit Sandbox holds whole, whose headroom leaves it ``memory_mb`` and ``margin_mb``: the
    memory free there less what each Orbit Sandbox sharing it reserved and does not use yet, as
    what it uses is no longer free. Too few refuses the Sandbox, naming each GPU and why.
    """
    in_use: Counter[tuple[str, str]] = Counter()
    for process in processes:
        in_use[process.container, process.gpu] += process.used_mb
    eligible: list[tuple[int | None, int, GpuDevice]] = []
    considered: list[str] = []
    for index, device in devices:
        whole = [held.name for held in holders if device.id in held.gpus and not held.memory_mb]
        sharing = [held for held in holders if device.id in held.gpus and held.memory_mb]
        reserved = sum(
            max(0, (held.memory_mb or 0) - in_use[held.container, device.id]) for held in sharing
        )
        free = device.memory_free_mb
        headroom = None if free is None else free - reserved
        label = f"GPU {index} ({device.id})"
        if whole:
            considered.append(f"{label}: {', '.join(whole)} holds it whole")
        elif memory_mb is None and sharing:
            considered.append(f"{label}: {', '.join(held.name for held in sharing)} share it")
        elif memory_mb is not None and headroom is None:
            considered.append(f"{label}: nvidia-smi reports no free memory")
        elif memory_mb is not None and headroom is not None and headroom < memory_mb + margin_mb:
            considered.append(
                f"{label}: {free} MiB free, {reserved} MiB of it reserved by Orbit Sandboxes "
                f"that do not use it yet, so {headroom} MiB of headroom"
            )
        else:
            eligible.append((headroom, index, device))
            considered.append(
                f"{label}: eligible"
                + (f" with {headroom} MiB of headroom" if headroom is not None else "")
            )
    eligible.sort(key=lambda item: (item[0] is None, -(item[0] or 0), item[1]))
    if len(eligible) < count:
        plural = "s" if count > 1 else ""
        wanted = (
            f"{count} GPU{plural} no Orbit Sandbox holds"
            if memory_mb is None
            else f"{memory_mb} MiB on each of {count} GPU{plural}, and a margin of {margin_mb} MiB"
        )
        raise ProviderCapacityError(
            f"the Sandbox needs {wanted}; of the GPUs it may take here: " + "; ".join(considered)
        )
    return tuple(device for _, _, device in eligible[:count])


def overruns(
    processes: Sequence[GpuProcess], reserved: dict[str, int]
) -> list[tuple[str, Overrun]]:
    """Each Sandbox, by the ID of its container, that uses more than it ``reserved`` on a GPU,
    with what it uses there. A share holds every CUDA context, and the driver's own memory
    for a context is in no process's row, so the rows' sum is compared with the share."""
    found: dict[tuple[str, str], list[GpuProcess]] = {}
    for process in processes:
        if process.container in reserved:
            found.setdefault((process.container, process.gpu), []).append(process)
    return [
        (container, Overrun(gpu, used, tuple(those)))
        for (container, gpu), those in found.items()
        if (used := sum(process.used_mb for process in those)) > reserved[container]
    ]


class Watchdog:
    """Every :data:`WATCH_SEC` while it watches any Sandbox, reads what each process uses of
    each GPU, and has each Sandbox that uses more than it reserved act, all at once.

    One task serves every Sandbox of the Provider that owns it, and ends when it watches none.
    A Sandbox stays watched until its Provider unwatches it.
    """

    def __init__(self) -> None:
        self._watched: dict[str, Watched] = {}
        self._task: asyncio.Task[None] | None = None

    def watch(self, watched: Watched) -> None:
        self._watched[watched.container] = watched
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._poll())

    def unwatch(self, container: str) -> None:
        self._watched.pop(container, None)

    async def check(self) -> None:
        """Look once, and have each Sandbox past what it reserved act, concurrently, so one
        slow action delays no other."""
        reserved = {container: watched.memory_mb for container, watched in self._watched.items()}
        if not reserved:
            return
        found = overruns(await read_processes(), reserved)
        acting = [watched.act(overrun) for container, overrun in found
                  if (watched := self._watched.get(container)) is not None]  # fmt: skip
        for outcome in await asyncio.gather(*acting, return_exceptions=True):
            if isinstance(outcome, Exception):
                raise outcome

    async def _poll(self) -> None:
        """Look until nothing is watched, warning once when looks start to fail and once
        when they work again."""
        failing = False
        while self._watched:
            await asyncio.sleep(WATCH_SEC)
            try:
                await self.check()
            except Exception:
                if not failing:
                    _log.warning(
                        "the GPU memory watchdog cannot look; it goes on trying", exc_info=True
                    )
                failing = True
                continue
            if failing:
                _log.warning("the GPU memory watchdog looks again")
            failing = False
