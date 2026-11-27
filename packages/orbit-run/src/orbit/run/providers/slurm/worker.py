"""A Slurm allocation and the Sandboxes in it, or the job that prepares an image for them.
Copied to the cluster, standard library only.

On each node of the allocation, the batch step on the first and a step of its own on every
other hold Podman's private namespace and store there and load the image that node was
seeded with. Each Sandbox the engine asks for runs as a job step of its own over the first
nodes it spans, one task on each, which Slurm holds to that Sandbox's CPUs, memory and GPUs
on each; control steps overlap it. A Sandbox saved as a snapshot is committed to the store
of each of its nodes, where the next one boots from it.

The nodes of a Sandbox that spans several share a network of their own: on each node a
router namespace, which only this worker enters, holds a WireGuard device to the other
nodes' routers and forwards between it and the Sandbox's own namespace only the group's
traffic, so nothing the Sandbox does to its own network reaches anything else. Each node
gets the node's InfiniBand HCAs, which gVisor's RDMA proxy drives, and the Sandbox's shared
directory, a directory of the shared file system at ``/shared``.

A preparation job pulls a Task's image, or builds it from its context, adds the framework
layer and saves the result to the shared cache, from which every allocation loads it.

A Sandbox's volumes are layers in the store: a Podman volume of its own, which Podman fills
from the image, a copy of a snapshot's layer, or an overlay view of a snapshot's layers under
an empty layer of its own, mounted in Podman's namespace, where its containers see it. A
snapshot keeps the layer that holds each one's writes. Every layer has a short link below
the store, by which an overlay's options name a stack of hundreds in a page.
"""

from __future__ import annotations

import contextlib
import csv
import fcntl
import json
import os
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SANDBOXES = Path("sandboxes")
#: Below the job's directory: each node's store and runtime, by the node's Slurm name.
STORES = Path("stores")
#: Below a Sandbox's directory: one directory for each of its nodes, by its number there,
#: and what each node's router offers the others.
NODES, PEERS = "nodes", "peers"
#: The network of a Sandbox's nodes: node ``k`` is ``GROUP + str(k + 1)``. The interface
#: that carries it in the Sandbox, the router's end of it, and the router's tunnel.
GROUP, GROUP_INTERFACE, ROUTER_INTERFACE, TUNNEL = "10.214.0.", "peers", "peers-router", "wg0"
#: The nodes' network's MTU: with WireGuard's 80 bytes, a jumbo frame of 9000, well within
#: pasta's 65520; measured through pasta, 0.28 GB/s against 0.12 at the default 1420.
GROUP_MTU = 8920
#: Where every node of a Sandbox that spans several mounts its shared directory.
SHARED = "/shared"
#: How many files of the shared file system a copy, a removal or a measurement handles at
#: once: each waits for the server.
FILES_AT_ONCE = 32
#: How long a node waits for the others: their routers' keys, or their stores.
GROUP_TIMEOUT_SEC = 600
SNAPSHOT_REPOSITORY = "localhost/orbit-snapshot"
LOADED_REPOSITORY = "localhost/orbit-image"
#: Below the cache: the images prepared for the cluster, each by its key.
IMAGES = "images"
#: Each allocation's Podman store on its node, below its configured directory.
STORE_PREFIX = "orbit-slurm-"
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


def job_nodes() -> list[str]:
    """The job's nodes, in the order Slurm lists them: a Sandbox of ``n`` spans the first."""
    return output("scontrol", "show", "hostnames", os.environ["SLURM_JOB_NODELIST"]).split()


def this_node() -> str:
    return os.environ["SLURMD_NODENAME"]


def node_store() -> dict[str, str]:
    """This node's store and runtime, which the step that holds them recorded."""
    return json.loads(Path(STORES / f"{this_node()}.json").read_text())


def appeared(path: Path) -> bool:
    """Whether ``path``, which another node may have just made, exists: as an NFS client
    trusts a cached listing of a directory, its directory is listed first."""
    try:
        os.listdir(path.parent)
    except OSError:
        return False
    return path.exists()


def node_address() -> str:
    """The address Slurm reaches this node at, which the other nodes reach it at too."""
    shown = output("scontrol", "show", "node", "-o", this_node())
    fields = dict(part.partition("=")[::2] for part in shown.split() if "=" in part)
    return socket.gethostbyname(fields.get("NodeAddr") or this_node())


def infiniband(root: Path = Path("/sys/class/infiniband")) -> list[str]:
    """The node's verbs devices of HCAs with an active InfiniBand port: never an Ethernet
    one, whose RoCE traffic would leave by the node's own network."""
    found = []
    for hca in sorted(root.glob("*")):
        if any(
            read(port / "link_layer") == "InfiniBand" and read(port / "state").endswith("ACTIVE")
            for port in (hca / "ports").glob("*")
        ):
            found.extend(
                "/dev/infiniband/" + verbs.name
                for verbs in sorted((hca / "device" / "infiniband_verbs").glob("uverbs*"))
            )
    return found


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
    return f"{Path.cwd().name.removeprefix('orbit-')[:6]}.{index}.{number}"


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
            name = "orbit-" + layer
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


def account(directory: str, image: str, user: str) -> str:
    """In Podman's namespace: ``user``'s uid and gid in ``image``, read from its own
    ``/etc/passwd``."""
    command = podman(directory)
    root = output(*command, "image", "mount", image)
    try:
        passwd = Path(root, "etc", "passwd")
        if not directories_in(root, ["/etc"]) or passwd.is_symlink():
            raise RuntimeError("the image's /etc/passwd is no file of its own")
        for line in passwd.read_text().splitlines():
            fields = line.split(":")
            if len(fields) > 3 and fields[0] == user:
                return f"{fields[2]}:{fields[3]}"
    finally:
        subprocess.run(
            [*command, "image", "unmount", image], check=False, stdout=subprocess.DEVNULL
        )
    raise RuntimeError(f"the image has no account {user}")


def listing(
    root: Path, pool: ThreadPoolExecutor, *, restore: bool = True
) -> list[tuple[Path, os.stat_result]]:
    """Every entry below ``root`` and its own status, breadth-first, many directories listed
    at once, on ``root``'s file system alone: no link is followed, and an entry that goes
    meanwhile is left out. A directory its owner may not list or search is opened for the
    walk and, with ``restore``, given its mode back after it."""
    device = os.lstat(root).st_dev
    opened: list[tuple[Path, int]] = []

    def entries(directory: Path) -> list[tuple[Path, os.stat_result]]:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            found = []
            for entry in os.scandir(descriptor):
                with contextlib.suppress(FileNotFoundError):
                    found.append((directory / entry.name, entry.stat(follow_symlinks=False)))
            return found
        finally:
            os.close(descriptor)

    def scan(directory: Path) -> list[tuple[Path, os.stat_result]]:
        try:
            return entries(directory)
        except PermissionError:
            # A directory, as no link opens without O_NOFOLLOW failing otherwise.
            mode = os.lstat(directory).st_mode
            os.chmod(directory, mode | stat.S_IRWXU)
            opened.append((directory, stat.S_IMODE(mode)))
            return entries(directory)
        except (FileNotFoundError, NotADirectoryError):
            return []

    found: list[tuple[Path, os.stat_result]] = []
    pending = [pool.submit(scan, root)]
    try:
        while pending:
            level, pending = pending, []
            for future in level:
                for path, status in future.result():
                    found.append((path, status))
                    if stat.S_ISDIR(status.st_mode) and status.st_dev == device:
                        pending.append(pool.submit(scan, path))
    finally:
        for directory, mode in reversed(opened) if restore else ():
            with contextlib.suppress(FileNotFoundError):
                os.chmod(directory, mode)
    return found


def copy_tree(source: str, destination: str) -> None:
    """Copy a Sandbox's shared directory, on the shared file system, whole: modes, times,
    links and hard links, as the account that owns every file there. On a network file
    system each file waits for the server, so many go at once."""
    src, dst = Path(source), Path(destination)
    with ThreadPoolExecutor(FILES_AT_ONCE) as pool:
        entries = listing(src, pool)
        dst.mkdir(mode=0o700)
        directories = [(path, status) for path, status in entries if stat.S_ISDIR(status.st_mode)]
        depth = {path: len(path.relative_to(src).parts) for path, _ in directories}
        for level in sorted(set(depth.values())):
            list(pool.map(lambda path: (dst / path.relative_to(src)).mkdir(),
                          [path for path in depth if depth[path] == level]))  # fmt: skip
        first: dict[tuple[int, int], Path] = {}
        linked: list[tuple[Path, Path]] = []
        placed: list[tuple[Path, os.stat_result]] = []
        for path, status in entries:
            if stat.S_ISREG(status.st_mode) and status.st_nlink > 1:
                key = (status.st_dev, status.st_ino)
                if key in first:
                    linked.append((first[key], path))
                    continue
                first[key] = path
            if not stat.S_ISDIR(status.st_mode):
                placed.append((path, status))

        def place(item: tuple[Path, os.stat_result]) -> None:
            path, status = item
            target = dst / path.relative_to(src)
            if stat.S_ISREG(status.st_mode):
                try:
                    shutil.copyfile(path, target)
                except PermissionError:
                    os.chmod(path, status.st_mode | stat.S_IRUSR)
                    shutil.copyfile(path, target)
                    os.chmod(path, status.st_mode)
                os.chmod(target, stat.S_IMODE(status.st_mode))
            elif stat.S_ISLNK(status.st_mode):
                os.symlink(os.readlink(path), target)
            elif stat.S_ISFIFO(status.st_mode):
                os.mkfifo(target, stat.S_IMODE(status.st_mode))
            else:
                return  # A socket or device, which only lives with its process or node.
            os.utime(target, ns=(status.st_atime_ns, status.st_mtime_ns), follow_symlinks=False)

        list(pool.map(place, placed))
        for existing, path in linked:
            os.link(dst / existing.relative_to(src), dst / path.relative_to(src))
        for path, status in sorted(directories, key=lambda item: -depth[item[0]]):
            target = dst / path.relative_to(src)
            os.chmod(target, stat.S_IMODE(status.st_mode))
            os.utime(target, ns=(status.st_atime_ns, status.st_mtime_ns))
        root = os.stat(src)
        os.chmod(dst, stat.S_IMODE(root.st_mode))


def remove_tree(path: str) -> None:
    """Remove a directory of the shared file system whole, many files at once."""
    root = Path(path)
    if not root.exists():
        return

    def gone(remove: Callable[[Path], object], each: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            remove(each)

    with ThreadPoolExecutor(FILES_AT_ONCE) as pool:
        entries = listing(root, pool, restore=False)
        # Each directory's entries go only where its owner may change it.
        locked = [
            (path, status.st_mode)
            for path, status in [(root, root.stat()), *entries]
            if stat.S_ISDIR(status.st_mode) and status.st_mode & stat.S_IRWXU != stat.S_IRWXU
        ]
        list(pool.map(lambda item: os.chmod(item[0], item[1] | stat.S_IRWXU), locked))
        files = [path for path, status in entries if not stat.S_ISDIR(status.st_mode)]
        list(pool.map(lambda each: gone(Path.unlink, each), files))
        depth: dict[int, list[Path]] = {}
        for each, status in entries:
            if stat.S_ISDIR(status.st_mode):
                depth.setdefault(len(each.relative_to(root).parts), []).append(each)
        for level in sorted(depth, reverse=True):
            list(pool.map(lambda each: gone(os.rmdir, each), depth[level]))
    gone(os.rmdir, root)


def usage(paths: list[str]) -> int:
    """The disk the files below ``paths`` take, in bytes, each file once: its blocks, as
    ``du`` counts them."""
    seen: set[tuple[int, int]] = set()
    total = 0
    with ThreadPoolExecutor(FILES_AT_ONCE) as pool:
        for path in paths:
            # Orbit's own path, which may name a Podman volume through a link of the store.
            real = Path(os.path.realpath(path))
            try:
                top = os.lstat(real)
            except FileNotFoundError:
                continue
            entries = [(real, top), *(listing(real, pool) if stat.S_ISDIR(top.st_mode) else [])]
            for _, status in entries:
                key = (status.st_dev, status.st_ino)
                if key not in seen:
                    seen.add(key)
                    total += status.st_blocks * 512
    return total


def measure(command: list[str], paths: list[str]) -> int:
    """What the files below ``paths`` take, read in Podman's namespace, where every owner's
    files are readable."""
    return int(in_namespace(command, "measure", json.dumps(paths))) if paths else 0


def interval(schedule: dict[str, float], headroom: int) -> float:
    """How long until the next measurement of a Sandbox with ``headroom`` bytes left below
    its limit: as long as it takes to write them at the fastest rate a disk takes, within
    bounds, so that one far below its limit is rarely measured (``providers/storage.py``)."""
    return min(max(headroom / schedule["rate"], schedule["min_sec"]), schedule["max_sec"])


def volumes(directory: str, operations: list[list[object]]) -> None:
    """In Podman's namespace: copy a layer, mount a view whose root gets its stack's owner
    and mode, merge a mounted view into one layer, unmount one, or remove a layer."""
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
            case ["merge", str(view), str(layer)]:
                # A view as it is, its stack's whiteouts applied, as one plain layer.
                subprocess.run(
                    ["sh", "-c", COPY, "sh", f"{VIEWS}/{view}", f"{LAYERS}/{layer}"], check=True
                )
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
    """Seal or open the network namespace of the container ``name``, which holds a Sandbox
    or, for one of several nodes, its pod's: drop or pass every packet of its slirp4netns
    tap, outside gVisor. The group's interface, whose router passes only the group's
    traffic, stays as it is."""
    namespace = output(*command, "inspect", "-f", "{{.NetworkSettings.SandboxKey}}", name)
    net = [*command, "unshare", "nsenter", "--net=" + namespace]
    # Slirp4netns gives this container one tap. gVisor has taken its routes into netstack.
    interfaces = [
        link["ifname"]
        for link in json.loads(output(*net, "ip", "-j", "link"))
        if link["ifname"] not in ("lo", GROUP_INTERFACE)
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


#: X25519's prime, for a WireGuard key pair (RFC 7748).
_P = 2**255 - 19


def public_key(private: bytes) -> bytes:
    """The X25519 public key of a private one, as WireGuard derives it."""
    clamped = bytearray(private)
    clamped[0] &= 248
    clamped[31] = (clamped[31] & 127) | 64
    scalar = int.from_bytes(clamped, "little")
    x2, z2, x3, z3, swap = 1, 0, 9, 1, 0
    for bit in (scalar >> t & 1 for t in reversed(range(255))):
        if swap ^ bit:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b, c, d = x2 + z2, x2 - z2, x3 + z3, x3 - z3
        aa, bb, da, cb = a * a, b * b, d * a, c * b
        e = aa - bb
        x3, z3 = (da + cb) ** 2 % _P, 9 * (da - cb) ** 2 % _P
        x2, z2 = aa * bb % _P, e * (aa + 121665 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def _attribute(kind: int, payload: bytes, *, nested: bool = False) -> bytes:
    data = struct.pack("=HH", 4 + len(payload), kind | (0x8000 if nested else 0)) + payload
    return data + b"\0" * (-len(data) % 4)


def _netlink(
    sock: socket.socket, family: int, command: int, payload: bytes, sequence: int
) -> bytes:
    """One generic netlink request, acknowledged: its reply, if it has one, read up to its
    acknowledgement, so that the next request reads its own; or the error it got."""
    body = struct.pack("=BBH", command, 1, 0) + payload
    # NLM_F_REQUEST | NLM_F_ACK
    sock.send(struct.pack("=IHHII", 16 + len(body), family, 5, sequence, 0) + body)
    answer = b""
    while True:
        data, offset = sock.recv(65536), 0
        while offset + 16 <= len(data):
            length, kind, _, seen = struct.unpack_from("=IHHI", data, offset)
            message = data[offset : offset + length]
            offset += (length + 3) & ~3
            if seen != sequence:
                continue
            if kind != 2:  # NLMSG_ERROR, which is also how the acknowledgement comes
                answer = message
                continue
            (code,) = struct.unpack_from("=i", message, 16)
            if code:
                raise OSError(-code, f"netlink: {os.strerror(-code)}")
            return answer


def wireguard(config: dict[str, object]) -> None:
    """In the router's network namespace: give its WireGuard device its key and port, and the
    other nodes as its peers, each by its router's key and endpoint and the one address it may
    send from. Over generic netlink, as the nodes have no ``wg``."""
    peers = b"".join(
        _attribute(
            number,
            _attribute(1, bytes.fromhex(str(peer["key"])))  # WGPEER_A_PUBLIC_KEY
            + _attribute(
                4,  # WGPEER_A_ENDPOINT, a sockaddr_in
                struct.pack("=H", socket.AF_INET)
                + struct.pack("!H", int(peer["port"]))
                + socket.inet_aton(str(peer["host"]))
                + bytes(8),
            )
            + _attribute(5, struct.pack("=H", 25))  # WGPEER_A_PERSISTENT_KEEPALIVE_INTERVAL
            + _attribute(
                9,  # WGPEER_A_ALLOWEDIPS
                _attribute(
                    0,
                    _attribute(1, struct.pack("=H", socket.AF_INET))
                    + _attribute(2, socket.inet_aton(str(peer["address"])))
                    + _attribute(3, struct.pack("=B", 32)),
                    nested=True,
                ),
                nested=True,
            ),
            nested=True,
        )
        for number, peer in enumerate(config["peers"])  # type: ignore[arg-type]
    )
    payload = (
        _attribute(2, TUNNEL.encode() + b"\0")  # WGDEVICE_A_IFNAME
        + _attribute(3, bytes.fromhex(str(config["private"])))  # WGDEVICE_A_PRIVATE_KEY
        + _attribute(6, struct.pack("=H", int(config["port"])))  # type: ignore[arg-type]
        + _attribute(5, struct.pack("=I", 1))  # WGDEVICE_F_REPLACE_PEERS
        + _attribute(8, peers, nested=True)  # WGDEVICE_A_PEERS
    )
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16) as sock:  # NETLINK_GENERIC
        sock.bind((0, 0))
        # CTRL_CMD_GETFAMILY of the controller (GENL_ID_CTRL), by CTRL_ATTR_FAMILY_NAME.
        reply = _netlink(sock, 0x10, 3, _attribute(2, b"wireguard\0"), 1)
        offset, family = 20, 0
        while offset < len(reply):
            length, kind = struct.unpack_from("=HH", reply, offset)
            if kind == 1:  # CTRL_ATTR_FAMILY_ID
                (family,) = struct.unpack_from("=H", reply, offset + 4)
            offset += (length + 3) & ~3
        if not family:
            raise RuntimeError("the node's kernel has no WireGuard")
        _netlink(sock, family, 1, payload, 2)  # WG_CMD_SET_DEVICE


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("", 0))
        return int(probe.getsockname()[1])


def infra_pid(command: list[str], pod: str) -> int:
    infra = output(*command, "pod", "inspect", "-f", "{{.InfraContainerID}}", pod)
    return int(output(*command, "inspect", "-f", "{{.State.Pid}}", infra))


def group_up(command: list[str], box: Path, index: str, rank: int, count: int, shm_mb: int) -> str:
    """Give node ``rank`` of the Sandbox ``index`` its share of the group's network, once
    every node offers its router's key and endpoint, and return the pod whose network
    namespace the Sandbox joins: one that holds its slirp4netns tap, as a single node's
    Sandbox has, and the group's interface, whose other end is in the router's namespace.
    A container in a pod gets the pod's ``/dev/shm``, whatever its own ``--shm-size``, so
    the pod has the Sandbox's ``shm_mb``."""
    router, pod = f"router-{index}", f"group-{index}"
    for _ in range(5):
        # The router's uplink, which forwards WireGuard's port from the node.
        port = free_udp_port()
        created = subprocess.run(
            [*command, "pod", "create", "--name", router, "--share", "net",
             "--network", "pasta", "--publish", f"{port}:{port}/udp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False,
        )  # fmt: skip
        if (
            not created.returncode
            and not subprocess.run(
                [*command, "pod", "start", router], stdout=subprocess.DEVNULL, check=False
            ).returncode
        ):
            break
        subprocess.run([*command, "pod", "rm", "-f", router], capture_output=True, check=False)
    else:
        raise RuntimeError(f"could not start the group's router: {created.stderr.strip()}")
    subprocess.run(
        [*command, "pod", "create", "--name", pod, "--share", "net", f"--shm-size={shm_mb}m",
         "--network", "slirp4netns:allow_host_loopback=false",
         *(f"--add-host=node{k}:{GROUP}{k + 1}" for k in range(count))],
        check=True, stdout=subprocess.DEVNULL,
    )  # fmt: skip
    subprocess.run([*command, "pod", "start", pod], check=True, stdout=subprocess.DEVNULL)
    private = os.urandom(32)
    offers = box / PEERS
    offers.mkdir(exist_ok=True)
    write(
        offers / f"{rank}.json", dict(host=node_address(), port=port, key=public_key(private).hex())
    )
    deadline = time.monotonic() + GROUP_TIMEOUT_SEC
    while not all(appeared(offers / f"{k}.json") for k in range(count)):
        if time.monotonic() > deadline:
            raise RuntimeError("the Sandbox's other nodes did not offer their routers")
        time.sleep(0.5)
    config = dict(
        router=infra_pid(command, router),
        sandbox=infra_pid(command, pod),
        rank=rank,
        port=port,
        private=private.hex(),
        peers=[
            dict(json.loads((offers / f"{k}.json").read_text()), address=f"{GROUP}{k + 1}")
            for k in range(count)
            if k != rank
        ],
    )
    # The private key goes by a pipe, never by a command line another account can read.
    subprocess.run(
        [*command, "unshare", "python3", "-I", __file__, "group"],
        input=json.dumps(config).encode(),
        check=True,
    )
    return pod


def group(config: dict[str, object]) -> None:
    """In Podman's namespace: the router's tunnel to the other nodes, the link between the
    router and the Sandbox's namespace, and the router's forwarding, by policy routing
    alone: from the Sandbox only to the group's addresses through the tunnel, from the tunnel
    only to the Sandbox; nothing to the router's uplink."""
    router = ["nsenter", "-t", str(config["router"]), "-n"]
    sandbox = ["nsenter", "-t", str(config["sandbox"]), "-n"]
    own = f"{GROUP}{int(config['rank']) + 1}"  # type: ignore[call-overload]
    mtu = str(GROUP_MTU)

    def run(*argv: str, given: bytes | None = None) -> None:
        subprocess.run(argv, input=given, check=True)

    run(*router, "ip", "link", "add", TUNNEL, "type", "wireguard")
    run(*router, "python3", "-I", __file__, "wireguard", given=json.dumps(config).encode())
    run(*router, "ip", "link", "add", ROUTER_INTERFACE, "type", "veth",
        "peer", "name", GROUP_INTERFACE, "netns", str(config["sandbox"]))  # fmt: skip
    run(*sandbox, "ip", "address", "add", own + "/24", "dev", GROUP_INTERFACE)
    run(*sandbox, "ip", "link", "set", GROUP_INTERFACE, "mtu", mtu, "up")
    for device in (ROUTER_INTERFACE, TUNNEL):
        run(*router, "ip", "link", "set", device, "mtu", mtu, "up")
    # Neither of the router's interfaces has an address its routes reach back through, which
    # a node's strict or loose reverse-path filter, inherited, would take as spoofing.
    run(*router, "sh", "-c", "echo 1 >/proc/sys/net/ipv4/ip_forward && "
        f"echo 1 >/proc/sys/net/ipv4/conf/{ROUTER_INTERFACE}/proxy_arp && "
        "for c in all default " + ROUTER_INTERFACE + " " + TUNNEL + "; do "
        "echo 0 >/proc/sys/net/ipv4/conf/$c/rp_filter; done")  # fmt: skip
    for table, route in (
        ("100", [GROUP + "0/24", "dev", TUNNEL]),
        ("101", [own, "dev", ROUTER_INTERFACE]),
    ):
        run(*router, "ip", "route", "add", *route, "table", table)
        run(*router, "ip", "route", "add", "unreachable", "default", "table", table)
    run(*router, "ip", "rule", "add", "iif", ROUTER_INTERFACE, "lookup", "100", "pref", "100")
    run(*router, "ip", "rule", "add", "iif", TUNNEL, "lookup", "101", "pref", "101")


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


def runtime(command: list[str], version: str, build: str) -> Path:
    """Orbit's gVisor build ``build`` of ``version``, copied once from GHCR into the cache,
    which every node shares: the directory that holds its ``runsc``."""
    cache = Path.cwd().parent
    name = f"gvisor-{version}.{build}"
    found = cache / name
    with (cache / (name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not found.is_dir():
            image = "ghcr.io/agentslastexam/orbit-gvisor:" + version
            subprocess.run([*command, "pull", "-q", image], check=True, stdout=subprocess.DEVNULL)
            published = output(
                *command, "image", "inspect", "-f", '{{index .Labels "orbit.gvisor.build"}}', image
            )
            if published != build:
                raise RuntimeError(
                    f"{image} on GHCR is build {published}, not this engine's {build}"
                )
            source = output(*command, "create", image, "/bin/true")
            with tempfile.TemporaryDirectory(dir=cache) as temporary:
                dest = Path(temporary) / "gvisor"
                subprocess.run([*command, "cp", f"{source}:/gvisor", str(dest)], check=True)
                dest.rename(found)
    return found


def launch(
    index: str, request: dict[str, object], nodes: list[str], *, gpus: bool
) -> subprocess.Popen[bytes]:
    """Run one Sandbox as a step of its own over the first of ``nodes`` it spans, one task
    on each, which Slurm holds to its resources there; one task failing ends them all."""
    count = len(request["nodes"])  # type: ignore[arg-type]
    args = [
        "srun",
        "--exact",
        f"--nodes={count}",
        f"--ntasks={count}",
        "--ntasks-per-node=1",
        "--nodelist=" + ",".join(nodes[:count]),
        "--kill-on-bad-exit=1",
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


def stale_stores(node_local_dir: Path) -> list[Path]:
    """The stores on this node of this account's jobs that have ended: one that Slurm
    killed, at its wall time or by preemption, or whose node went down, cleaned up nothing."""
    stale = []
    for store in node_local_dir.glob(STORE_PREFIX + "*"):
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


def empty_stores(node_local_dir: Path) -> list[Path]:
    """This account's empty store directories some minutes old on this node: one can come
    back after a node's hold step removed its store, naming no job, while a store in use
    names its job as soon as it is made."""
    found = []
    for store in node_local_dir.glob(STORE_PREFIX + "*"):
        with contextlib.suppress(OSError):
            made = store.stat()
            if (
                made.st_uid == os.getuid()
                and time.time() - made.st_mtime > 600
                and not any(store.iterdir())
            ):
                found.append(store)
    return found


def release(command: list[str], directory: str) -> None:
    """Remove a store and every mount Podman made below it."""
    subprocess.run(
        [*command, "unshare", "python3", "-I", __file__, "release", directory], check=True
    )
    # Podman sets up an empty store at its configured path again as it exits.
    subprocess.run(["rm", "-rf", "--one-file-system", "--", directory], check=True)


def terminated(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)


def store(spec: dict[str, object]) -> tuple[str, list[str], Path]:
    """This job's own Podman store on its node, below the directory its configuration names,
    else ``$SLURM_TMPDIR``, else ``/tmp``, and the command that uses it; the stores of this
    account's jobs that ended on the node are removed first."""
    named = os.path.expanduser(os.path.expandvars(str(spec.get("node_local_dir") or "")))
    node_local_dir = Path(named or os.getenv("SLURM_TMPDIR") or "/tmp")
    node_local_dir.mkdir(parents=True, exist_ok=True)
    directory = tempfile.mkdtemp(prefix=STORE_PREFIX, dir=node_local_dir)
    (Path(directory) / OWNER).write_text(os.environ["SLURM_JOB_ID"])
    # Slurm signals the batch step before it kills it, as a job is cancelled or preempted:
    # clean up then too.
    signal.signal(signal.SIGTERM, terminated)
    return directory, podman(directory), node_local_dir


def hold_namespace(command: list[str], node_local_dir: Path) -> None:
    """Keep the private rootless user namespace alive until the batch step finishes cleanup,
    including when Slurm kills every Sandbox's step, and remove the stale stores."""
    subprocess.run([*command, "unshare", "true"], check=True)
    for stale in stale_stores(node_local_dir):
        subprocess.run(
            [*command, "unshare", "rm", "-rf", "--one-file-system", "--", str(stale)],
            check=False,
        )
    for empty in empty_stores(node_local_dir):
        with contextlib.suppress(OSError):
            empty.rmdir()


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
    directory, command, node_local_dir = store(recipe)
    archive = Path.cwd().parent / IMAGES / f"{recipe['key']}.tar.gz"
    part = archive.with_name(f"{archive.name}.{os.environ['SLURM_JOB_ID']}.part")
    step = "pull"
    try:
        hold_namespace(command, node_local_dir)
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
        tag = "localhost/orbit-framework-base:" + base.removeprefix("sha256:")[:12]
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


def set_up(command: list[str], directory: str, spec: dict[str, object], rank: int) -> str:
    """Load node ``rank``'s seed into its store, and record the store and the runtime there
    for the node's steps; the runtime's version."""
    seed = spec["seeds"][rank]  # type: ignore[index]
    load(command, seed["archive"], seed["image"])
    load_layers(command, directory, seed["layers"])
    found = runtime(command, str(spec["version"]), str(spec["build"]))
    runsc = str(found / "runsc")
    version = output(runsc, "--version").splitlines()[0]
    if not version.endswith(f"+orbit.{spec['version']}.{spec['build']}"):
        raise RuntimeError(f"unexpected gVisor build {version} on {this_node()}")
    if spec["gpus"]:
        supported = output(runsc, "nvproxy", "list-supported-drivers").split()
        for driver in {str(device["driver_version"]) for device in devices()}:
            if driver not in supported:
                raise RuntimeError(
                    f"the NVIDIA driver {driver} of {this_node()} is not one gVisor {version} "
                    "supports; its nvproxy lists them with list-supported-drivers"
                )
    STORES.mkdir(exist_ok=True)
    write(
        STORES / f"{this_node()}.json",
        dict(directory=directory, runsc=runsc),
    )
    return version


def supervise() -> None:
    spec = json.loads(Path("allocation.json").read_text())
    nodes = job_nodes()
    directory, command, node_local_dir = store(spec)
    steps: dict[str, subprocess.Popen[bytes]] = {}
    holders: subprocess.Popen[bytes] | None = None
    try:
        hold_namespace(command, node_local_dir)
        if len(nodes) > 1:
            # Every other node's store, held by a step there for as long as this one runs,
            # which loads its seed as this node loads its own.
            with open("hold.log", "wb") as log:
                holders = subprocess.Popen(
                    ["srun", "--overlap", f"--nodes={len(nodes) - 1}",
                     f"--ntasks={len(nodes) - 1}", "--ntasks-per-node=1",
                     "--nodelist=" + ",".join(nodes[1:]), "--kill-on-bad-exit=1",
                     "python3", "-I", __file__, "hold"],
                    stdout=log, stderr=subprocess.STDOUT,
                )  # fmt: skip
        version = set_up(command, directory, spec, 0)
        while not all(appeared(STORES / f"{node}.json") for node in nodes[1:]):
            assert holders is not None
            if holders.poll() is not None or read("error"):
                raise RuntimeError(
                    read("error") or "a node's store failed: " + read("hold.log")[-2000:]
                )
            time.sleep(0.5)
        SANDBOXES.mkdir(exist_ok=True)
        write("ready", dict(runtime=version, node=nodes[0], nodes=nodes))
        while not read("stop"):
            for request in sorted(SANDBOXES.glob("*/request.json")):
                index = request.parent.name
                if index not in steps and not (request.parent / "done").exists():
                    steps[index] = launch(
                        index, json.loads(request.read_text()), nodes, gpus=bool(spec["gpus"])
                    )
            for index, step in list(steps.items()):
                if (code := step.poll()) is not None:
                    write(SANDBOXES / index / "done", code)
                    del steps[index]
            if holders is not None and holders.poll() is not None:
                raise RuntimeError("a node's store ended: " + read("hold.log")[-2000:])
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
        if holders is not None and not read("stop"):
            # Each removes its node's store once the job is stopped or released: a signal
            # would not let it, as srun turns SIGTERM into SIGKILL for its tasks.
            write("release", "1")
        # The batch step outlives each Sandbox's step, including an OOM that kills every
        # process there. Keep the job's counters before the allocation goes away.
        Path("memory").write_text((job_cgroup() / "memory.events").read_text())
        try:
            clean(command, directory)
        finally:
            if holders is not None:
                try:
                    holders.wait(timeout=540)
                except subprocess.TimeoutExpired:
                    holders.kill()
                    holders.wait()


def hold() -> None:
    """Hold a node's store for as long as the allocation runs, other than the first's, which
    the batch step holds."""
    spec = json.loads(Path("allocation.json").read_text())
    directory, command, node_local_dir = store(spec)
    try:
        hold_namespace(command, node_local_dir)
        set_up(command, directory, spec, job_nodes().index(this_node()))
        # Written on other machines: the directory is listed first (see appeared). The job's
        # stop is there from the start, empty until the Host stops it.
        while not (appeared(Path("stop")) and read("stop")) and not appeared(Path("release")):
            time.sleep(1)
    finally:
        clean(command, directory)


def start(index: str) -> None:
    """Run node ``SLURM_PROCID`` of the Sandbox ``index``: its share of the group's network
    and the shared directory where it has several nodes or that directory, its volumes and
    its container; then hold it until it is saved or destroyed, measuring what
    it writes where it is held to ``storage_mb``."""
    box = SANDBOXES / index
    rank = int(os.environ["SLURM_PROCID"])
    own = box / NODES / str(rank)
    deadline = time.monotonic() + GROUP_TIMEOUT_SEC
    while not appeared(box / "request.json"):
        if time.monotonic() > deadline:
            raise RuntimeError(f"{this_node()} sees no request for Sandbox {index}")
        time.sleep(0.5)
    spec = json.loads((box / "request.json").read_text())
    node, count = spec["nodes"][rank], len(spec["nodes"])
    own.mkdir(parents=True, exist_ok=True)
    if job_nodes()[rank] != this_node():
        # Every control step finds a node's part of the Sandbox by the node's place in the job.
        raise RuntimeError(f"node {rank} of Sandbox {index} runs on {this_node()}")
    stored = node_store()
    limit = limit_cgroup()
    if (
        int((limit / "memory.max").read_text()) != spec["memory_mb"] << 20
        or (limit / "memory.swap.max").read_text().strip() != "0"
        or len(os.sched_getaffinity(0)) != spec["cpus"]
    ):
        raise RuntimeError(
            f"Slurm did not enforce the requested CPU, memory and zero-swap allocation on "
            f"{this_node()}"
        )
    command = podman(stored["directory"])
    name = container(index)
    gpus = devices() if spec["gpus"] else []
    if len(gpus) != spec["gpus"]:
        raise RuntimeError(f"{this_node()} gave {len(gpus)} GPUs, requested {spec['gpus']}")
    mounts: list[dict[str, object]] = []
    kept: set[str] = set()
    pods: list[str] = []
    flags = [
        *command,
        "run",
        "-d",
        "--name",
        name,
        "--http-proxy=false",
        "--runtime",
        stored["runsc"],
        "--runtime-flag=ignore-cgroups",
        "--runtime-flag=enclosing-memory-cgroup=" + str(limit),
        "--oom-score-adj=500",
        f"--shm-size={spec['shm_mb']}m",
        *spec["runtime_args"],
    ]
    env = dict(spec["env"])
    if gpus:
        flags += ["--runtime-flag=nvproxy", "-e", "NVIDIA_VISIBLE_DEVICES=void"]
        for gpu in gpus:
            flags.extend(["--device", "nvidia.com/gpu=" + str(gpu["id"])])
    try:
        sealing, hcas = name, []
        if count == 1:
            flags.append("--network=slirp4netns:allow_host_loopback=false")
        else:
            pods = [f"router-{index}", f"group-{index}"]
            pod = group_up(command, box, index, rank, count, spec["shm_mb"])
            sealing = output(*command, "pod", "inspect", "-f", "{{.InfraContainerID}}", pod)
            hcas = infiniband()
            flags += ["--pod", pod, f"--hostname=node{rank}", "--ulimit=memlock=-1:-1"]
            for device in hcas:
                flags.extend(["--device", device])
            if hcas:
                flags.append("--runtime-flag=rdmaproxy")
        for key, value in env.items():
            flags.extend(["--env", f"{key}={value}"])
        shared: list[str] = []
        if spec.get("shared"):
            # Every file there is the Sandbox's account's, whoever made it; on the host, the
            # job's account owns them all, as the shared file system lets it.
            given = dict(spec["shared"])  # type: ignore[call-overload]
            shared = [str(Path.cwd().parent / str(given["path"]))]
            owner = in_namespace(
                command, "account", stored["directory"], node["image"], str(given["account"])
            )
            flags += [
                f"--volume={shared[0]}:{SHARED}" + (":ro" if given["readonly"] else ""),
                f"--runtime-flag=shared-mount={SHARED}={owner}",
            ]
        # What node 0 counts of it, for each node.
        counted = shared if rank == 0 and (spec.get("shared") or {}).get("counted") else []
        # A candidate is a volume only where the image has a directory.
        candidates = spec.get("candidates") or []
        found = (
            json.loads(
                in_namespace(
                    command,
                    "directories",
                    stored["directory"],
                    node["image"],
                    json.dumps(candidates),
                )
            )
            if candidates
            else []
        )
        arguments, mounts = mount_volumes(
            command,
            stored["directory"],
            f"{index}.{rank}",
            [dict(path=path, kind="fresh") for path in found] + node["volumes"],
        )
        subprocess.run([*flags, *arguments, node["image"]], check=True, stdout=subprocess.DEVNULL)
        if output(*command, "inspect", "--format", "{{.Image}}", name) != node["image"]:
            raise RuntimeError("Podman started a different image")
        if spec["sealed"]:
            network(command, sealing, sealed=True)
        runtime_flags = [
            flag
            for flag in (*spec["runtime_args"], *flags)  # type: ignore[misc]
            if str(flag).startswith("--runtime-flag=")
        ]
        conmon = int(output(*command, "inspect", "--format", "{{.State.ConmonPid}}", name))
        upper = output(*command, "inspect", "--format", "{{.GraphDriver.Data.UpperDir}}", name)
        if spec.get("storage") and not os.path.isdir(upper):
            raise RuntimeError(
                f"Podman's store on {this_node()} keeps no layer to measure: {upper}"
            )
        layers = [f"{stored['directory']}/{LINKS}/{m['layer']}" for m in mounts]
        # What it has written: what the snapshot it booted from holds, its writable layer and
        # what its volumes gained, Podman's filled from the image and copies as it started.
        carried_bytes = int(node.get("written") or 0) - measure(command, layers)

        def written() -> int:
            return carried_bytes + measure(command, [upper, *layers])

        schedule = spec.get("storage")
        due = time.monotonic()
        write(
            own / "ready",
            dict(
                container=name,
                network=sealing,
                runtime_flags=runtime_flags,
                image=node["image"],
                devices=gpus,
                step=os.environ["SLURM_STEP_ID"],
                volumes=found,
                node=this_node(),
                hcas=hcas,
            ),
        )
        while True:
            # The Host writes it on another machine: the directory is listed first.
            order = read(box / "stop") if appeared(box / "stop") else ""
            if order == "save":
                kept = save(
                    command,
                    stored["directory"],
                    name,
                    own,
                    mounts,
                    written,
                    max_layers=int(spec["max_layers"]),
                )
                return
            if order:
                return
            if not alive(conmon):
                raise RuntimeError(f"the sandbox image command exited on {this_node()}")
            if schedule and time.monotonic() >= due:
                total = written() + measure(command, counted)
                limit_bytes = int(schedule["storage_mb"]) << 20  # type: ignore[index]
                if total > limit_bytes:
                    exceeded = dict(written_mb=total >> 20, storage_mb=limit_bytes >> 20)
                    write(own / "storage", exceeded)
                    subprocess.run([*command, "kill", name], check=False, capture_output=True)
                    # A step that fails ends on every node.
                    raise SystemExit(f"node {rank} wrote {total >> 20} MiB, past storage_mb")
                due = time.monotonic() + interval(schedule, limit_bytes - total)  # type: ignore[arg-type]
            time.sleep(1)
    finally:
        (own / "memory").write_text((limit / "memory.events").read_text())
        subprocess.run(
            [*command, "rm", "-f", "-v", "-t", "1", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if pods:
            subprocess.run(
                [*command, "pod", "rm", "-f", "-t", "1", *pods],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if mounts:
            # Its views and the layers no snapshot of it keeps go with it.
            in_namespace(
                command,
                "volumes",
                stored["directory"],
                json.dumps(
                    [["unmount", m["layer"]] for m in mounts if m["lowers"]]
                    + [["remove", m["layer"]] for m in mounts if m["layer"] not in kept]
                ),
            )


def save(
    command: list[str],
    directory: str,
    name: str,
    own: Path,
    mounts: list[dict[str, object]],
    written: Callable[[], int],
    *,
    max_layers: int,
) -> set[str]:
    """Stop a node of the Sandbox and commit its disk to the node's store, and record each
    volume's stack, the layer that holds its writes on what it viewed, and what the node
    has written. A commit, or a volume's stack, that a Sandbox booted from it would stack
    past ``max_layers`` with its own writable layer is flattened to one layer. Return the
    layers the snapshot keeps."""
    stop = [*command, "stop", "-t", "30", name]
    # A guest session whose srun on the Host was killed ended with its step, and Podman's
    # monitor of it too, before the monitor wrote the session's exit: Podman stops the
    # container, then fails as it waits for that exit, but records the session ended, so a
    # second stop finishes the first (AGENTS.md).
    if subprocess.run(stop, stdout=subprocess.DEVNULL).returncode:
        subprocess.run(stop, check=True, stdout=subprocess.DEVNULL)
    # By the Sandbox's index alone, which its handle names on every node.
    tag = f"{SNAPSHOT_REPOSITORY}:{own.parent.parent.name}"
    image = output(*command, "commit", "-q", name, tag)
    if layers_of(command, image) + 1 > max_layers:
        image = squash(command, directory, image, tag)
    stacks: dict[str, dict[str, object]] = {}
    merges: list[list[object]] = []
    for m in mounts:
        layers = [str(m["layer"]), *(str(lower) for lower in m["lowers"])]
        root = root_of(command, directory, layers[0])
        if m["lowers"] and len(layers) + 1 > max_layers:
            merged = f"{layers[0]}.f"
            (Path(directory) / LAYERS / merged).mkdir()
            (Path(directory) / LINKS / merged).symlink_to(f"../{LAYERS}/{merged}")
            merges.append(["merge", layers[0], merged])
            layers = [merged]
        stacks[str(m["path"])] = dict(layers=layers, root=root, copied=m["copied"])
    if merges:
        in_namespace(command, "volumes", directory, json.dumps(merges))
    if any(m["lowers"] for m in mounts):
        # Before the Host hears of the snapshot: a view's upper layer is no lower of
        # another view while it is mounted.
        in_namespace(
            command,
            "volumes",
            directory,
            json.dumps([["unmount", m["layer"]] for m in mounts if m["lowers"]]),
        )
    write(
        own / "saved",
        dict(
            image=image.removeprefix("sha256:"),
            layers=layers_of(command, image),
            stacks=stacks,
            written=written(),
        ),
    )
    return {str(stack["layers"][0]) for stack in stacks.values()}


def layers_of(command: list[str], image: str) -> int:
    """How many layers ``image`` stacks."""
    return int(output(*command, "image", "inspect", "--format", "{{len .RootFS.Layers}}", image))


def squash(command: list[str], directory: str, image: str, tag: str) -> str:
    """Rebuild ``image`` as ``tag`` in one layer, with its configuration, and remove it;
    the new image's ID."""
    context = tempfile.mkdtemp(dir=directory)
    try:
        Path(context, "Containerfile").write_text(f"FROM {image}\n")
        squashed = output(
            *command, "build", "--quiet", "--squash-all", "--pull=never", "--tag", tag,
            "--file", f"{context}/Containerfile", context,
        )  # fmt: skip
    finally:
        shutil.rmtree(context, ignore_errors=True)
    subprocess.run([*command, "rmi", image], check=False, stdout=subprocess.DEVNULL)
    return squashed


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
    if mode == "hold":
        try:
            hold()
        except Exception as exc:
            if not Path("ready").exists():
                write("error", f"{this_node()}: {exc}")
            raise
        return
    if mode == "prepare":
        try:
            prepare()
        except Exception as exc:
            write("error", str(exc))
            raise
        return
    if mode in ("group", "wireguard"):
        # Their input holds a private key, which goes by a pipe, never by a command line.
        (group if mode == "group" else wireguard)(json.loads(sys.stdin.read()))
        return
    if mode == "volumes":
        volumes(args[0], json.loads(args[1]))
        return
    if mode == "directories":
        print(json.dumps(directories(args[0], args[1], json.loads(args[2]))))
        return
    if mode == "account":
        print(account(args[0], args[1], args[2]))
        return
    if mode == "measure":
        print(usage(json.loads(args[0])))
        return
    if mode == "copy":
        # A Sandbox's shared directory, below the cache, from a snapshot's.
        copy_tree(str(Path.cwd().parent / args[0]), str(Path.cwd().parent / args[1]))
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
    stored = node_store()
    command = podman(stored["directory"])
    if mode in ("block", "open", "exec"):
        # This node's part of the Sandbox: a Sandbox spans the job's first nodes.
        rank = job_nodes().index(this_node())
        box = json.loads((SANDBOXES / args[0] / NODES / str(rank) / "ready").read_text())
    if mode in ("block", "open"):
        network(command, box["network"], sealed=mode == "block")
    elif mode == "exec":
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
        export(command, stored["directory"], args[0], args[1], args[2:])
    elif mode == "load":
        load(command, args[0], args[1])
    elif mode == "drop":
        in_namespace(
            command, "volumes", stored["directory"], json.dumps([["remove", a] for a in args])
        )
    else:
        raise ValueError(mode)


if __name__ == "__main__":
    main()
