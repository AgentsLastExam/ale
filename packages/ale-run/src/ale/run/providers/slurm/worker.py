"""A single-node Slurm allocation and the Sandboxes in it. Copied to the cluster, standard
library only.

The batch step holds Podman's private namespace and store on the node and loads the image
the allocation was seeded with. Each Sandbox the Host asks for runs as a job step of its
own, which Slurm holds to that Sandbox's CPUs, memory and GPUs; control steps overlap it.
A Sandbox saved as a snapshot is committed to the store, where the next one boots from it.
"""

from __future__ import annotations

import csv
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SANDBOXES = Path("sandboxes")
SNAPSHOT_REPOSITORY = "localhost/ale-snapshot"
#: Each allocation's Podman store on its node, below ``$SLURM_TMPDIR`` or ``/tmp``.
STORE_PREFIX = "ale-slurm-"
#: In each store, the ID of the job it belongs to.
OWNER = "job"


def output(*argv: str) -> str:
    return subprocess.check_output(argv, text=True).strip()


def write(path: Path | str, value: object) -> None:
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def read(path: Path | str) -> str:
    try:
        return Path(path).read_text().strip()
    except FileNotFoundError:
        return ""


def devices() -> list[dict[str, object]]:
    rows = output(
        "nvidia-smi",
        "--query-gpu=uuid,name,driver_version,memory.total",
        "--format=csv,noheader,nounits",
    )
    return [
        dict(id=uuid, model=name, driver_version=driver, memory_total_mb=int(memory))
        for uuid, name, driver, memory in csv.reader(rows.splitlines(), skipinitialspace=True)
    ]


def podman(directory: str) -> list[str]:
    # All steps in this job share its pause process. Concurrent jobs on the same node
    # must never use the submitting UID's common pause PID, lock or mount namespace.
    xdg = Path(directory) / "xdg"
    xdg.mkdir(mode=0o700, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = str(xdg)
    os.environ["TMPDIR"] = directory
    return [
        "podman",
        "--root",
        directory + "/root",
        "--runroot",
        directory + "/run",
        "--tmpdir",
        directory + "/libpod",
        "--volumepath",
        directory + "/volumes",
    ]


def container(index: str) -> str:
    return "sandbox-" + index


def network(command: list[str], name: str, *, sealed: bool) -> None:
    namespace = output(*command, "inspect", "-f", "{{.NetworkSettings.SandboxKey}}", name)
    net = [*command, "unshare", "nsenter", "--net=" + namespace]
    # Slirp4netns gives this container one tap. gVisor has taken its routes into netstack.
    interfaces = [
        link["ifname"]
        for link in json.loads(output(*net, "ip", "-j", "link"))
        if link["ifname"] != "lo"
    ]
    if len(interfaces) != 1:
        raise RuntimeError(f"expected one private network interface, found {interfaces}")
    interface = interfaces[0]
    tc = [*net, "tc"]
    if sealed:
        subprocess.run([*tc, "qdisc", "replace", "dev", interface, "clsact"], check=True)
        for side in ("egress", "ingress"):
            subprocess.run(
                [
                    *tc,
                    "filter",
                    "replace",
                    "dev",
                    interface,
                    side,
                    "pref",
                    "1",
                    "protocol",
                    "all",
                    "handle",
                    "1",
                    "matchall",
                    "action",
                    "drop",
                ],
                check=True,
            )
    elif any(
        q["kind"] == "clsact"
        for q in json.loads(output(*tc, "-j", "qdisc", "show", "dev", interface))
    ):
        subprocess.run([*tc, "qdisc", "del", "dev", interface, "clsact"], check=True)


def own_cgroup() -> Path:
    relative = next(
        line[3:]
        for line in Path("/proc/self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    return Path("/sys/fs/cgroup") / relative.lstrip("/")


def job_cgroup() -> Path:
    path = own_cgroup()
    return next(
        parent
        for parent in (path, *path.parents)
        if parent.name == "job_" + os.environ["SLURM_JOB_ID"]
    )


def limit_cgroup() -> Path:
    """The nearest cgroup above this process that limits its memory: Slurm limits each step
    of a job on its own, below the job's limit, at a level of its hierarchy that varies."""
    path = own_cgroup()
    for parent in (path, *path.parents):
        limit = parent / "memory.max"
        if limit.is_file() and limit.read_text().strip() != "max":
            return parent
    raise RuntimeError("Slurm placed this step in no cgroup that limits its memory")


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except (FileNotFoundError, IndexError):
        return False
    return state not in {"Z", "X"}


def runtime_binary(command: list[str], version: str) -> Path:
    cache = Path.cwd().parent
    runtime = cache / ("gvisor-" + version)
    with (cache / ("gvisor-" + version + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not runtime.is_dir():
            source = output(
                *command, "create", "ghcr.io/agentslastexam/ale-gvisor:" + version, "/bin/true"
            )
            with tempfile.TemporaryDirectory(dir=cache) as temporary:
                dest = Path(temporary) / "runtime"
                subprocess.run([*command, "cp", source + ":/gvisor", str(dest)], check=True)
                dest.rename(runtime)
    return runtime / "runsc"


def launch(index: str, request: dict[str, object], *, gpus: bool) -> subprocess.Popen[bytes]:
    """Run one Sandbox as a step of its own, which Slurm holds to its resources."""
    args = [
        "srun",
        "--exact",
        "--nodes=1",
        "--ntasks=1",
        f"--cpus-per-task={request['cpus']}",
        f"--mem={request['memory_mb']}M",
        "--cpu-bind=threads",
    ]
    if gpus:
        # A step that asks for none sees none of the job's GPUs.
        args.append(f"--gres=gpu:{request['gpus']}")
    log = (SANDBOXES / index / "step.log").open("wb")
    try:
        return subprocess.Popen(
            [*args, "python3", "-I", __file__, "start", index],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    finally:
        log.close()


def stale_stores(scratch: Path) -> list[Path]:
    """The stores on this node of this account's jobs that have ended: one that Slurm
    killed, at its wall time or by preemption, or whose node went down, cleaned up nothing."""
    stale = []
    for store in scratch.glob(STORE_PREFIX + "*"):
        job = read(store / OWNER)
        if (
            not job.isdecimal()
            or job == os.environ["SLURM_JOB_ID"]
            or store.stat().st_uid != os.getuid()
        ):
            continue
        # squeue fails for a job Slurm no longer knows, and lists nothing for one that ended.
        listed = subprocess.run(
            ["squeue", "-h", "-j", job, "-o", "%T"], capture_output=True, text=True, check=False
        )
        if not listed.stdout.strip():
            stale.append(store)
    return stale


def release(command: list[str], directory: str) -> None:
    """Remove a store and every mount Podman made below it."""
    subprocess.run(
        [*command, "unshare", "python3", "-I", __file__, "release", directory], check=True
    )
    # Podman sets up an empty store at its configured path again as it exits.
    subprocess.run(["rm", "-rf", "--one-file-system", "--", directory], check=True)


def supervise() -> None:
    spec = json.loads(Path("allocation.json").read_text())
    scratch = Path(os.getenv("SLURM_TMPDIR") or "/tmp")
    directory = tempfile.mkdtemp(prefix=STORE_PREFIX, dir=scratch)
    (Path(directory) / OWNER).write_text(os.environ["SLURM_JOB_ID"])
    command = podman(directory)
    steps: dict[str, subprocess.Popen[bytes]] = {}

    def terminated(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    # Slurm signals the batch step before it kills it, as a job is cancelled or preempted:
    # clean up then too.
    signal.signal(signal.SIGTERM, terminated)
    try:
        # Keep the private rootless user namespace alive until the batch step finishes
        # cleanup, including when Slurm kills every Sandbox's step.
        subprocess.run([*command, "unshare", "true"], check=True)
        for store in stale_stores(scratch):
            subprocess.run(
                [*command, "unshare", "rm", "-rf", "--one-file-system", "--", str(store)],
                check=False,
            )
        subprocess.run(
            [*command, "load", "-q", "-i", str(Path.cwd().parent / spec["archive"])],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        if output(*command, "image", "inspect", "-f", "{{.Id}}", spec["image"]) != spec["image"]:
            raise RuntimeError("the staged archive holds a different image")
        runsc = str(runtime_binary(command, spec["version"]))
        version = output(runsc, "--version").splitlines()[0]
        if not version.endswith("+ale." + spec["version"]):
            raise RuntimeError(f"unexpected gVisor build {version}")
        if spec["gpus"]:
            supported = output(runsc, "nvproxy", "list-supported-drivers").split()
            for driver in {str(device["driver_version"]) for device in devices()}:
                if driver not in supported:
                    raise RuntimeError(
                        f"the node's NVIDIA driver {driver} is not one gVisor {version} "
                        "supports; its nvproxy lists them with list-supported-drivers"
                    )
        SANDBOXES.mkdir(exist_ok=True)
        write(
            "ready",
            dict(directory=directory, runsc=runsc, runtime=version, node=os.uname().nodename),
        )
        while not read("stop"):
            for request in sorted(SANDBOXES.glob("*/request.json")):
                index = request.parent.name
                if index not in steps and not (request.parent / "done").exists():
                    steps[index] = launch(
                        index, json.loads(request.read_text()), gpus=bool(spec["gpus"])
                    )
            for index, step in list(steps.items()):
                if (code := step.poll()) is not None:
                    write(SANDBOXES / index / "done", code)
                    del steps[index]
            time.sleep(0.5)
    finally:
        # The Host stops the allocation once its Sandboxes are gone; any left now end with
        # their steps, which srun ends on a signal.
        for step in steps.values():
            step.terminate()
        for index, step in steps.items():
            try:
                step.wait(timeout=60)
            except subprocess.TimeoutExpired:
                step.kill()
                step.wait()
            write(SANDBOXES / index / "done", step.returncode)
        # The batch step outlives each Sandbox's step, including an OOM that kills every
        # process there. Keep the job's counters before the allocation goes away.
        Path("memory").write_text((job_cgroup() / "memory.events").read_text())
        try:
            subprocess.run([*command, "rm", "-a", "-f", "-v", "-t", "1"], check=True)
            release(command, directory)
        except Exception as exc:
            write("cleanup-error", str(exc))
            raise


def start(index: str) -> None:
    box = SANDBOXES / index
    spec = json.loads((box / "request.json").read_text())
    ready = json.loads(Path("ready").read_text())
    limit = limit_cgroup()
    if (
        int((limit / "memory.max").read_text()) != spec["memory_mb"] << 20
        or (limit / "memory.swap.max").read_text().strip() != "0"
        or len(os.sched_getaffinity(0)) != spec["cpus"]
    ):
        raise RuntimeError(
            "Slurm did not enforce the requested CPU, memory and zero-swap allocation"
        )
    command = podman(ready["directory"])
    name = container(index)
    gpus = devices() if spec["gpus"] else []
    if len(gpus) != spec["gpus"]:
        raise RuntimeError(f"received {len(gpus)} GPUs, requested {spec['gpus']}")
    flags = [
        *command,
        "run",
        "-d",
        "--name",
        name,
        "--network=slirp4netns:allow_host_loopback=false",
        "--http-proxy=false",
        "--runtime",
        ready["runsc"],
        "--runtime-flag=ignore-cgroups",
        "--runtime-flag=enclosing-memory-cgroup=" + str(limit),
        "--oom-score-adj=500",
        *spec["runtime_args"],
    ]
    if gpus:
        flags += ["--runtime-flag=nvproxy", "-e", "NVIDIA_VISIBLE_DEVICES=void"]
        for gpu in gpus:
            flags.extend(["--device", "nvidia.com/gpu=" + str(gpu["id"])])
    for key, value in spec["env"].items():
        flags.extend(["--env", f"{key}={value}"])
    try:
        subprocess.run([*flags, spec["image"]], check=True, stdout=subprocess.DEVNULL)
        if output(*command, "inspect", "--format", "{{.Image}}", name) != spec["image"]:
            raise RuntimeError("Podman started a different image")
        if spec["sealed"]:
            network(command, name, sealed=True)
        conmon = int(output(*command, "inspect", "--format", "{{.State.ConmonPid}}", name))
        write(
            box / "ready",
            dict(
                container=name,
                runtime_flags=[
                    flag for flag in spec["runtime_args"] if flag.startswith("--runtime-flag=")
                ],
                image=spec["image"],
                devices=gpus,
                step=os.environ["SLURM_STEP_ID"],
            ),
        )
        while True:
            order = read(box / "stop")
            if order == "save":
                save(command, name, index, box)
                return
            if order:
                return
            if not alive(conmon):
                raise RuntimeError("the sandbox image command exited")
            time.sleep(1)
    finally:
        (box / "memory").write_text((limit / "memory.events").read_text())
        subprocess.run(
            [*command, "rm", "-f", "-v", "-t", "1", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def save(command: list[str], name: str, index: str, box: Path) -> None:
    """Stop the Sandbox and commit its disk to the allocation's store."""
    subprocess.run([*command, "stop", "-t", "30", name], check=True, stdout=subprocess.DEVNULL)
    image = output(*command, "commit", "-q", name, f"{SNAPSHOT_REPOSITORY}:{index}")
    write(box / "saved", dict(image=image.removeprefix("sha256:")))


def export(command: list[str], image: str, destination: str) -> None:
    """Write a snapshot to the shared file system, from which another allocation loads it."""
    path = Path.cwd().parent / destination
    path.parent.mkdir(mode=0o700, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("wb") as file:
        saver = subprocess.Popen([*command, "save", image], stdout=subprocess.PIPE)
        assert saver.stdout is not None
        gzip = subprocess.run(["gzip", "-1"], stdin=saver.stdout, stdout=file, check=False)
        saver.stdout.close()
        if saver.wait() or gzip.returncode:
            temporary.unlink()
            raise RuntimeError(f"could not export {image}")
    temporary.replace(path)


def main() -> None:
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "supervise":
        try:
            supervise()
        except Exception as exc:
            if not Path("ready").exists():
                write("error", str(exc))
            raise
        return
    if mode == "release":
        directory = Path(args[0]).resolve()
        # Detach Podman's private namespace and overlay mounts before removing their
        # backing directories. findmnt decodes paths, including mountinfo escapes.
        table = json.loads(output("findmnt", "--json", "--list", "--output", "TARGET"))
        mounts = (Path(entry["target"]) for entry in table["filesystems"])
        for path in sorted((p for p in mounts if p.is_relative_to(directory)), reverse=True):
            subprocess.run(["umount", "-l", "--", str(path)], check=True)
        subprocess.run(["rm", "-rf", "--one-file-system", "--", str(directory)], check=True)
        return
    if mode == "start":
        try:
            start(args[0])
        except Exception as exc:
            write(SANDBOXES / args[0] / "error", str(exc))
            raise
        return
    ready = json.loads(Path("ready").read_text())
    command = podman(ready["directory"])
    if mode in ("block", "open"):
        network(command, container(args[0]), sealed=mode == "block")
    elif mode == "exec":
        box = json.loads((SANDBOXES / args[0] / "ready").read_text())
        os.execvp(
            command[0],
            [
                *command,
                *box["runtime_flags"],
                "exec",
                "-i",
                "--user",
                "0",
                "--workdir",
                "/",
                box["container"],
                *args[1:],
            ],
        )
    elif mode == "rmi":
        # By its tag: a snapshot that a later one was committed over stays as that one's
        # parent, and removing it only untags it.
        subprocess.run(
            [*command, "rmi", f"{SNAPSHOT_REPOSITORY}:{args[0]}"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    elif mode == "export":
        export(command, args[0], args[1])
    else:
        raise ValueError(mode)


if __name__ == "__main__":
    main()
