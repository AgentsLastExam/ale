#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 INPUT.qcow2 OUTPUT.qcow2" >&2
    exit 2
fi

qemu-img check "$1"
qemu-img convert -p -O qcow2 -o compression_type=zstd -c "$1" "$2"
qemu-img check "$2"
qemu-img info "$2"
