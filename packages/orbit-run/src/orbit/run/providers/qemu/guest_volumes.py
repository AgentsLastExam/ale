"""A Linux VM's volumes, mounted as it boots, before any session starts.

The framework installs this file as ``/opt/orbit/volumes.py`` on the framework's Python, so
like guestd it is standard-library only.

* As the guest boots, ``orbit-volumes.service`` runs it with the Sandbox's volumes, which
  the Provider hands it in QEMU's firmware configuration: each path that is a directory on
  the disk, with no link on its way, becomes an overlay of that directory under a layer of
  its own on the disk, ``/opt/orbit/volumes/<name>``, which a snapshot of the disk keeps and
  a VM booted from it mounts again. What it mounted it writes to
  ``/run/orbit/volumes.json``.
* Once the guest is up, guestd runs it with ``views``, the paths of another Sandbox's
  snapshot to view and the serial of that snapshot's disk, which the Provider has just
  attached read-only: each becomes an overlay of what that disk holds there, its directory
  and its layer, under a layer of this disk's own, and the rest of that disk is unmounted,
  out of sight. It prints the paths it mounted.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path("/sys/firmware/qemu_fw_cfg/by_name/opt/orbit/volumes/raw")
VOLUMES = "/opt/orbit/volumes"
VIEWS = "/opt/orbit/views"
#: Where a viewed snapshot's disk is mounted.
VIEWED = "/opt/orbit/viewed"
MOUNTED = Path("/run/orbit/volumes.json")
#: How long udev may take to name the viewed disk.
DISK_WAIT_SEC = 60


def volume_name(path: str) -> str:
    """The layer of a volume at ``path``, the same on every snapshot of the disk."""
    return hashlib.sha256(path.encode()).hexdigest()[:16]


def _overlay(path: str, lowers: list[str], layer: str) -> None:
    """Mount at ``path`` an overlay of ``lowers``, newest first, under ``layer``, whose root
    takes the owner and mode of the newest, as the volume's root had them."""
    upper, work = f"{layer}/upper", f"{layer}/work"
    if not os.path.isdir(upper):
        os.makedirs(upper)
        os.makedirs(work, exist_ok=True)
        found = os.stat(lowers[0])
        os.chown(upper, found.st_uid, found.st_gid)
        os.chmod(upper, found.st_mode & 0o7777)
    # A view's path may be missing from the verifier's own disk.
    os.makedirs(path, exist_ok=True)
    stack = ":".join(lower.replace(":", "\\:") for lower in lowers)
    options = f"lowerdir={stack},upperdir={upper},workdir={work}"
    subprocess.run(["mount", "-t", "overlay", "overlay", "-o", options, path], check=True)


def _viewed_root(serial: str) -> str:
    """Mount read-only the file system of the disk QEMU attached with ``serial`` that holds
    the snapshot's volumes; return where."""
    deadline = time.monotonic() + DISK_WAIT_SEC
    while True:
        links = sorted(Path("/dev/disk/by-id").glob(f"*{serial}"))
        if links:
            break
        if time.monotonic() > deadline:
            raise RuntimeError(f"no disk with the serial {serial} appeared")
        time.sleep(0.5)
    disk = os.path.realpath(links[0])
    listed = subprocess.run(
        ["lsblk", "-lnpo", "NAME", disk], capture_output=True, text=True, check=True
    ).stdout.split()
    target = f"{VIEWED}/root"
    os.makedirs(target, exist_ok=True)
    # Its partitions, then the disk itself, which may hold a file system of its own.
    for device in [*listed[1:], listed[0]]:
        for options in ("ro,noload", "ro,norecovery", "ro"):
            if subprocess.run(["mount", "-o", options, device, target]).returncode == 0:
                break
        else:
            continue
        if os.path.isdir(target + VOLUMES):
            return target
        subprocess.run(["umount", target], check=True)
    raise RuntimeError(f"the disk {disk} holds no file system with {VOLUMES}")


def boot() -> None:
    subprocess.run(["modprobe", "qemu_fw_cfg"], capture_output=True)
    config = json.loads(CONFIG.read_bytes()) if CONFIG.exists() else {}
    volumes = []
    for path in config.get("volumes", ()):
        if os.path.realpath(path) == path and os.path.isdir(path):
            _overlay(path, [path], f"{VOLUMES}/{volume_name(path)}")
            volumes.append(path)
    MOUNTED.parent.mkdir(parents=True, exist_ok=True)
    MOUNTED.write_text(json.dumps(volumes))


def views(serial: str, paths: list[str]) -> None:
    root = _viewed_root(serial)
    for path in paths:
        name = volume_name(path)
        lowers = [f"{root}{VOLUMES}/{name}/upper"]
        lowers = [lower for lower in lowers if os.path.isdir(lower)] + [f"{root}{path}"]
        _overlay(path, lowers, f"{VIEWS}/{name}")
    # The views hold what they stack; the rest of the snapshot's disk is no one's to see.
    subprocess.run(["umount", "-l", root], check=True)
    print(json.dumps(paths))


if __name__ == "__main__":
    if sys.argv[1:2] == ["views"]:
        views(sys.argv[2], json.loads(sys.argv[3]))
    else:
        boot()
