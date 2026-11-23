#!/usr/bin/env bash
# Make the Orbit images this engine uses available locally.
#
# Every Orbit image is published once per version and never overwritten. The builder and
# runner share the engine's version (packages/orbit-run/pyproject.toml); a base has its own
# (BASES in packages/orbit-run/src/orbit/run/__init__.py). A version the registry already has is
# pulled; one not published yet is built here, for testing before CI publishes it. The
# gVisor runtime is installed on a Host rather than used from here; its own build.sh builds
# it.
#
# The Linux VM base is a VM disk, not a Docker image. Orbit downloads a published one when a
# Task first names it, so nothing is pulled ahead; an unpublished one is built into Orbit's
# cache, and a Task names it there by path to test it.
#
#   images/build.sh                      every image
#   images/build.sh orbit-qemu-runner      selected images, by repository name
set -euo pipefail

cd "$(dirname "$0")/.."   # repository root

runtime="${ORBIT_CONTAINER_RUNTIME:-docker}"
command -v "$runtime" >/dev/null 2>&1 || {
    echo "ERROR: $runtime not found — see 'just doctor'" >&2
    exit 1
}

engine=$(sed -n 's/^version = "\(.*\)"$/\1/p' packages/orbit-run/pyproject.toml)
images=("$@")
(( ${#images[@]} )) || images=(container-ubuntu22-base orbit-vm-materializer vm-ubuntu24-base orbit-qemu-runner orbit-firecracker-runner)

for repository in "${images[@]}"; do
    case "$repository" in
        container-ubuntu22-base) dockerfile=images/base/container-ubuntu22/Dockerfile context=images/base/container-ubuntu22 ;;
        vm-ubuntu24-base)        dockerfile="" context="" ;;
        orbit-vm-materializer)     dockerfile=images/builders/vm-materializer/Dockerfile context=images/builders/vm-materializer ;;
        orbit-qemu-runner)         dockerfile=images/runtimes/qemu-runner/Dockerfile context=images/runtimes/qemu-runner ;;
        orbit-firecracker-runner)  dockerfile=images/runtimes/firecracker-runner/Dockerfile context=images/runtimes/firecracker-runner ;;
        *) echo "ERROR: unknown image: $repository" >&2; exit 1 ;;
    esac
    case "$repository" in
        *-base) version=$(sed -n "s/.*\"$repository\": \"\([0-9.]*\)\".*/\1/p" packages/orbit-run/src/orbit/run/__init__.py) ;;
        *) version=$engine ;;
    esac
    name="ghcr.io/agentslastexam/${repository}:${version}"
    published=false
    "$runtime" buildx imagetools inspect "$name" >/dev/null 2>&1 && published=true
    if [[ -z $dockerfile ]]; then
        if $published; then
            echo ">> $name is published; Orbit downloads it when a Task first names it"
            continue
        fi
        disk="$(uv run python -c 'from orbit.run.sources import cache_root; print(cache_root())')/base-disks/$repository-$version.qcow2"
        echo ">> building unpublished $name as $disk"
        images/build.sh orbit-vm-materializer
        rm -f "$disk"
        images/base/vm-ubuntu24/build.sh "$disk"
        echo ">> name $disk as a Task's [environment].image to test it"
    elif $published; then
        echo ">> pulling published $name"
        "$runtime" pull "$name"
    else
        echo ">> building unpublished $name"
        "$runtime" build --file "$dockerfile" --tag "$name" "$context"
    fi
done
