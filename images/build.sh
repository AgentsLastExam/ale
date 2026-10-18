#!/usr/bin/env bash
# Make the ALE images of this engine version available locally.
#
# Every ALE image is published under the engine's version (packages/ale-run/pyproject.toml)
# and never overwritten. A version the registry already has is pulled; one not published
# yet is built here, for testing before CI publishes it.
#
#   images/build.sh                      every image
#   images/build.sh vm-ubuntu24-base     selected images, by repository name
set -euo pipefail

cd "$(dirname "$0")/.."   # repository root

runtime="${ALE_CONTAINER_RUNTIME:-docker}"
command -v "$runtime" >/dev/null 2>&1 || {
    echo "ERROR: $runtime not found — see 'just doctor'" >&2
    exit 1
}

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' packages/ale-run/pyproject.toml)
images=("$@")
(( ${#images[@]} )) || images=(container-ubuntu22-base vm-ubuntu24-base ale-vm-materializer ale-qemu-runner)

for repository in "${images[@]}"; do
    case "$repository" in
        container-ubuntu22-base) dockerfile=images/base/container-ubuntu22/Dockerfile context=images/base/container-ubuntu22 ;;
        vm-ubuntu24-base)        dockerfile=images/base/vm-ubuntu24/Dockerfile context=. ;;
        ale-vm-materializer)     dockerfile=images/builders/vm-materializer/Dockerfile context=images/builders/vm-materializer ;;
        ale-qemu-runner)         dockerfile=images/runtimes/qemu-runner/Dockerfile context=images/runtimes/qemu-runner ;;
        *) echo "ERROR: unknown image: $repository" >&2; exit 1 ;;
    esac
    name="ghcr.io/agentslastexam/${repository}:${version}"
    if "$runtime" buildx imagetools inspect "$name" >/dev/null 2>&1; then
        echo ">> pulling published $name"
        "$runtime" pull "$name"
    else
        echo ">> building unpublished $name"
        "$runtime" build --file "$dockerfile" --tag "$name" "$context"
    fi
done
