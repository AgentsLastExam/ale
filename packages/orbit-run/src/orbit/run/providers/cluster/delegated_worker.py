"""The worker's helper where the workload manager delegates the job's cgroup rather than
starting steps, as PBS Pro and LSF do, staged beside it: one node, whose Sandboxes it holds
each to its share of the job in a cgroup of its own."""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path


def native(architecture: str) -> str:
    """The matching Linux OCI platform. Refuse emulation or a misrouted build job."""
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    expected = {"arm64": "arm64", "x86_64": "amd64"}[architecture]
    if arch != expected or sys.platform != "linux":
        raise RuntimeError(
            f"job needs native linux/{expected}, node is {sys.platform}/{platform.machine()}"
        )
    return "linux/" + arch


def subids(command: list[str]) -> None:
    """The OCI layer's multi-user ownership must survive extraction and Podman builds."""
    for kind in ("uid", "gid"):
        data = subprocess.check_output(
            [*command, "unshare", "cat", f"/proc/self/{kind}_map"], text=True
        )
        if sum(int(row.split()[2]) for row in data.splitlines()) < 2:
            raise RuntimeError(
                f"rootless Podman needs a subordinate {kind.upper()} range on this compute node "
                "to preserve image ownership"
            )


def cuda_uuids() -> list[str]:
    """Resolve CUDA ordinals through the driver that interprets CUDA_VISIBLE_DEVICES."""
    driver = ctypes.CDLL("libcuda.so.1")

    def check(code: int) -> None:
        if code:
            raise RuntimeError(f"CUDA driver returned error {code} resolving allocated devices")

    check(driver.cuInit(0))
    count = ctypes.c_int()
    check(driver.cuDeviceGetCount(ctypes.byref(count)))
    ids = []
    for ordinal in range(count.value):
        device = ctypes.c_int()
        check(driver.cuDeviceGet(ctypes.byref(device), ordinal))
        value = (ctypes.c_ubyte * 16)()
        check(driver.cuDeviceGetUuid_v2(ctypes.byref(value), device))
        ids.append("GPU-" + str(uuid.UUID(bytes=bytes(value))))
    return ids


def allocated(devices: list[dict], count: int) -> list[dict]:
    """Only whole devices of this job, indexed by physical UUID for CDI."""
    if not count:
        return []
    mask = os.environ.get("CUDA_VISIBLE_DEVICES")
    by_id = {gpu["id"]: gpu for gpu in devices}
    if mask is None:
        if len(by_id) == count:
            return devices
    else:
        visible = [item.strip() for item in mask.split(",")]
        if len(visible) == count and len(set(visible)) == count:
            if all(item.isascii() and item.isdecimal() for item in visible):
                # NVML and CUDA can enumerate the same GPUs in different orders.
                visible = cuda_uuids()
            if (
                len(visible) == count
                and len(set(visible)) == count
                and all(dev.startswith("GPU-") and dev in by_id for dev in visible)
            ):
                return [by_id[dev] for dev in visible]
    raise RuntimeError(
        f"allocation requested {count} GPUs, NVML lists {len(devices)}, "
        f"CUDA_VISIBLE_DEVICES={mask!r}; the scheduler must grant that many whole GPU UUIDs"
    )


class Allocation:
    """The worker's subtree of the job's delegated cgroup v2 hierarchy, which Orbit made for
    it beside its own process's (``delegated.py``), and the Sandboxes reserved in it."""

    def __init__(self, group: Path, gpu_ids: list[str]):
        self.group = group
        self.control = group / f"orbit-control-{os.getpid()}"
        self.enabled: set[str] = set()
        self.cpus = sorted(os.sched_getaffinity(0))
        self.gpus = gpu_ids
        self.steps: dict[str, tuple[Path, set[int], set[str]]] = {}

    def open(self) -> None:
        try:
            if self.group == Path("/sys/fs/cgroup"):
                raise RuntimeError("worker is outside a dedicated job cgroup")
            existing = (self.group / "cgroup.subtree_control").read_text().split()
            available = set((self.group / "cgroup.controllers").read_text().split())
            needed = {"memory", "cpu"} | ({"cpuset"} & available)
            if absent := needed - available:
                raise RuntimeError("unavailable controllers: " + ", ".join(sorted(absent)))
            needed -= set(existing)
            self.control.mkdir()
            # Only move this worker: nothing else of the job is in its subtree. An
            # undelegated site must not silently apply the job's aggregate memory limit to
            # every Sandbox running beside it.
            (self.control / "cgroup.procs").write_text(str(os.getpid()))
            if needed:
                (self.group / "cgroup.subtree_control").write_text(
                    " ".join("+" + name for name in sorted(needed))
                )
                self.enabled = needed
        except (OSError, RuntimeError) as exc:
            self.close()
            raise RuntimeError(
                f"{self.group}: the job needs delegated cgroup v2 memory and CPU controllers "
                f"for each Sandbox: {exc}"
            ) from exc

    def close(self) -> None:
        if not self.control.exists():
            return
        # Orbit owns the parent. Restore only controllers enabled by this worker.
        if self.enabled:
            (self.group / "cgroup.subtree_control").write_text(
                " ".join("-" + name for name in sorted(self.enabled))
            )
        if str(os.getpid()) in (self.control / "cgroup.procs").read_text().split():
            # Its parent enables no controller now, so it may hold this process.
            (self.group / "cgroup.procs").write_text(str(os.getpid()))
        # Rootless Podman can leave its namespace keeper alive after store cleanup.
        # Everything still below this private cgroup belongs to this supervisor, never to
        # Orbit, whose process is in a cgroup of its own beside this subtree.
        if "populated 1" in (self.control / "cgroup.events").read_text().splitlines():
            (self.control / "cgroup.kill").write_text("1")
            for _ in range(100):
                if "populated 0" in (self.control / "cgroup.events").read_text().splitlines():
                    break
                time.sleep(0.05)
        self.control.rmdir()

    def launch(self, index: str, request: dict, script: str, log):
        if len(request["nodes"]) != 1:
            raise RuntimeError("this batch allocation can start only a single-node Sandbox")
        taken_cpus = {cpu for _, cpus, _ in self.steps.values() for cpu in cpus}
        taken_gpus = {gpu for _, _, gpus in self.steps.values() for gpu in gpus}
        cpus = [c for c in self.cpus if c not in taken_cpus][: request["cpus"]]
        gpus = [g for g in self.gpus if g not in taken_gpus][: request["gpus"]]
        if len(cpus) != request["cpus"] or len(gpus) != request["gpus"]:
            raise RuntimeError("the allocation lacks free CPUs or GPUs for this Sandbox")
        limit = self.group / f"orbit-step-{os.getpid()}-{index}"
        limit.mkdir()
        try:
            cpu_list = ",".join(str(c) for c in cpus)
            if (limit / "cpuset.cpus").is_file():
                (limit / "cpuset.cpus").write_text(cpu_list)
            (limit / "cpu.max").write_text(f"{len(cpus) * 100000} 100000")
            (limit / "memory.max").write_text(str(request["memory_mb"] << 20))
            (limit / "memory.swap.max").write_text("0")
            # The child joins the group before it starts Podman, including its gofer and Sentry.
            env = {
                **os.environ,
                "ORBIT_BATCH_STEP_CGROUP": str(limit),
                "ORBIT_BATCH_STEP_GPUS": ",".join(gpus),
                "ORBIT_BATCH_CPUS": cpu_list,
            }
            proc = subprocess.Popen(
                [sys.executable, "-I", script, "start", index],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        except BaseException:
            limit.rmdir()
            raise
        self.steps[index] = (limit, set(cpus), set(gpus))
        return proc

    def end(self, index: str, counters: Path) -> None:
        group, _, _ = self.steps.pop(index)
        if counters.parent.is_dir():
            counters.write_text((group / "memory.events").read_text())
        if "populated 1" in (group / "cgroup.events").read_text().splitlines():
            (group / "cgroup.kill").write_text("1")
        for _ in range(100):
            try:
                group.rmdir()
                return
            except OSError:
                time.sleep(0.05)
        group.rmdir()


class Delegated:
    def __init__(self, config: dict):
        self.workload_manager = config["workload_manager"]
        self.job = os.environ[config["job_variable"]]
        self.gpu_count = config["gpus"]
        self.cpu_count = config.get("cpus")
        self.podman = config.get("podman") or "podman"
        #: The subtree of the job's cgroup Orbit made for the worker, where it holds an
        #: allocation's Sandboxes; none for an image's preparation.
        self.cgroup = config.get("cgroup")
        self.node = socket.gethostname()
        self.allocation: Allocation | None = None
        self.store_owner = None

    native = staticmethod(native)

    def podman_argv(self, directory: str) -> list[str]:
        # Keep site runtime/storage choices while replacing just its injected container
        # environment. Podman applies CONTAINERS_CONF_OVERRIDE after any CLI modules.
        original = os.environ.setdefault(
            "ORBIT_SITE_CONTAINERS_CONF", os.environ.get("CONTAINERS_CONF_OVERRIDE", "")
        )
        os.environ["CONTAINERS_CONF_OVERRIDE"] = str(Path(directory) / "environment.conf")
        return [self.podman, *(["--module", original] if original else [])]

    def open(self, visible: list[dict]) -> None:
        if not self.cgroup:
            raise RuntimeError("Orbit gave the worker no cgroup to hold Sandboxes in")
        gpus = allocated(visible, self.gpu_count)
        allocation = Allocation(Path(self.cgroup), [gpu["id"] for gpu in gpus])
        if self.cpu_count is not None:
            if len(allocation.cpus) < self.cpu_count:
                raise RuntimeError(
                    f"job requested {self.cpu_count} CPUs, affinity permits {len(allocation.cpus)}"
                )
            allocation.cpus = allocation.cpus[: self.cpu_count]
        allocation.open()
        self.allocation = allocation

    def claim(self, owner: Path) -> None:
        # The supervisor holds this lock until its store is cleaned or the job is killed.
        # Site containers.conf may set env_host=true or name scheduler secrets in env.
        # A Task keeps its image-defined environment, plus values passed by the engine.
        owner.with_name("environment.conf").write_text(
            "[containers]\nenv=[{append=false}]\nenv_host=false\nhttp_proxy=false\n"
        )
        self.store_owner = owner.open("x")
        fcntl.flock(self.store_owner, fcntl.LOCK_EX)
        self.store_owner.write(self.job)
        self.store_owner.flush()

    def abandoned(self, directory: Path) -> list[Path]:
        stale = []
        for store in directory.glob(f"orbit-{self.workload_manager}-*"):
            with contextlib.suppress(FileNotFoundError, BlockingIOError, PermissionError):
                if store.is_symlink() or store.stat().st_uid != os.getuid():
                    continue
                # Allow time for scheduler cleanup after a hard kill and for store setup.
                if time.time() - store.stat().st_mtime < 600:
                    continue
                with (store / "job").open() as owner:
                    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if owner.read().strip():
                        stale.append(store)
        return stale

    def runtime_available(self, command: list[str], *, sandbox: bool = True) -> None:
        if not shutil.which(self.podman):
            raise RuntimeError(
                f"rootless Podman executable {self.podman!r} is missing on {self.node}"
            )
        for helper in ("slirp4netns", "nsenter") if sandbox else ():
            if not shutil.which(helper):
                raise RuntimeError(f"{helper} is missing on compute node {self.node}")
        subids(command)

    def verify_process(self, pid: int, limit: Path) -> None:
        path = next(
            (
                line[3:]
                for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                if line.startswith("0::")
            ),
            "",
        )
        if not path or not (Path("/sys/fs/cgroup") / path.lstrip("/")).is_relative_to(limit):
            raise RuntimeError(f"Podman process {pid} left the Sandbox resource cgroup {limit}")

    def join(self) -> Path:
        limit = Path(os.environ["ORBIT_BATCH_STEP_CGROUP"])
        (limit / "cgroup.procs").write_text(str(os.getpid()))
        cpus = {int(cpu) for cpu in os.environ["ORBIT_BATCH_CPUS"].split(",")}
        os.sched_setaffinity(0, cpus)
        quota, period = map(int, (limit / "cpu.max").read_text().split())
        if quota != len(cpus) * period:
            raise RuntimeError(f"wrong CPU quota in {limit}")
        self.verify_process(os.getpid(), limit)
        return limit

    def devices(self, visible: list[dict]) -> list[dict]:
        ids = os.environ["ORBIT_BATCH_STEP_GPUS"].split(",")
        by_id = {gpu["id"]: gpu for gpu in visible}
        return [by_id[gpu] for gpu in ids if gpu]
