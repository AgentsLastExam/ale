#!/usr/bin/env bash
# Build the Ubuntu guest disk for the VM backend.
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

# Room for the desktop packages and anything a task installs. The cloud image ships a
# 2.2GB virtual disk, which fills during the first apt run.
echo ">> preparing $OUTPUT"
cp --reflink=auto "$base" "$OUTPUT"
qemu-img resize "$OUTPUT" "${ALE_DISK_SIZE:-20G}"

# --- the guest service ------------------------------------------------------------
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
mkdir -p "$staging/guestd"
cp "$REPO_ROOT"/packages/ale-run/src/ale/run/guestd/*.py "$staging/guestd/"

cat > "$staging/ale-guestd.service" <<UNIT
[Unit]
Description=ALE guest service
After=network-online.target
Wants=network-online.target

[Service]
# TCP rather than stdio: there is no exec channel into a virtual machine, so the host
# reaches this through a forwarded port. One codebase, two transports.
ExecStart=/usr/bin/python3 /opt/ale/guestd/main.py --tcp 0.0.0.0:${GUEST_PORT}
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
UNIT

# --- a datasource that is already here --------------------------------------------
# Cloud images do not finish booting until cloud-init finds a datasource, and by default
# it goes looking for a metadata server on the network. In a sandbox there is no such
# server and never will be, so the first boot hung at "waiting for cloud-init to be
# configured" until the timeouts expired. A seed baked into the image is found instantly
# and offline, which keeps what cloud-init is genuinely useful for here — growing the root
# filesystem to the disk it was given, and writing the network configuration — while
# removing the wait. Restricting the datasource list is what stops it searching at all.
mkdir -p "$staging/seed"
cat > "$staging/seed/meta-data" <<META
instance-id: ale-sandbox
local-hostname: ale-sandbox
META
cat > "$staging/seed/user-data" <<'USERDATA'
#cloud-config
# Deliberately empty: everything this guest needs is baked at build time, where it is
# reviewable, rather than applied on each boot where it is not.
USERDATA
cat > "$staging/99-ale-datasource.cfg" <<'DSCFG'
datasource_list: [ NoCloud, None ]
DSCFG

# --- default-deny egress ----------------------------------------------------------
# The rule that actually confines a guest lives in the runner, in a network namespace the
# agent cannot reach — a task may declare `sudo`, and an agent with root can flush the
# tables below. This is the guest's own half: defence in depth, and the posture an image
# keeps if it is ever booted somewhere else. The one permitted address is the runner
# itself, which forwards a single port onward to the gateway.
cat > "$staging/nftables.conf" <<NFT
#!/usr/sbin/nft -f
flush ruleset

table inet ale {
    chain output {
        type filter hook output priority 0; policy drop;

        ct state established,related accept
        oifname "lo" accept

        # The gateway is the one permitted egress.
        ip daddr ${HOST_IP} accept

        # DHCP, or the guest never gets an address to be restricted on.
        udp dport 67 accept
    }
}
NFT

# --- the image contract, in a file -------------------------------------------------
# A container image answers "which account is the agent?" with a label. A disk image has
# nowhere to put one, so the same facts are written where the provider can read them
# through the guest service. Same contract (docs/specs/sandbox-image.md), same fields.
mkdir -p "$staging"
cat > "$staging/image.json" <<MANIFEST
{"user": "${AGENT_USER}", "gui": false, "port": ${GUEST_PORT}}
MANIFEST

# libguestfs builds a small appliance from the host's own kernel, and Debian and Ubuntu
# ship /boot/vmlinuz-* readable only by root. Rather than loosening a system file's
# permissions on someone's machine, the customise step runs elevated and the result is
# handed back — the alternative advice found everywhere is `chmod 0644 /boot/vmlinuz-*`,
# which is a lasting change to the host in exchange for one build.
customize=(virt-customize)
if [ ! -r "/boot/vmlinuz-$(uname -r)" ]; then
    echo ">> /boot/vmlinuz is root-only; running the customise step with sudo"
    customize=(sudo -E virt-customize)
fi

# The build installs packages, so it needs the archive. What must be offline is the
# *sandbox*, not the build of its disk — a guest that had to reach the network to become
# usable would be a guest that cannot run under a deny-all policy.
echo ">> baking the sandbox image contract"
"${customize[@]}" -a "$OUTPUT" \
    --update \
    --install python3,python3-pil,python3-xlib,sudo,nftables,ca-certificates \
    --run-command "id -u ${AGENT_USER} >/dev/null 2>&1 || useradd --create-home --shell /bin/bash ${AGENT_USER}" \
    --mkdir /opt/ale \
    --mkdir /etc/ale \
    --copy-in "$staging/guestd:/opt/ale" \
    --copy-in "$staging/nftables.conf:/etc" \
    --copy-in "$staging/ale-guestd.service:/etc/systemd/system" \
    --mkdir /var/lib/cloud/seed/nocloud \
    --copy-in "$staging/seed/meta-data:/var/lib/cloud/seed/nocloud" \
    --copy-in "$staging/seed/user-data:/var/lib/cloud/seed/nocloud" \
    --copy-in "$staging/99-ale-datasource.cfg:/etc/cloud/cloud.cfg.d" \
    --copy-in "$staging/image.json:/etc/ale" \
    --run-command "chown -R root:root /opt/ale && chmod -R go-rwx /opt/ale" \
    --run-command "mkdir -p /ale/kits /ale/store && chown -R ${AGENT_USER} /ale" \
    --run-command "systemctl enable ale-guestd.service" \
    --run-command "systemctl enable nftables.service" \
    --run-command "python3 -c 'import PIL, Xlib'" \
    --run-command "systemctl disable snapd.seeded.service snapd.service snapd.socket || true" \
    --run-command "cloud-init clean --logs || true"

# Handed back if sudo built it, so a run needs no privilege of its own.
[ -O "$OUTPUT" ] || sudo chown "$(id -u):$(id -g)" "$OUTPUT"

echo ">> done: $OUTPUT"
qemu-img info "$OUTPUT" | sed 's/^/   /'
echo
echo "   run with: uv run ale run <task> --provider qemu"
echo "   or point elsewhere with ALE_QEMU_IMAGE"
