#!/usr/bin/env bash
# Build Orbit's gVisor runtime image: runsc, its containerd shim and its gvisor-bin/
# sidecars from an official gVisor release tag with Orbit's patches applied in order,
# built by gVisor's own Bazel build the way gVisor builds its release tarball.
#
#   images/runtimes/gvisor/build.sh WORKDIR
#   images/runtimes/gvisor/build.sh --id      prints the build's id
#
# WORKDIR holds the gVisor checkout, the Bazel cache (about 10 GB) and the built tarball;
# put it on a fast local disk. The build runs Bazel in a privileged container on the local
# Docker daemon, which must be rootful. The result is tagged
# ghcr.io/agentslastexam/orbit-gvisor:$VERSION; .github/workflows/images.yml publishes it.
set -euo pipefail

# This image's own version, which stays 0.1.0 while Orbit is in development: a changed
# build is published under it again, and the build's id tells the builds apart.
VERSION=0.1.0
GVISOR_TAG=release-20261109.0
GVISOR_COMMIT=a0e6be6fb5e03d8ebea13592f36a097c7e9a41b9

here=$(cd "$(dirname "$0")" && pwd)
# What goes into the build: the release, each patch's change in name order (not its header)
# and this Dockerfile's instructions (not its comments). BUILD_ID in packages/orbit-run/src/orbit/run/providers/docker/gvisor.py
# names it too, which its unit test checks.
build_id() {
    {
        echo "$GVISOR_COMMIT"
        for patch in "$here"/patches/*.patch; do sed -n '/^diff --git /,$p' "$patch"; done
        grep -v '^[[:space:]]*#' "$here/Dockerfile"
    } | sha256sum | cut -c1-12
}
if [[ ${1-} == --id ]]; then build_id; exit 0; fi
(( $# == 1 )) || { echo "usage: $0 WORKDIR" >&2; exit 2; }
mkdir -p "$1"
work=$(cd "$1" && pwd)
src=$work/gvisor
out=$work/out
image=ghcr.io/agentslastexam/orbit-gvisor:$VERSION
id=$(build_id)
# runsc reports this as its version: the upstream release, then Orbit's build of it.
stamp=$GVISOR_TAG+orbit.$VERSION.$id

if docker buildx imagetools inspect "$image" >/dev/null 2>&1; then
    echo "WARNING: $image is published; this build replaces it in the local Docker only." >&2
fi

echo ">> checking out gVisor $GVISOR_TAG"
rm -rf "$src" "$out"
git clone --quiet --depth 1 --branch "$GVISOR_TAG" \
    https://github.com/google/gvisor.git "$src"
[[ $(git -C "$src" rev-parse HEAD) == "$GVISOR_COMMIT" ]] || {
    echo "ERROR: $GVISOR_TAG no longer names $GVISOR_COMMIT" >&2
    exit 1
}

for patch in "$here"/patches/*.patch; do
    echo ">> applying ${patch##*/}"
    git -C "$src" apply "$patch"
done

# gVisor stamps the output of tools/workspace_status.sh into every binary; a patched tree
# would read as "$GVISOR_TAG-dirty".
printf '#!/bin/sh\necho STABLE_VERSION %s\n' "$stamp" >"$src/tools/orbit_workspace_status.sh"
chmod +x "$src/tools/orbit_workspace_status.sh"

# gVisor's release pipeline builds //debian:gvisor-release-tar-* with these options; its
# Makefile runs Bazel in its builder container.
echo ">> building $stamp"
options="--config=x86_64 --compilation_mode=opt"
options+=" --workspace_status_command=tools/orbit_workspace_status.sh"
mkdir -p "$out"
status=0
make -C "$src" BAZEL_CACHE="$work/bazel-cache" BAZEL_OPTIONS="$options" \
    copy TARGETS=//debian:gvisor-release-tar-bz2 DESTINATION="$out/" || status=$?
make -C "$src" bazel-shutdown >/dev/null 2>&1 || true
# gVisor's Makefile leaves the container it commits its builder image from.
docker rm -f "gvisor-builder-$("$src/tools/compat/realpath.py" "$src" | md5sum | cut -c1-8)-x86_64" \
    >/dev/null 2>&1 || true
(( status == 0 )) || exit "$status"

# The tarball must hold exactly the release layout, and runsc must report this build.
expected="containerd-shim-runsc-v1
gvisor-bin/checkpointgofer
gvisor-bin/gvisor-proc-umounter
gvisor-bin/gvisor-sentry-prewarmer
gvisor-bin/gvisor_sentry
gvisor-bin/runsc-fd-parking
gvisor-bin/runsc-metric-server
gvisor-bin/runsc-netgofer
runsc"
actual=$(tar -tjf "$out/gvisor.tar.bz2" | grep -v '/$' | LC_ALL=C sort)
[[ $actual == "$expected" ]] || {
    echo "ERROR: the tarball does not have the release layout:" >&2
    diff <(echo "$expected") <(echo "$actual") >&2 || true
    exit 1
}
mkdir -p "$out/check"
tar -xjf "$out/gvisor.tar.bz2" -C "$out/check" runsc
reported=$("$out/check/runsc" --version | head -1)
[[ $reported == "runsc version $stamp" ]] || {
    echo "ERROR: runsc reports '$reported', not $stamp" >&2
    exit 1
}
rm -rf "$out/check"

echo ">> building $image"
docker build --file "$here/Dockerfile" --build-arg "GVISOR_TAG=$GVISOR_TAG" \
    --build-arg "BUILD_ID=$id" --tag "$image" "$out"
