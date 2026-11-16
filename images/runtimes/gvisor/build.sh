#!/usr/bin/env bash
# Build ALE's gVisor runtime image: runsc, its containerd shim and its gvisor-bin/
# sidecars from an official gVisor release tag with ALE's patches applied in order,
# built by gVisor's own Bazel build the way gVisor builds its release tarball.
#
#   images/runtimes/gvisor/build.sh WORKDIR
#
# WORKDIR holds the gVisor checkout, the Bazel cache (about 10 GB) and the built tarball;
# put it on a fast local disk. The build runs Bazel in a privileged container on the local
# Docker daemon, which must be rootful. The result is tagged
# ghcr.io/agentslastexam/ale-gvisor:$VERSION; .github/workflows/images.yml publishes it.
set -euo pipefail

# This image's own version: raise it to publish a changed build. A published version is
# never rebuilt.
VERSION=0.1.0
GVISOR_TAG=release-20261109.0
GVISOR_COMMIT=a0e6be6fb5e03d8ebea13592f36a097c7e9a41b9

(( $# == 1 )) || { echo "usage: $0 WORKDIR" >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$1"
work=$(cd "$1" && pwd)
src=$work/gvisor
out=$work/out
image=ghcr.io/agentslastexam/ale-gvisor:$VERSION
# runsc reports this as its version: the upstream release, then ALE's build of it.
stamp=$GVISOR_TAG+ale.$VERSION

if docker buildx imagetools inspect "$image" >/dev/null 2>&1; then
    echo "WARNING: $image is already published; this build replaces it in the local" >&2
    echo "         Docker only. If the patches or the release changed, raise VERSION." >&2
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
printf '#!/bin/sh\necho STABLE_VERSION %s\n' "$stamp" >"$src/tools/ale_workspace_status.sh"
chmod +x "$src/tools/ale_workspace_status.sh"

# gVisor's release pipeline builds //debian:gvisor-release-tar-* with these options; its
# Makefile runs Bazel in its builder container.
echo ">> building $stamp"
options="--config=x86_64 --compilation_mode=opt"
options+=" --workspace_status_command=tools/ale_workspace_status.sh"
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
    --tag "$image" "$out"
