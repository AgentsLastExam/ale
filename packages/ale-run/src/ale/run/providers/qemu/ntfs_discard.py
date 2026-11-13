"""Discard the clusters of a Windows disk layer that hold only NTFS free space.

Run by the QEMU Provider inside the VM builder once a Windows build VM has shut down, never
on the Host. Standard library only, since it runs on the builder's system Python.

A qcow2 overlay copies a whole cluster up from the layer below on the first write to any
part of it, and keeps it after Windows deletes the file that was there. A cluster that lies
wholly in NTFS free space, by the volume's own allocation bitmap (``$Bitmap``), holds
nothing the guest can read back, so it is discarded: the overlay keeps one metadata entry
for it, and the guest reads zeros there. Only clusters the layer itself holds are touched,
and only inside an NTFS volume. libguestfs reads the bitmap through its appliance VM;
``qemu-io`` discards. Never zero-fill free space instead: every zero written to an overlay
is a cluster more in it.

Usage: ``python3 ntfs_discard.py DISK``. Prints a JSON object with the bytes the layer held
before and after.
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: qemu-io refuses a request of 2 GiB or more, so longer free runs go in pieces.
MAX_RANGE = 1 << 30


@dataclass(frozen=True)
class Volume:
    """An NTFS volume: where it starts on the disk, its length and its cluster bitmap."""

    start: int
    length: int
    cluster: int
    bitmap: bytes

    def free(self, offset: int, length: int) -> bool:
        """Whether every NTFS cluster of the disk range lies in this volume and is free."""
        if offset < self.start or offset + length > self.start + self.length:
            return False
        first = (offset - self.start) // self.cluster
        last = (offset + length - 1 - self.start) // self.cluster
        if last >= len(self.bitmap) * 8:
            return False
        bits = range(first, last + 1)
        return not any(self.bitmap[index >> 3] >> (index & 7) & 1 for index in bits)


def main() -> int:
    disk = sys.argv[1]
    cluster = int(_json("qemu-img", "info", "--output=json", disk)["cluster-size"])
    before = _held(disk)
    volumes = list(_volumes(disk)) if before else []
    ranges = list(discardable(before, volumes, cluster))
    if ranges:
        commands = "".join(f"discard -q {offset} {length}\n" for offset, length in ranges)
        # Without "unmap" qemu ignores discard requests; it exits 1 when any one fails.
        done = subprocess.run(
            ["qemu-io", "-f", "qcow2", "-d", "unmap", disk],
            input=commands,
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            print(f"ALE: qemu-io could not discard: {done.stderr or done.stdout}", file=sys.stderr)
            return 1
        subprocess.run(["qemu-img", "check", "-q", disk], check=True)
    after = _held(disk)
    print(
        json.dumps(
            {
                "held_before": sum(length for _, length in before),
                "held_after": sum(length for _, length in after),
                "ranges": len(ranges),
            }
        )
    )
    return 0


def discardable(
    held: list[tuple[int, int]], volumes: list[Volume], cluster: int
) -> Iterator[tuple[int, int]]:
    """The ranges of whole qcow2 clusters the layer holds that are all NTFS free space,
    contiguous ones merged up to ``MAX_RANGE``."""
    start = end = None
    for offset, length in held:
        first = -(-offset // cluster) * cluster
        for position in range(first, offset + length - cluster + 1, cluster):
            if not any(volume.free(position, cluster) for volume in volumes):
                continue
            if end == position and end - start < MAX_RANGE:
                end += cluster
                continue
            if start is not None and end is not None:
                yield start, end - start
            start, end = position, position + cluster
    if start is not None and end is not None:
        yield start, end - start


def _held(disk: str) -> list[tuple[int, int]]:
    """The guest ranges whose data the layer itself holds, not a layer below it."""
    return [
        (int(entry["start"]), int(entry["length"]))
        for entry in _json("qemu-img", "map", "--output=json", disk)
        if entry["depth"] == 0 and entry["data"]
    ]


def _volumes(disk: str) -> Iterator[Volume]:
    """Every NTFS volume on a partition of the disk, read by one libguestfs appliance."""
    # The server it leaves behind keeps stderr: a pipe would never reach end of file.
    with tempfile.TemporaryFile() as errors:
        started = subprocess.run(
            ["guestfish", "--listen", "--ro", "--format=qcow2", "-a", disk],
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            check=False,
        )
        errors.seek(0)
        found = re.search(r"GUESTFISH_PID=(\d+)", started.stdout)
        if started.returncode != 0 or found is None:
            raise RuntimeError(f"guestfish did not start: {errors.read().decode()}")
    remote = f"--remote={found.group(1)}"

    def fish(*command: str) -> str:
        return subprocess.run(
            ["guestfish", remote, *command], capture_output=True, text=True, check=True
        ).stdout

    try:
        fish("run")
        for line in fish("list-filesystems").splitlines():
            device, _, kind = line.partition(": ")
            if kind.strip() != "ntfs":
                continue
            whole = fish("part-to-dev", device).strip()
            number = int(fish("part-to-partnum", device))
            starts = {
                int(part): int(offset)
                for part, offset in re.findall(
                    r"part_num: (\d+)\s+part_start: (\d+)", fish("part-list", whole)
                )
            }
            with tempfile.TemporaryDirectory(prefix="ale-ntfs-") as temporary:
                boot, bitmap = Path(temporary) / "boot", Path(temporary) / "bitmap"
                fish("download-offset", device, str(boot), "0", "512")
                # Inode 6 is $Bitmap: one bit per cluster, set when it is in use.
                fish("ntfscat-i", device, "6", str(bitmap))
                yield _volume(starts[number], boot.read_bytes(), bitmap.read_bytes())
    finally:
        subprocess.run(["guestfish", remote, "exit"], capture_output=True, check=False)


def _volume(start: int, boot: bytes, bitmap: bytes) -> Volume:
    if boot[3:11] != b"NTFS    ":
        raise RuntimeError("an NTFS volume has no NTFS boot sector")
    sector = struct.unpack_from("<H", boot, 0x0B)[0]
    per_cluster = boot[0x0D]
    per_cluster = 1 << (256 - per_cluster) if per_cluster > 0x80 else per_cluster
    sectors = struct.unpack_from("<Q", boot, 0x28)[0]
    return Volume(start=start, length=sectors * sector, cluster=sector * per_cluster, bitmap=bitmap)


def _json(*argv: str) -> Any:
    return json.loads(subprocess.run(argv, capture_output=True, text=True, check=True).stdout)


if __name__ == "__main__":
    sys.exit(main())
