#!/usr/bin/env bash
# Build a *headless* Ubuntu guest disk, from Canonical's cloud image.
#
# The smaller of the two guests: fast to build, ~1GB, and enough for every task that does
# not need a screen. For one that does, see build-desktop.sh, which installs the real
# desktop from the official ISO — Canonical publishes no desktop cloud image, so a
# desktop cannot come from here.
#
# Starts from Canonical's own cloud image rather than anyone's exported disk: it is
# published, versioned, and its contents are accountable to a build nobody here ran. What
# this adds is the sandbox image contract — one interpreter with the guest service's
# dependencies, an unprivileged agent account, the guest service itself, and a firewall
# whose only exception is the gateway.
#
# The guest is booted by the runner container (see images/base/qemu/README.md), so nothing
# here needs qemu on the host — only libguestfs to modify a disk offline.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"

RELEASE="${ALE_UBUNTU_RELEASE:-jammy}"           # 22.04
BASE_URL="${ALE_UBUNTU_URL:-https://cloud-images.ubuntu.com/${RELEASE}/current/${RELEASE}-server-cloudimg-amd64.img}"
CACHE="${ALE_IMAGE_CACHE:-$HOME/.cache/ale/images}"
OUTPUT="${ALE_QEMU_IMAGE:-$CACHE/ale-ubuntu22.qcow2}"

AGENT_USER="${ALE_AGENT_USER:-user}"
GUEST_PORT="${ALE_GUESTD_PORT:-7411}"

# The address the runner presents the host at. The guest reaches the gateway here and
# nowhere else, which is what the firewall below encodes.
HOST_IP="${ALE_QEMU_HOST_IP:-172.30.0.1}"

need() { command -v "$1" >/dev/null || { echo "missing: $1 ($2)" >&2; exit 1; }; }
need qemu-img "apt install qemu-utils"
need virt-customize "apt install libguestfs-tools"

mkdir -p "$CACHE"
base="$CACHE/$(basename "$BASE_URL")"

if [ ! -s "$base" ]; then
    echo ">> fetching $BASE_URL"
    curl -fL --progress-bar -o "$base" "$BASE_URL"
fi

# Room for anything a task installs. The cloud image ships a 2.2GB virtual disk, which
# fills during the first apt run.
echo ">> preparing $OUTPUT"
cp --reflink=auto "$base" "$OUTPUT"
qemu-img resize "$OUTPUT" "${ALE_DISK_SIZE:-20G}"

# --- the sandbox contract -----------------------------------------------------------
# Shared with the desktop build, so the two guests cannot drift in what they promise.
bash "$HERE/customise.sh" "$OUTPUT" "$AGENT_USER" "$GUEST_PORT" "$HOST_IP" "$REPO_ROOT" false

# Handed back if sudo built it, so a run needs no privilege of its own.
[ -O "$OUTPUT" ] || sudo chown "$(id -u):$(id -g)" "$OUTPUT"

echo ">> done: $OUTPUT"
qemu-img info "$OUTPUT" | sed 's/^/   /'
echo
echo "   run with: uv run ale run <task> --provider qemu"
echo "   or point elsewhere with ALE_QEMU_IMAGE"
