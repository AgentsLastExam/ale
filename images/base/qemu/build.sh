#!/usr/bin/env bash
# Build the golden guest image for the QEMU backend.
#
# Starts from the published disk the previous framework's tasks were authored against,
# rather than from a stock cloud image. That disk already carries the desktop, the
# browser and the fonts those tasks expect; rebuilding an equivalent would mean
# rediscovering years of small decisions, and any difference would surface as a task
# that passes on one backend and fails on the other.
#
# What this adds is ours: the guest service, a unit that starts it on boot, and a
# default-deny firewall whose only exception is the gateway.
#
# Offline by construction — the guest never reaches the network during the build, so a
# transient registry outage cannot change what the image contains.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"

BASE_REPO="${ALE_QEMU_BASE_REPO:-agents-last-exam/ale-images-qcow2}"
BASE_FILE="${ALE_QEMU_BASE_FILE:-ale-ubuntu22.qcow2}"
BASE_REVISION="${ALE_QEMU_BASE_REVISION:-main}"
OUTPUT="${ALE_QEMU_IMAGE:-$HOME/.cache/ale/images/ale-ubuntu22.qcow2}"
GUEST_PORT="${ALE_GUESTD_PORT:-7411}"
SLIRP_HOST="10.0.2.2"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)" >&2; exit 1; }; }
need qemu-img "apt install qemu-utils"
need virt-customize "apt install libguestfs-tools"

mkdir -p "$(dirname "$OUTPUT")"

# --- 1. the base disk -------------------------------------------------------------
# Pinned by revision like every other asset: an image that moved underneath a recorded
# result would make two runs incomparable in a way nothing else would reveal.
echo ">> fetching $BASE_REPO:$BASE_FILE @ $BASE_REVISION"
BASE_PATH="$(python3 - "$BASE_REPO" "$BASE_FILE" "$BASE_REVISION" <<'PY'
import sys
from huggingface_hub import hf_hub_download

repo, filename, revision = sys.argv[1:4]
print(hf_hub_download(repo_id=repo, filename=filename, revision=revision, repo_type="dataset"))
PY
)"

echo ">> copying to $OUTPUT"
cp --reflink=auto "$BASE_PATH" "$OUTPUT"
chmod u+w "$OUTPUT"

# --- 2. the guest service ---------------------------------------------------------
# Stdlib only, so the guest's own Python is enough — which is the point: the guest
# interpreter belongs to the image, not to us.
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
mkdir -p "$STAGING/guestd"
cp "$REPO_ROOT"/packages/ale-run/src/ale/run/guestd/*.py "$STAGING/guestd/"

cat > "$STAGING/ale-guestd.service" <<UNIT
[Unit]
Description=ALE guest service
After=network.target

[Service]
# TCP rather than stdio: there is no exec channel into a VM, so the host reaches this
# through a forwarded port. One codebase, two transports.
Environment=DISPLAY=:0
ExecStart=/usr/bin/python3 /opt/ale/guestd/main.py --tcp 0.0.0.0:${GUEST_PORT}
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
UNIT

# --- 3. default-deny egress -------------------------------------------------------
# Slirp already leaves the guest with no route anywhere except what is forwarded, so
# this is defence in depth rather than the primary control. It matters for `open` mode,
# where the guest does have a route and the rules are what keep block/allowlist honest
# if the topology ever changes.
cat > "$STAGING/nftables.conf" <<NFT
#!/usr/sbin/nft -f
flush ruleset

table inet ale {
    chain output {
        type filter hook output priority 0; policy drop;

        ct state established,related accept
        oifname "lo" accept

        # The gateway is the one permitted egress. Under slirp the host is always here.
        ip daddr ${SLIRP_HOST} accept
    }
}
NFT

# --- 4. bake ----------------------------------------------------------------------
echo ">> customising the image"
virt-customize -a "$OUTPUT" \
    --mkdir /opt/ale \
    --copy-in "$STAGING/guestd:/opt/ale" \
    --copy-in "$STAGING/nftables.conf:/etc" \
    --copy-in "$STAGING/ale-guestd.service:/etc/systemd/system" \
    --run-command "systemctl enable ale-guestd.service" \
    --run-command "systemctl enable nftables.service || true" \
    --run-command "mkdir -p /ale/kits /ale/store && chmod -R 0777 /ale" \
    --run-command "python3 -c 'import sys; assert sys.version_info >= (3, 8)'" \
    --no-network

echo ">> done: $OUTPUT"
echo "   run with: uv run ale run <task> --provider qemu"
echo "   or point elsewhere with ALE_QEMU_IMAGE"
