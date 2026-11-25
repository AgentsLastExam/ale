"""What admits a Sandbox to a Docker daemon's Host: one lock per daemon, and the Host's CPUs.

Every Provider that starts its Sandboxes through a Docker daemon, a Docker container or the
runner container of a QEMU or Firecracker VM, pins each with ``--cpuset-cpus`` to ``cpus``
Host CPUs on cores of its own: it takes free cores whole, so no two Sandboxes share a core. A
label on each Sandbox's container records what it holds, so the daemon's running containers
are the record, as they are of its GPUs. The lock, which every Orbit process of every user
takes for the daemon, is held from reading what is free until Docker has started the
container that takes it. A Sandbox that finds too few free waits for them, saying so every
ten minutes. Nothing keeps the Host's other processes off a Sandbox's CPUs.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import functools
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from orbit.core.errors import ProviderCapabilityError, ProviderStartError
from orbit.core.taskspec import OperatingSystem, Resources
from orbit.run import guestd
from orbit.run.transport import GuestClient

__all__ = [
    "LABEL",
    "Admission",
    "Grant",
    "Topology",
    "choose",
    "cpu_list",
    "lock_path",
    "locked",
    "parse_cpu_list",
    "pinned",
    "require_cpus",
    "run_args",
    "topology",
]

#: The label of every pinned Sandbox's container: each Host CPU it holds, in ``cpu_list`` form.
LABEL = "orbit.cpus"
#: How often a Sandbox that waits for CPUs looks again, and how often it says it still waits.
_POLL_SEC = 5.0
_NOTICE_SEC = 600.0
_SYSTEM = Path("/sys/devices/system/cpu")
#: How many CPUs the framework's process in a Sandbox runs on, as its system counts them.
_SEEN = (
    "import os; print(len(os.sched_getaffinity(0)) "
    "if hasattr(os, 'sched_getaffinity') else os.cpu_count())"
)
_log = logging.getLogger("orbit.execution")

#: Runs ``docker`` with these arguments, as a Provider does: its exit code, output and errors.
Docker = Callable[..., Awaitable[tuple[int, str, str]]]


def parse_cpu_list(text: str) -> tuple[int, ...]:
    """The CPUs of a list such as ``0-3,8``, as the kernel and Docker write them."""
    cpus: set[int] = set()
    for part in text.strip().split(","):
        if not part:
            continue
        first, _, last = part.partition("-")
        cpus.update(range(int(first), int(last or first) + 1))
    return tuple(sorted(cpus))


def cpu_list(cpus: Iterable[int]) -> str:
    """``cpus`` as such a list, each run of consecutive CPUs as a range."""
    runs: list[list[int]] = []
    for cpu in sorted(set(cpus)):
        if runs and cpu == runs[-1][1] + 1:
            runs[-1][1] = cpu
        else:
            runs.append([cpu, cpu])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


@dataclass(frozen=True)
class Topology:
    """The Host's online CPUs, grouped by the physical core each is a hardware thread of."""

    cores: tuple[tuple[int, ...], ...]
    """Each core's threads, cores by package and then core, as Linux numbers them."""

    @property
    def threads(self) -> int:
        return sum(len(core) for core in self.cores)


@functools.cache
def topology(system: Path = _SYSTEM) -> Topology | None:
    """This Host's, from sysfs, read once; None on a Host without Linux's, such as a Mac."""
    if not (system / "online").is_file():
        return None
    cores: dict[tuple[int, int], list[int]] = {}
    for cpu in parse_cpu_list((system / "online").read_text()):
        place = system / f"cpu{cpu}" / "topology"
        key = (
            int((place / "physical_package_id").read_text()),
            int((place / "core_id").read_text()),
        )
        cores.setdefault(key, []).append(cpu)
    return Topology(tuple(tuple(sorted(cores[key])) for key in sorted(cores)))


@dataclass(frozen=True)
class Grant:
    """The Host CPUs a Sandbox is pinned to."""

    cpus: tuple[int, ...]

    def run_args(self) -> list[str]:
        """What ``docker run`` pins and labels its container with."""
        return ["--cpuset-cpus", cpu_list(self.cpus), "--label", f"{LABEL}={cpu_list(self.cpus)}"]


def run_args(grant: Grant | None) -> list[str]:
    """What ``docker run`` pins a Sandbox's container with, if anything, and labels it with
    either way: empty where it holds nothing, so that the label of the image, as a snapshot
    of a pinned Sandbox keeps it, never stands in for what it holds."""
    return grant.run_args() if grant is not None else ["--label", f"{LABEL}="]


def choose(host: Topology, taken: set[int], count: int) -> Grant | None:
    """The ``count`` CPUs a Sandbox takes beside ``taken``, the threads of cores none of which
    is taken, or None when too few are free. A core it takes a thread of is all its own: one
    that ``taken`` holds a thread of is no one else's to take."""
    picked = [cpu for core in host.cores if taken.isdisjoint(core) for cpu in core]
    if len(picked) < count:
        return None
    return Grant(tuple(sorted(picked[:count])))


async def lock_path(directory: Path, docker: Docker) -> Path:
    """The admission lock file of the Docker daemon ``docker`` reaches, by the daemon's ID,
    which every Orbit process that admits through the daemon takes, whatever its user or
    cache."""
    code, out, stderr = await docker("info", "--format", "{{.ID}}", timeout=30)
    if code != 0 or not out.strip():
        raise ProviderStartError(f"could not identify the Docker daemon: {stderr.strip()}")
    return directory / f"orbit-admission-{out.strip()}.lock"


@contextlib.asynccontextmanager
async def locked(path: Path) -> AsyncIterator[None]:
    """Hold the admission lock at ``path``."""
    descriptor = _open_lock(path)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        yield
    finally:
        os.close(descriptor)  # which releases the lock


def _open_lock(path: Path) -> int:
    """The lock file at ``path``, read-only, which flock needs no more than.

    One that exists is opened without ``O_CREAT``: ``fs.protected_regular`` refuses that on
    another user's file in a sticky directory such as ``/tmp``. One that does not is created
    readable by every user, whatever the umask; when another process creates it first, it is
    opened as theirs.
    """
    while True:
        try:
            return os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            pass
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        os.fchmod(descriptor, 0o644)
        return descriptor


async def _taken(docker: Docker) -> set[int]:
    """Every Host CPU a running container on the daemon holds, by its label."""
    code, out, stderr = await docker(
        "ps", "--filter", f"label={LABEL}", "--format", f'{{{{.Label "{LABEL}"}}}}', timeout=30
    )
    if code != 0:
        raise ProviderStartError(f"could not list the Sandboxes' CPUs: {stderr.strip()}")
    return {cpu for line in out.split() for cpu in parse_cpu_list(line)}


@contextlib.asynccontextmanager
async def pinned(
    lock: Path, resources: Resources, host: Topology, docker: Docker
) -> AsyncIterator[Grant]:
    """The CPUs a Sandbox of ``resources`` takes, with the lock held until the caller has
    started its container; waits, without the lock, while too few are free."""
    if resources.cpus > host.threads:
        raise ProviderCapabilityError(
            f"the Sandbox needs {resources.cpus} CPUs; the Host has {host.threads}"
        )
    started = time.monotonic()
    noticed: float | None = None
    while True:
        async with locked(lock):
            grant = choose(host, await _taken(docker), resources.cpus)
            if grant is not None:
                yield grant
                return
        now = time.monotonic()
        if noticed is None or now - noticed >= _NOTICE_SEC:
            noticed = now
            _log.info(
                "the Sandbox waits for free CPUs",
                extra={
                    "orbit_data": {
                        "cpus": resources.cpus,
                        "waited_sec": round(now - started),
                    }
                },
            )
        await asyncio.sleep(_POLL_SEC)


async def require_cpus(
    client: GuestClient, cpus: int, system: OperatingSystem = OperatingSystem.LINUX
) -> None:
    """Fail unless the Sandbox behind ``client`` sees itself run on ``cpus`` CPUs."""
    code, out, err, _ = await client.exec([guestd.PYTHON[system.value], "-I", "-c", _SEEN])
    if code != 0 or out.strip() != str(cpus):
        raise ProviderStartError(
            f"the Sandbox runs on {out.strip() or err.strip()} CPUs, not the {cpus} it was given"
        )


class Admission:
    """A Provider's admission through the Docker daemon it reaches: the daemon's lock, in
    ``lock_dir``, and the CPUs it pins Sandboxes to, which it does wherever the daemon applies
    a cpuset. A rootless daemon applies one only where the user's systemd delegates the
    cpuset controller to it, and Docker otherwise drops ``--cpuset-cpus`` with a warning."""

    def __init__(self, docker: Docker, lock_dir: Path = Path("/tmp")) -> None:
        self.docker = docker
        self.lock_dir = lock_dir
        self._lock: Path | None = None
        self._cpusets: bool | None = None

    async def lock(self) -> Path:
        if self._lock is None:
            self._lock = await lock_path(self.lock_dir, self.docker)
        return self._lock

    async def cpusets(self) -> bool:
        """Whether the daemon applies a cpuset, which it reports once."""
        if self._cpusets is None:
            code, out, stderr = await self.docker("info", "--format", "{{.CPUSet}}", timeout=30)
            if code != 0:
                raise ProviderStartError(f"could not read how Docker runs: {stderr.strip()}")
            self._cpusets = out.strip() == "true"
        return self._cpusets

    @contextlib.asynccontextmanager
    async def admitted(
        self, resources: Resources, *, pins: bool = True, gpus: bool = False
    ) -> AsyncIterator[Grant | None]:
        """The daemon's lock, held while the caller admits a Sandbox of ``resources`` and
        starts its container, with the CPUs it is pinned to; None where its Sandboxes are not
        pinned: under a runtime that does not hold them to a cpuset (``pins``), by a daemon
        that applies none, or on a Host without Linux's CPU topology. The lock is then held
        only for ``gpus``."""
        host = topology()
        if pins and host is not None and await self.cpusets():
            async with pinned(await self.lock(), resources, host, self.docker) as grant:
                yield grant
        elif gpus:
            async with locked(await self.lock()):
                yield None
        else:
            yield None
