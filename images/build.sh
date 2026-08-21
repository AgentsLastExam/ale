#!/usr/bin/env bash
# Build the publishable Sandbox images locally.
#
# CI publishes these to GHCR (see .github/workflows/images.yml); this script exists so
# a developer can iterate without pushing. The content tag mirrors CI's, so a locally
# built image and a published one with the same tag have the same inputs.
set -euo pipefail

cd "$(dirname "$0")/.."   # repository root

if (( $# )); then
    images=("$@")
else
    images=(container-ubuntu22 vm-ubuntu24 vm-materializer qemu-runner)
fi
runtime="${ALE_CONTAINER_RUNTIME:-docker}"

command -v "$runtime" >/dev/null 2>&1 || {
    echo "ERROR: $runtime not found — see 'just doctor'" >&2
    exit 1
}

for image in "${images[@]}"; do
    case "$image" in
        container-ubuntu22)
            directory="images/base/container-ubuntu22"
            repository="container-ubuntu22-base"
            version="latest"
            context="."
            inputs=("$directory" packages/ale-run/src/ale/run/guestd)
            ;;
        vm-ubuntu24)
            directory="images/base/vm-ubuntu24"
            repository="vm-ubuntu24-base"
            version="0.1.0"
            context="."
            inputs=(
                "$directory"
                images/builders/vm-materializer/10-root.conf
                packages/ale-run/src/ale/run/guestd
            )
            ;;
        vm-materializer)
            directory="images/builders/vm-materializer"
            repository="ale-vm-materializer"
            version="0.1.0"
            context="$directory"
            inputs=("$directory")
            ;;
        qemu-runner)
            directory="images/runtimes/qemu-runner"
            repository="ale-qemu-runner"
            version="0.1.0"
            context="$directory"
            inputs=("$directory")
            ;;
        *)
            echo "ERROR: unknown image: $image" >&2
            exit 1
            ;;
    esac

    content=$(find "${inputs[@]}" -type f -print0 \
        | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)
    name="ghcr.io/agentslastexam/${repository}"
    tags=(--tag "${name}:${version}" --tag "${name}:${content}")

    echo ">> building ${name}:${content}"
    "$runtime" build \
        --file "$directory/Dockerfile" \
        "${tags[@]}" \
        --label "ale.content_hash=${content}" \
        "$context"
done
