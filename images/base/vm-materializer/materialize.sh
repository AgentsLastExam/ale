#!/usr/bin/env bash
set -euo pipefail

test -s /input/rootfs.tar
test -s /input/initrd.img
rm -f /output/disk.raw /output/disk.qcow2

mkosi -C /config build
qemu-img convert -f raw -O qcow2 -c /output/disk.raw /output/disk.qcow2
qemu-img check -q /output/disk.qcow2
