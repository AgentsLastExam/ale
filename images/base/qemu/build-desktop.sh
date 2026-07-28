#!/usr/bin/env bash
# Build the Ubuntu Desktop guest disk, from Canonical's own installer.
#
# The guest is installed the way a person would install it — the official Desktop ISO,
# running its own installer — rather than assembled by adding desktop packages to a server
# cloud image. What that buys is a machine that matches what a task author would have in
# front of them, including the applications a desktop actually ships with.
#
# It is unattended because a build nobody can repeat is not a build: every answer the
# installer would ask for is in autoinstall.yaml, next to this file. Autoinstall is
# supported by the Desktop installer from 23.04 onward, which is why this is 24.04 and not
# 22.04 — on 22.04 the desktop installer is ubiquity, which has no equivalent.
#
# Two phases, and they need different tools:
#   1. install   — boot the ISO under QEMU with a seed, let it install, power off
#   2. customise — offline, add the guest service, the firewall and the image manifest
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"

RELEASE="${ALE_UBUNTU_RELEASE:-24.04.4}"
ISO_URL="${ALE_UBUNTU_ISO:-https://releases.ubuntu.com/24.04/ubuntu-${RELEASE}-desktop-amd64.iso}"
ISO_SHA256="${ALE_UBUNTU_ISO_SHA256:-3a4c9877b483ab46d7c3fbe165a0db275e1ae3cfe56a5657e5a47c2f99a99d1e}"

CACHE="${ALE_IMAGE_CACHE:-$HOME/.cache/ale/images}"
OUTPUT="${ALE_QEMU_IMAGE:-$CACHE/ale-ubuntu-desktop.qcow2}"
DISK_SIZE="${ALE_DISK_SIZE:-40G}"

AGENT_USER="${ALE_AGENT_USER:-user}"
GUEST_PORT="${ALE_GUESTD_PORT:-7411}"

# The address the runner presents itself at. The guest's only permitted destination, and
# what the in-guest firewall below encodes.
HOST_IP="${ALE_QEMU_HOST_IP:-172.30.0.1}"

# An installer needs real memory and time; these are build-time only and have nothing to
# do with what an episode runs with.
BUILD_RAM="${ALE_BUILD_RAM:-4096}"
BUILD_CPUS="${ALE_BUILD_CPUS:-4}"
INSTALL_TIMEOUT="${ALE_INSTALL_TIMEOUT:-5400}"

# The installer must run under the same firmware the runner boots with, which is UEFI.
# Installed under BIOS instead, the installer partitions a GPT with an EFI system
# partition and then never populates it — it puts grub in the MBR, because that is what it
# booted from. The disk then looks perfectly normal offline and drops the runner straight
# into the EFI shell, which is exactly how the first build failed.
OVMF_CODE="${ALE_OVMF_CODE:-/usr/share/OVMF/OVMF_CODE_4M.fd}"
OVMF_VARS="${ALE_OVMF_VARS:-/usr/share/OVMF/OVMF_VARS_4M.fd}"

need() { command -v "$1" >/dev/null || { echo "missing: $1 ($2)" >&2; exit 1; }; }
need qemu-system-x86_64 "apt install qemu-system-x86"
need qemu-img "apt install qemu-utils"
need genisoimage "apt install genisoimage"
need virt-customize "apt install libguestfs-tools"
[ -r "$OVMF_CODE" ] || { echo "missing $OVMF_CODE (apt install ovmf)" >&2; exit 1; }
[ -w /dev/kvm ] || { echo "/dev/kvm is not writable; the install would take hours" >&2; exit 1; }

mkdir -p "$CACHE"
iso="$CACHE/$(basename "$ISO_URL")"

# --- the installer --------------------------------------------------------------------
if [ ! -s "$iso" ]; then
    echo ">> fetching $ISO_URL"
    curl -fL --progress-bar -o "$iso" "$ISO_URL"
fi

# Verified every time, not only on download: a truncated or swapped ISO is a guest whose
# provenance nobody can state, and this is the one place to catch it.
echo ">> verifying the ISO"
echo "${ISO_SHA256}  ${iso}" | sha256sum -c -

# --- the answers ----------------------------------------------------------------------
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

mkdir -p "$staging/seed"
cp "$HERE/autoinstall.yaml" "$staging/seed/user-data"
printf 'instance-id: ale-desktop-build\nlocal-hostname: ale-sandbox\n' > "$staging/seed/meta-data"

# The installer finds this by volume label. `cidata` is what cloud-init looks for, and
# subiquity reads the autoinstall section out of the same user-data.
genisoimage -quiet -output "$staging/seed.iso" -volid cidata -joliet -rock \
    "$staging/seed/user-data" "$staging/seed/meta-data"

# --- phase 1: install -----------------------------------------------------------------
# The ISO's own boot entry is `linux /casper/vmlinuz --- quiet splash`, with no
# `autoinstall` — so booting it as-is lands in the graphical installer and waits for a
# person. The flag cannot be added without either remastering the ISO or booting its
# kernel directly, and the second is far less machinery: the kernel and initrd are copied
# out, and the ISO stays attached so casper still finds the filesystem it unpacks from.
echo ">> extracting the installer kernel"
mountpoint="$staging/iso"
mkdir -p "$mountpoint"
sudo mount -o loop,ro "$iso" "$mountpoint"
cp "$mountpoint/casper/vmlinuz" "$mountpoint/casper/initrd" "$staging/"
sudo umount "$mountpoint"

echo ">> installing (this boots the real installer; expect 20-40 minutes)"
qemu-img create -f qcow2 "$OUTPUT.partial" "$DISK_SIZE" >/dev/null

# `autoinstall` is what tells the installer to take its answers from the seed instead of
# asking. `console=ttyS0` so a build that goes wrong says why on the serial log rather
# than silently sitting on a screen nobody is looking at.
# Writable copy: the firmware records the boot entry the installer creates, and a
# read-only VARS file means that entry is lost the moment the machine powers off.
cp "$OVMF_VARS" "$staging/OVMF_VARS.fd"

timeout "$INSTALL_TIMEOUT" qemu-system-x86_64 \
    -enable-kvm -machine q35,accel=kvm -cpu host \
    -drive "if=pflash,format=raw,unit=0,readonly=on,file=$OVMF_CODE" \
    -drive "if=pflash,format=raw,unit=1,file=$staging/OVMF_VARS.fd" \
    -m "$BUILD_RAM" -smp "$BUILD_CPUS" \
    -drive "file=$OUTPUT.partial,format=qcow2,if=virtio" \
    -drive "file=$iso,media=cdrom,readonly=on" \
    -drive "file=$staging/seed.iso,media=cdrom,readonly=on" \
    -kernel "$staging/vmlinuz" -initrd "$staging/initrd" \
    -append "autoinstall console=ttyS0 --- quiet" \
    -netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
    -display none -serial mon:stdio \
    || { echo "the installer did not finish; the disk is at $OUTPUT.partial" >&2; exit 1; }

mv "$OUTPUT.partial" "$OUTPUT"

# --- phase 2: the sandbox contract ------------------------------------------------------
bash "$HERE/customise.sh" "$OUTPUT" "$AGENT_USER" "$GUEST_PORT" "$HOST_IP" "$REPO_ROOT" true

echo ">> done: $OUTPUT"
qemu-img info "$OUTPUT" | sed 's/^/   /'
