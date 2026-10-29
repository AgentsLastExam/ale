#!/usr/bin/env bash
# Finish a prepared Windows base offline (finalize.py, in ALE's VM builder), then compress
# it. INPUT is finished in place.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 INPUT.qcow2 OUTPUT.qcow2" >&2
    exit 2
fi

here="$(cd "$(dirname "$0")" && pwd)"
tools="$here/../../../packages/ale-run/src/ale/run"
input="$(realpath "$1")"
builder="$(uv run python -c 'from ale.run.framework import MATERIALIZER_IMAGE; print(MATERIALIZER_IMAGE)')"

qemu-img check "$input"
docker run --rm --device=/dev/kvm --network=none \
    --mount "type=bind,src=$(dirname "$input"),dst=/disk" \
    --mount "type=bind,src=$here/finalize.py,dst=/tools/finalize.py,readonly" \
    --mount "type=bind,src=$tools/framework_windows.py,dst=/tools/framework_windows.py,readonly" \
    --entrypoint python3 "$builder" /tools/finalize.py "/disk/$(basename "$input")"
qemu-img convert -p -O qcow2 -o compression_type=zstd -c "$input" "$2"
qemu-img check "$2"
qemu-img info "$2"
