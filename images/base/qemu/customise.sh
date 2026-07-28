#!/usr/bin/env bash
# Add the sandbox contract to a finished Ubuntu disk.
#
# Shared by both guest builds — the server one assembled from a cloud image and the
# desktop one installed from the ISO — so the two cannot drift in what they promise. What
# it adds is exactly what docs/specs/sandbox-image.md requires: one interpreter with the
# guest service's dependencies, the guest service itself, a default-deny firewall, and the
# manifest that states which account the agent runs as.
#
#   customise.sh <image> <agent_user> <guest_port> <host_ip> <repo_root> <gui>
set -euo pipefail

IMAGE="$1"; AGENT_USER="$2"; GUEST_PORT="$3"; HOST_IP="$4"; REPO_ROOT="$5"; GUI="${6:-false}"

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

# --- default-deny egress ----------------------------------------------------------
# The rule that actually confines a guest lives in the runner, in a network namespace the
# agent cannot reach — a task may declare `sudo`, and an agent with root can flush the
# tables below. This is the guest's own half: defence in depth, and the posture an image
# keeps if it is ever booted somewhere else. The one permitted address is the runner,
# which forwards a single port onward to the gateway.
cat > "$staging/nftables.conf" <<NFT
#!/usr/sbin/nft -f
flush ruleset

table inet ale {
    chain output {
        type filter hook output priority 0; policy drop;

        ct state established,related accept
        oifname "lo" accept

        # The runner is the one permitted destination.
        ip daddr ${HOST_IP} accept

        # DHCP, or the guest never gets an address to be restricted on.
        udp dport 67 accept
    }
}
NFT

# --- the image contract, in a file -------------------------------------------------
# A container image answers "which account is the agent?" with a label. A disk image has
# nowhere to put one, so the same facts are written where the provider can read them
# through the guest service. Same fields, same meanings.
cat > "$staging/image.json" <<MANIFEST
{"user": "${AGENT_USER}", "gui": ${GUI}, "port": ${GUEST_PORT}}
MANIFEST

# libguestfs builds a small appliance from the host's own kernel, and Debian and Ubuntu
# ship /boot/vmlinuz-* readable only by root. Rather than loosening a system file's
# permissions on someone's machine, the customise step runs elevated and the result is
# handed back.
customize=(virt-customize)
if [ ! -r "/boot/vmlinuz-$(uname -r)" ]; then
    echo ">> /boot/vmlinuz is root-only; running the customise step with sudo"
    customize=(sudo -E virt-customize)
fi

echo ">> baking the sandbox image contract"
"${customize[@]}" -a "$IMAGE" \
    --install python3,python3-pil,python3-xlib,sudo,nftables,ca-certificates,xdotool \
    --run-command "id -u ${AGENT_USER} >/dev/null 2>&1 || useradd --create-home --shell /bin/bash ${AGENT_USER}" \
    --mkdir /opt/ale \
    --mkdir /etc/ale \
    --copy-in "$staging/guestd:/opt/ale" \
    --copy-in "$staging/nftables.conf:/etc" \
    --copy-in "$staging/ale-guestd.service:/etc/systemd/system" \
    --copy-in "$staging/image.json:/etc/ale" \
    --run-command "chown -R root:root /opt/ale && chmod -R go-rwx /opt/ale" \
    --run-command "systemctl enable ale-guestd.service" \
    --run-command "systemctl enable nftables.service" \
    --run-command "python3 -c 'import PIL, Xlib'" \
    --run-command "[ -f /etc/gdm3/custom.conf ] && grep -q WaylandEnable /etc/gdm3/custom.conf || true" \
    --run-command "cloud-init clean --logs || true"
