#!/usr/bin/env bash
# Build ALE's Linux VM base disk, once per version: this directory's Dockerfile gives its
# root filesystem and initramfs, which the VM builder's mkosi makes a bootable disk
# (materialize.sh). OUTPUT is written uncompressed, as `ale vm-image push` publishes it:
#
#   images/base/vm-ubuntu24/build.sh OUTPUT.qcow2
#   uv run ale vm-image push OUTPUT.qcow2 ghcr.io/agentslastexam/vm-ubuntu24-base:VERSION
#
# The work files (about three times the disk) go beside OUTPUT; put it on a fast local
# disk. The VM builder is the engine's; while its version is unpublished,
# `images/build.sh ale-vm-materializer` builds it first.
set -euo pipefail

(( $# == 1 )) || { echo "usage: $0 OUTPUT.qcow2" >&2; exit 2; }
root=$(cd "$(dirname "$0")/../../.." && pwd)
output=$(realpath -m "$1")
[[ ! -e $output ]] || { echo "ERROR: $output exists" >&2; exit 1; }
mkdir -p "$(dirname "$output")"
builder=$(cd "$root" && uv run python -c \
    'from ale.run.framework import MATERIALIZER_IMAGE; print(MATERIALIZER_IMAGE)')

work=$(mktemp -d "$(dirname "$output")/.vm-ubuntu24-base.XXXXXX")
image="" container=""
release() {
    [[ -z $container ]] || docker rm --force "$container" >/dev/null
    [[ -z $image ]] || docker image rm "$image" >/dev/null 2>&1 || true
    container="" image=""
}
trap 'release; rm -rf "$work"' EXIT
mkdir "$work/input" "$work/output"

echo ">> building the root filesystem"
docker build --file "$root/images/base/vm-ubuntu24/Dockerfile" --iidfile "$work/image" "$root"
image=$(<"$work/image")
container=$(docker create "$image")
docker export --output "$work/input/rootfs.tar" "$container"
docker cp --follow-link "$container:/boot/initrd.img" "$work/input/initrd.img"
release

echo ">> making it a bootable disk with $builder"
docker run --rm --runtime runc --privileged \
    --mount "type=bind,src=$work/input,dst=/input,readonly" \
    --mount "type=bind,src=$work/output,dst=/output" \
    "$builder"
mv "$work/output/disk.qcow2" "$output"
qemu-img info "$output"
