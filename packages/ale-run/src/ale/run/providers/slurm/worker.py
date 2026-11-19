"""Single-node Slurm job and control steps. Copied to the cluster, standard library only."""

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

#: Each job's Podman store on its node, below ``$SLURM_TMPDIR`` or ``/tmp``.
STORE_PREFIX = "ale-slurm-"
#: In each store, the ID of the job it belongs to.
OWNER = "job"


def output(*argv: str) -> str:
    return subprocess.check_output(argv, text=True).strip()


def write(name: str, value: object) -> None:
    path = Path(name + ".tmp")
    path.write_text(json.dumps(value))
    path.replace(name)


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


def network(command: list[str], *, sealed: bool) -> None:
    namespace = output(*command, "inspect", "-f", "{{.NetworkSettings.SandboxKey}}", "sandbox")
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


def job_cgroup() -> Path:
    relative = next(
        line[3:]
        for line in Path("/proc/self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    path = Path("/sys/fs/cgroup") / relative.lstrip("/")
    return next(
        parent
        for parent in (path, *path.parents)
        if parent.name == "job_" + os.environ["SLURM_JOB_ID"]
    )


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


def start(directory: str) -> None:
    spec = json.loads(Path("request.json").read_text())
    job = job_cgroup()
    if (
        int((job / "memory.max").read_text()) != spec["job_memory_mb"] << 20
        or (job / "memory.swap.max").read_text().strip() != "0"
        or len(os.sched_getaffinity(0)) != spec["cpus"]
    ):
        raise RuntimeError(
            "Slurm did not enforce the requested CPU, memory and zero-swap allocation"
        )
    command = podman(directory)
    archive = spec["image"].removeprefix("sha256:") + ".tar.gz"
    image = spec["loaded"].removeprefix("sha256:")
    subprocess.run([*command, "load", "-i", str(Path.cwd().parent / archive)], check=True)
    runsc = str(runtime_binary(command, spec["version"]))
    version = output(runsc, "--version").splitlines()[0]
    if not version.endswith("+ale." + spec["version"]):
        raise RuntimeError(f"unexpected gVisor build {version}")
    gpus = devices() if spec["gpus"] else []
    if len(gpus) != spec["gpus"]:
        raise RuntimeError(f"received {len(gpus)} GPUs, requested {spec['gpus']}")
    flags = [
        *command,
        "run",
        "-d",
        "--name",
        "sandbox",
        "--network=slirp4netns:allow_host_loopback=false",
        "--http-proxy=false",
        "--runtime",
        runsc,
        "--runtime-flag=ignore-cgroups",
        "--runtime-flag=enclosing-memory-cgroup=" + str(job),
        "--oom-score-adj=500",
        *spec["runtime_args"],
    ]
    if gpus:
        flags += ["--runtime-flag=nvproxy", "-e", "NVIDIA_VISIBLE_DEVICES=void"]
        for gpu in gpus:
            flags.extend(["--device", "nvidia.com/gpu=" + str(gpu["id"])])
    for key, value in spec["env"].items():
        flags.extend(["--env", f"{key}={value}"])
    subprocess.run([*flags, image], check=True)
    observed = output(*command, "inspect", "--format", "{{.Image}}", "sandbox").removeprefix(
        "sha256:"
    )
    if observed != image:
        raise RuntimeError("Podman started a different image")
    if spec["sealed"]:
        network(command, sealed=True)
    write(
        "ready",
        dict(
            directory=directory,
            runtime_flags=[
                flag for flag in spec["runtime_args"] if flag.startswith("--runtime-flag=")
            ],
            image="sha256:" + observed,
            devices=gpus,
            runtime=version,
            node=os.uname().nodename,
        ),
    )
    while not Path("stop").read_bytes():
        if output(*command, "inspect", "-f", "{{.State.Running}}", "sandbox") != "true":
            raise RuntimeError("the sandbox image command exited")
        time.sleep(2)


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


def read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def release(command: list[str], directory: str) -> None:
    """Remove a store and every mount Podman made below it."""
    subprocess.run(
        [*command, "unshare", "python3", "-I", __file__, "release", directory], check=True
    )
    # Podman sets up an empty store at its configured path again as it exits.
    subprocess.run(["rm", "-rf", "--one-file-system", "--", directory], check=True)


def supervise() -> None:
    # The batch step outlives the sandbox step's cgroup, including an OOM that kills
    # every process there. Keep the terminal counters before the allocation goes away.
    scratch = Path(os.getenv("SLURM_TMPDIR") or "/tmp")
    directory = tempfile.mkdtemp(prefix=STORE_PREFIX, dir=scratch)
    (Path(directory) / OWNER).write_text(os.environ["SLURM_JOB_ID"])
    command = podman(directory)

    def terminated(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    # Slurm signals the batch step before it kills it, as a job is cancelled or preempted:
    # clean up then too.
    signal.signal(signal.SIGTERM, terminated)
    try:
        # Keep the private rootless user namespace alive until the batch step finishes
        # cleanup, including when Slurm kills the entire workload step.
        subprocess.run([*command, "unshare", "true"], check=True)
        for store in stale_stores(scratch):
            subprocess.run(
                [*command, "unshare", "rm", "-rf", "--one-file-system", "--", str(store)],
                check=False,
            )
        subprocess.run(
            ["srun", "--cpu-bind=threads", "python3", "-I", __file__, "start", directory],
            check=True,
        )
    finally:
        Path("memory").write_text((job_cgroup() / "memory.events").read_text())
        try:
            subprocess.run([*command, "rm", "-aft", "1"], check=True)
            release(command, directory)
        except Exception as exc:
            write("cleanup-error", str(exc))
            raise


def main() -> None:
    mode = sys.argv[1]
    if mode == "supervise":
        supervise()
        return
    if mode == "release":
        directory = Path(sys.argv[2]).resolve()
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
            start(sys.argv[2])
        except Exception as exc:
            write("error", str(exc))
            raise
        return
    ready = json.loads(Path("ready").read_text())
    command = podman(ready["directory"])
    if mode in ("block", "open"):
        network(command, sealed=mode == "block")
    elif mode == "exec":
        os.execvp(
            command[0],
            [
                *command,
                *ready["runtime_flags"],
                "exec",
                "-i",
                "--user",
                "0",
                "--workdir",
                "/",
                "sandbox",
                *sys.argv[2:],
            ],
        )
    else:
        raise ValueError(mode)


if __name__ == "__main__":
    main()
