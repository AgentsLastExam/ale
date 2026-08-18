#!/usr/bin/env bash
# Build the sandbox base images locally.
#
# CI publishes these to GHCR (see .github/workflows/images.yml); this script exists so
# a developer can iterate without pushing. The content tag mirrors CI's, so a locally
# built image and a published one with the same tag have the same inputs.
set -euo pipefail

cd "$(dirname "$0")/../.."   # repository root

images=("${@:-gui vm-gui}")
runtime="${ALE_CONTAINER_RUNTIME:-docker}"

command -v "$runtime" >/dev/null 2>&1 || {
    echo "ERROR: $runtime not found — see 'just doctor'" >&2
    exit 1
}

for image in ${images[@]}; do
    dockerfile="images/base/${image}/Dockerfile"
    [ -f "$dockerfile" ] || { echo "ERROR: no such image: $image" >&2; exit 1; }

    content=$(find "images/base/${image}" packages/ale-run/src/ale/run/guestd \
              -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)
    name="ghcr.io/agentslastexam/${image}"
    tags=(--tag "${name}:latest" --tag "${name}:${content}")
    context="."
    if [ "$image" = vm-materializer ]; then
        name="ghcr.io/agentslastexam/ale-vm-materializer"
        tags=(--tag "${name}:0.1.0" --tag "${name}:${content}")
        context="images/base/vm-materializer"
    elif [ "$image" = vm-gui ]; then
        name="ghcr.io/agentslastexam/vm-ubuntu24-base"
        tags=(--tag "${name}:0.1.0" --tag "${name}:${content}")
    elif [ "$image" = gui ]; then
        name="ghcr.io/agentslastexam/container-ubuntu22-base"
        tags=(--tag "${name}:latest" --tag "${name}:${content}")
    fi

    echo ">> building ${name}:${content}"
    "$runtime" build \
        --file "$dockerfile" \
        "${tags[@]}" \
        --label "ale.content_hash=${content}" \
        "$context"
done
