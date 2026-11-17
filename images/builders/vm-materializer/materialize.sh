#!/usr/bin/env bash
# Make ALE's Linux VM base disk: /input/rootfs.tar, with /input/initrd.img, becomes a
# bootable disk (mkosi), written uncompressed to /output/disk.qcow2. `ale vm-image push`
# compresses each chunk for the registry, and a Host builds Task layers on the pulled disk
# as it is, where compressed clusters would slow every read.
set -euo pipefail

test -s /input/rootfs.tar
test -s /input/initrd.img
rm -f /output/disk.raw /output/disk.qcow2

mkosi -C /config build
qemu-img convert -f raw -O qcow2 /output/disk.raw /output/disk.qcow2
rm -f /output/disk.raw
qemu-img check -q /output/disk.qcow2
# Owned by whoever owns /output, so the caller keeps it without root.
chown --reference=/output /output/disk.qcow2
