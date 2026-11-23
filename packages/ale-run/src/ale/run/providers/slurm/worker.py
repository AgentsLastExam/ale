"""A single-node Slurm allocation and the Sandboxes in it, or the job that prepares an image
for them. Copied to the cluster, standard library only.

The batch step holds Podman's private namespace and store on the node and loads the image
the allocation was seeded with. Each Sandbox the engine asks for runs as a job step of its
own, which Slurm holds to that Sandbox's CPUs, memory and GPUs; control steps overlap it.
A Sandbox saved as a snapshot is committed to the store, where the next one boots from it.

A preparation job pulls a Task's image, or builds it from its context, adds the framework
layer and saves the result to the shared cache, from which every allocation loads it.

A Sandbox's volumes are layers in the store: a Podman volume of its own, which Podman fills
from the image, a copy of a snapshot's layer, or an overlay view of a snapshot's layers under
an empty layer of its own, mounted in Podman's namespace, where its containers see it. A
snapshot keeps the layer that holds each one's writes. Every layer has a short link below
the store, by which an overlay's options name a stack of hundreds in a page.
"""

from __future__ import annotations

import csv
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SANDBOXES = Path("sandboxes")
SNAPSHOT_REPOSITORY = "localhost/ale-snapshot"
LOADED_REPOSITORY = "localhost/ale-image"
#: Below the cache: the images prepared for the cluster, each by its key.
IMAGES = "images"
#: Each allocation's Podman store on its node, below ``$SLURM_TMPDIR`` or ``/tmp``.
STORE_PREFIX = "ale-slurm-"
#: In each store, the ID of the job it belongs to.
OWNER = "job"
#: Below each store: plain layers, overlays' work directories, views' mount points, and a
#: short link to every layer, a Podman volume's included.
LAYERS, WORK, VIEWS, LINKS = "v", "w", "m", "l"
#: Copies a layer as a whole, its root's owner and mode included: reflinked where the
#: filesystem can.
COPY = (
    'cp -a --reflink=auto "$1"/. "$2"/ '
    '&& chown --reference="$1" "$2" && chmod --reference="$1" "$2"'
)
#: How tar keeps an overlay layer's whiteouts and opaque directories, and its owners.
TAR = ["tar", "--xattrs", "--xattrs-include=user.*", "--numeric-owner"]


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


def layer_id(index: str, number: int) -> str:
    """A layer's name, unique beyond its allocation, whose snapshot another may load."""
    return f"{Path.cwd().name.removeprefix('ale-')[:6]}.{index}.{number}"


def mount_volumes(
    command: list[str], directory: str, index: str, specs: list[dict[str, object]]
) -> tuple[list[str], list[dict[str, object]]]:
    """Make a Sandbox's volumes in the store; return the arguments that mount them and what
    each is: its path, the layer that holds its writes, the layers it views below that, and
    whether it is an engine's storage."""
    store = Path(directory)
    for part in (LAYERS, WORK, VIEWS, LINKS):
        (store / part).mkdir(exist_ok=True)
    args: list[str] = []
    mounts: list[dict[str, object]] = []
    namespaced: list[list[object]] = []
    for number, spec in enumerate(specs):
        layer = layer_id(index, number)
        path, kind = str(spec["path"]), spec["kind"]
        lowers: list[str] = []
        if kind == "fresh":
            name = "ale-" + layer
            subprocess.run(
                [*command, "volume", "create", name], check=True, stdout=subprocess.DEVNULL
            )
            (store / LINKS / layer).symlink_to(f"../volumes/{name}/_data")
            args.append(f"--volume={name}:{path}")
        else:
            (store / LAYERS / layer).mkdir()
            (store / LINKS / layer).symlink_to(f"../{LAYERS}/{layer}")
            if kind == "copy":
                namespaced.append(["copy", spec["layers"][0], layer])
                args.append(f"--volume={store / LAYERS / layer}:{path}")
            else:
                lowers = list(spec["layers"])
                (store / WORK / layer).mkdir()
                (store / VIEWS / layer).mkdir()
                namespaced.append(["view", layer, spec["root"], lowers])
                args.append(f"--volume={store / VIEWS / layer}:{path}")
        mounts.append(dict(path=path, layer=layer, lowers=lowers, copied=bool(spec.get("copied"))))
    if namespaced:
        in_namespace(command, "volumes", directory, json.dumps(namespaced))
    return args, mounts


def in_namespace(command: list[str], mode: str, *args: str) -> str:
    """Run this file's ``mode`` in Podman's namespace, where its mounts and owners are; its
    output."""
    return output(*command, "unshare", "python3", "-I", __file__, mode, *args)


def directories(directory: str, image: str, paths: list[str]) -> list[str]:
    """In Podman's namespace: which of ``paths`` are directories in ``image``, read from the
    image's own files, so nothing of it runs."""
    command = podman(directory)
    root = output(*command, "image", "mount", image)
    try:
        return directories_in(root, paths)
    finally:
        subprocess.run(
            [*command, "image", "unmount", image], check=False, stdout=subprocess.DEVNULL
        )


def directories_in(root: str, paths: list[str]) -> list[str]:
    """Which of the absolute ``paths`` are directories below ``root``, with no link on their
    way."""
    found = []
    for path in paths:
        current = root
        for part in Path(path).parts[1:]:
            current = os.path.join(current, part)
            try:
                if not stat.S_ISDIR(os.lstat(current).st_mode):
                    break
            except OSError:
                break
        else:
            found.append(path)
    return found


def volumes(directory: str, operations: list[list[object]]) -> None:
    """In Podman's namespace: copy a layer, mount a view whose root gets its stack's owner
    and mode, unmount one, or remove a layer."""
    os.chdir(directory)
    for operation in operations:
        match operation:
            case ["copy", str(source), str(layer)]:
                subprocess.run(
                    ["sh", "-c", COPY, "sh", f"{LINKS}/{source}", f"{LAYERS}/{layer}"],
                    check=True,
                )
            case ["view", str(layer), [int(uid), int(gid), int(mode)], list(lowers)]:
                upper = Path(LAYERS) / layer
                os.chown(upper, uid, gid)
                os.chmod(upper, mode)
                stack = ":".join(f"{LINKS}/{lower}" for lower in lowers)
                subprocess.run(
                    [
                        "mount", "-t", "overlay", "overlay", "-o",
                        f"lowerdir={stack},upperdir={upper},workdir={WORK}/{layer},userxattr",
                        f"{VIEWS}/{layer}",
                    ],
                    check=True,
                )  # fmt: skip
            case ["unmount", str(layer)]:
                if os.path.ismount(f"{VIEWS}/{layer}"):
                    subprocess.run(["umount", f"{VIEWS}/{layer}"], check=True)
                for part in (WORK, VIEWS):
                    subprocess.run(
                        ["rm", "-rf", "--one-file-system", f"{part}/{layer}"], check=True
                    )
            case ["remove", str(layer)]:
                link = Path(LINKS) / layer
                if not link.is_symlink():
                    continue
                target = os.readlink(link)
                if target.startswith("../volumes/"):
                    # A Podman volume: its directory, emptied here, goes with the store.
                    subprocess.run(
                        ["find", str(link) + "/", "-mindepth", "1", "-delete"], check=True
                    )
                else:
                    subprocess.run(
                        ["rm", "-rf", "--one-file-system", f"{LAYERS}/{layer}"], check=True
                    )
                link.unlink()
            case _:
                raise ValueError(operation)


def root_of(command: list[str], directory: str, layer: str) -> list[int]:
    """The owner and mode of a layer's root, as the Sandbox saw them."""
    found = output(
        *command, "unshare", "stat", "-L", "-c", "%u %g %a", f"{directory}/{LINKS}/{layer}"
    )
    uid, gid, mode = found.split()
    return [int(uid), int(gid), int(mode, 8)]


def load_layers(command: list[str], directory: str, layers: dict[str, str]) -> None:
    """Unpack a written-out snapshot's layers, below the cache, into the store."""
    store = Path(directory)
    for part in (LAYERS, WORK, VIEWS, LINKS):
        (store / part).mkdir(exist_ok=True)
    for layer, archive in layers.items():
        (store / LAYERS / layer).mkdir()
        (store / LINKS / layer).symlink_to(f"../{LAYERS}/{layer}")
        subprocess.run(
            [*command, "unshare", *TAR, "-xf", str(Path.cwd().parent / archive),
             "-C", str(store / LAYERS / layer)],
            check=True,
        )  # fmt: skip


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


def load(command: list[str], archive: str, image: str) -> None:
    """Load an image's archive, below the cache, into the store, and tag it: Podman removes
    an untagged image with the last snapshot committed over it, which a Sandbox of the
    image, a separate verifier's, may start after."""
    subprocess.run(
        [*command, "load", "-q", "-i", str(Path.cwd().parent / archive)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    if output(*command, "image", "inspect", "-f", "{{.Id}}", image) != image:
        raise RuntimeError("the staged archive holds a different image")
    subprocess.run([*command, "tag", image, f"{LOADED_REPOSITORY}:{image[:12]}"], check=True)


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


def terminated(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)


def store() -> tuple[str, list[str], Path]:
    """This job's own Podman store on its node, and the command that uses it; the stores of
    this account's jobs that ended on the node are removed first."""
    scratch = Path(os.getenv("SLURM_TMPDIR") or "/tmp")
    directory = tempfile.mkdtemp(prefix=STORE_PREFIX, dir=scratch)
    (Path(directory) / OWNER).write_text(os.environ["SLURM_JOB_ID"])
    # Slurm signals the batch step before it kills it, as a job is cancelled or preempted:
    # clean up then too.
    signal.signal(signal.SIGTERM, terminated)
    return directory, podman(directory), scratch


def hold_namespace(command: list[str], scratch: Path) -> None:
    """Keep the private rootless user namespace alive until the batch step finishes cleanup,
    including when Slurm kills every Sandbox's step, and remove the stale stores."""
    subprocess.run([*command, "unshare", "true"], check=True)
    for stale in stale_stores(scratch):
        subprocess.run(
            [*command, "unshare", "rm", "-rf", "--one-file-system", "--", str(stale)],
            check=False,
        )


def clean(command: list[str], directory: str) -> None:
    try:
        subprocess.run([*command, "rm", "-a", "-f", "-v", "-t", "1"], check=True)
        release(command, directory)
    except Exception as exc:
        write("cleanup-error", str(exc))
        raise


def prepare() -> None:
    """Pull a Task's image, or build it from its context, add the framework layer the recipe
    describes, and save the result below the cache under the recipe's key; record what was
    prepared in ``prepared``, or in ``error`` which step failed and why."""
    recipe = json.loads(Path("recipe.json").read_text())
    framework = recipe["framework"]
    directory, command, scratch = store()
    archive = Path.cwd().parent / IMAGES / f"{recipe['key']}.tar.gz"
    part = archive.with_name(f"{archive.name}.{os.environ['SLURM_JOB_ID']}.part")
    step = "pull"
    try:
        hold_namespace(command, scratch)
        reference = recipe.get("reference")
        if reference:
            subprocess.run(
                [*command, "pull", "-q", reference], check=True, stdout=subprocess.DEVNULL
            )
            base = output(*command, "image", "inspect", "-f", "{{.Id}}", reference)
            digests = json.loads(
                output(*command, "image", "inspect", "-f", "{{json .RepoDigests}}", reference)
            )
        else:
            step = "build"
            # The context was copied without what the image must not hold; its own
            # .dockerignore still applies.
            subprocess.run(
                [*command, "build", "-q", "--iidfile", "built", "--file", "context/Dockerfile",
                 "context"],
                check=True,
                stdout=subprocess.DEVNULL,
            )  # fmt: skip
            base, digests = read("built"), []
        step = "framework"
        base = "sha256:" + base.removeprefix("sha256:")
        tag = "localhost/ale-framework-base:" + base.removeprefix("sha256:")[:12]
        subprocess.run([*command, "tag", base, tag], check=True)
        user = output(*command, "image", "inspect", "-f", "{{.Config.User}}", base)
        lines = [f"FROM {tag}", *framework["body"], *([f"USER {user}"] if user else [])]
        Path("Framework.Dockerfile").write_text("\n".join([*lines, *framework["tail"]]) + "\n")
        subprocess.run(
            [*command, "build", "-q", "--iidfile", "framed",
             "--build-context", "python=" + framework["python"],
             "--file", "Framework.Dockerfile", framework["context"]],
            check=True,
            stdout=subprocess.DEVNULL,
        )  # fmt: skip
        framed = "sha256:" + read("framed").removeprefix("sha256:")
        step = "save"
        archive.parent.mkdir(mode=0o700, exist_ok=True)
        with part.open("wb") as file:
            saver = subprocess.Popen([*command, "save", framed], stdout=subprocess.PIPE)
            assert saver.stdout is not None
            gzip = subprocess.run(["gzip", "-1"], stdin=saver.stdout, stdout=file, check=False)
            saver.stdout.close()
            if saver.wait() or gzip.returncode:
                raise RuntimeError(f"could not save the prepared image {framed}")
        part.replace(archive)
        write("prepared", dict(image=framed, base=base, repo_digests=digests))
    except BaseException as exc:
        write("failed", step)
        if isinstance(exc, SystemExit):
            write("error", f"{step}: the job was ended")
        raise
    finally:
        part.unlink(missing_ok=True)
        clean(command, directory)


def supervise() -> None:
    spec = json.loads(Path("allocation.json").read_text())
    directory, command, scratch = store()
    steps: dict[str, subprocess.Popen[bytes]] = {}
    try:
        hold_namespace(command, scratch)
        load(command, spec["archive"], spec["image"])
        load_layers(command, directory, spec.get("layers") or {})
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
        clean(command, directory)


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
    mounts: list[dict[str, object]] = []
    kept: set[str] = set()
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
        f"--shm-size={spec['shm_mb']}m",
        *spec["runtime_args"],
    ]
    if gpus:
        flags += ["--runtime-flag=nvproxy", "-e", "NVIDIA_VISIBLE_DEVICES=void"]
        for gpu in gpus:
            flags.extend(["--device", "nvidia.com/gpu=" + str(gpu["id"])])
    for key, value in spec["env"].items():
        flags.extend(["--env", f"{key}={value}"])
    try:
        # A candidate is a volume only where the image has a directory.
        candidates = spec.get("candidates") or []
        found = (
            json.loads(
                in_namespace(
                    command,
                    "directories",
                    ready["directory"],
                    spec["image"],
                    json.dumps(candidates),
                )
            )
            if candidates
            else []
        )
        arguments, mounts = mount_volumes(
            command,
            ready["directory"],
            index,
            [dict(path=path, kind="fresh") for path in found] + (spec.get("volumes") or []),
        )
        subprocess.run([*flags, *arguments, spec["image"]], check=True, stdout=subprocess.DEVNULL)
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
                volumes=found,
            ),
        )
        while True:
            order = read(box / "stop")
            if order == "save":
                kept = save(command, ready["directory"], name, index, box, mounts)
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
        if mounts:
            # Its views and the layers no snapshot of it keeps go with it.
            in_namespace(
                command,
                "volumes",
                ready["directory"],
                json.dumps(
                    [["unmount", m["layer"]] for m in mounts if m["lowers"]]
                    + [["remove", m["layer"]] for m in mounts if m["layer"] not in kept]
                ),
            )


def save(
    command: list[str],
    directory: str,
    name: str,
    index: str,
    box: Path,
    mounts: list[dict[str, object]],
) -> set[str]:
    """Stop the Sandbox and commit its disk to the allocation's store, and record each
    volume's stack: the layer that holds its writes on what it viewed. Return the layers the
    snapshot keeps."""
    subprocess.run([*command, "stop", "-t", "30", name], check=True, stdout=subprocess.DEVNULL)
    image = output(*command, "commit", "-q", name, f"{SNAPSHOT_REPOSITORY}:{index}")
    stacks = {
        str(m["path"]): dict(
            layers=[m["layer"], *m["lowers"]],
            root=root_of(command, directory, str(m["layer"])),
            copied=m["copied"],
        )
        for m in mounts
    }
    if any(m["lowers"] for m in mounts):
        # Before the Host hears of the snapshot: a view's upper layer is no lower of
        # another view while it is mounted.
        in_namespace(
            command,
            "volumes",
            directory,
            json.dumps([["unmount", m["layer"]] for m in mounts if m["lowers"]]),
        )
    write(box / "saved", dict(image=image.removeprefix("sha256:"), stacks=stacks))
    return {str(m["layer"]) for m in mounts}


def export(
    command: list[str], directory: str, image: str, destination: str, layers: list[str]
) -> None:
    """Write a snapshot to the shared file system, from which another allocation loads it:
    its image, and each of its volumes' ``layers`` beside it, ``<destination
    stem>.<layer>.tar``. Neither is compressed, as one gzip process is the slowest part:
    see this Provider's AGENTS.md."""
    path = Path.cwd().parent / destination
    path.parent.mkdir(mode=0o700, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("wb") as file:
        if subprocess.run([*command, "save", image], stdout=file, check=False).returncode:
            temporary.unlink()
            raise RuntimeError(f"could not export {image}")
    for layer in layers:
        archive = path.with_name(layer_archive(path.name, layer))
        part = archive.with_name(archive.name + ".part")
        subprocess.run(
            [*command, "unshare", *TAR, "-cf", str(part), "-C", f"{directory}/{LINKS}/{layer}/",
             "."],
            check=True,
        )  # fmt: skip
        part.replace(archive)
    temporary.replace(path)


def layer_archive(image_archive: str, layer: str) -> str:
    """The name of a written-out snapshot's layer, beside its image's archive."""
    return f"{image_archive.removesuffix('.tar')}.{layer}.tar"


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
    if mode == "prepare":
        try:
            prepare()
        except Exception as exc:
            write("error", str(exc))
            raise
        return
    if mode == "volumes":
        volumes(args[0], json.loads(args[1]))
        return
    if mode == "directories":
        print(json.dumps(directories(args[0], args[1], json.loads(args[2]))))
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
        export(command, ready["directory"], args[0], args[1], args[2:])
    elif mode == "load":
        load(command, args[0], args[1])
    elif mode == "drop":
        in_namespace(
            command, "volumes", ready["directory"], json.dumps([["remove", a] for a in args])
        )
    else:
        raise ValueError(mode)


if __name__ == "__main__":
    main()
