#!/usr/bin/env bash
# Build the sandbox base images locally.
#
# CI publishes these to GHCR (see .github/workflows/images.yml); this script exists so
# a developer can iterate without pushing. The content tag mirrors CI's, so a locally
# built image and a published one with the same tag have the same inputs.
set -euo pipefail

cd "$(dirname "$0")/../.."   # repository root

images=("${@:-cli gui}")
runtime="${ALE_CONTAINER_RUNTIME:-docker}"

command -v "$runtime" >/dev/null 2>&1 || {
    echo "ERROR: $runtime not found — see 'just doctor'" >&2
    exit 1
}

for image in ${images[@]}; do
    dockerfile="images/base/${image}/Dockerfile"
    [ -f "$dockerfile" ] || { echo "ERROR: no such image: $image" >&2; exit 1; }

    content=$(git ls-files -s "images/base/${image}" packages/ale-run/src/ale/run/guestd \
              | sha256sum | cut -c1-12)
    name="ghcr.io/agentslastexam/sandbox-base-${image}"

    echo ">> building ${name}:${content}"
    "$runtime" build \
        --file "$dockerfile" \
        --tag "${name}:latest" \
        --tag "${name}:${content}" \
        --label "ale.content_hash=${content}" \
        .
done
