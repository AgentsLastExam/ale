#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 WINDOWS_ARM64_ISO VIRTIO_WIN_ISO OUTPUT_QCOW2" >&2
  exit 64
fi

windows_iso="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
virtio_iso="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
output="$(cd "$(dirname "$3")" && pwd)/$(basename "$3")"
source_dir="$(cd "$(dirname "$0")" && pwd)"
repository="$(cd "$source_dir/../../.." && pwd)"
windows_image_name="${ALE_WINDOWS_IMAGE_NAME:-Windows 11 Enterprise Evaluation}"

[[ "$windows_image_name" =~ ^[A-Za-z0-9._\ \(\)-]+$ ]] || {
  echo "ALE_WINDOWS_IMAGE_NAME contains unsupported XML characters" >&2
  exit 65
}

for binary in qemu-system-aarch64 qemu-img xorriso; do
  command -v "$binary" >/dev/null || {
    echo "$binary is required (brew install qemu xorriso)" >&2
    exit 69
  }
done
[[ "$(uname -m)" == "arm64" ]] || {
  echo "this HVF builder requires an Apple Silicon host" >&2
  exit 69
}
[[ -f "$windows_iso" ]] || { echo "missing Windows ISO: $windows_iso" >&2; exit 66; }
[[ -f "$virtio_iso" ]] || { echo "missing VirtIO ISO: $virtio_iso" >&2; exit 66; }
[[ ! -e "$output" ]] || { echo "refusing to overwrite $output" >&2; exit 73; }

temporary="$(mktemp -d "${TMPDIR:-/tmp}/ale-winarm.XXXXXX")"
cleanup() { rm -rf "$temporary"; }
trap cleanup EXIT

config="$temporary/config"
mkdir -p "$config/guestd"
sed "s/@@WINDOWS_IMAGE_NAME@@/$windows_image_name/g" \
  "$source_dir/Autounattend.xml" > "$config/Autounattend.xml"
cp "$source_dir/install.ps1" "$config/install.ps1"
cp "$repository/images/base/vm-windows10/prepare.ps1" "$config/prepare.ps1"
cp "$repository/images/base/vm-windows10/debloat.ps1" "$config/debloat.ps1"
cp "$repository"/packages/ale-run/src/ale/run/guestd/*.py "$config/guestd/"
touch "$config/ALE_CONFIG.TAG"
xorriso -as mkisofs -quiet -iso-level 3 -J -R -V ALE_CONFIG \
  -o "$temporary/ale-config.iso" "$config"

firmware="$(cd "$(dirname "$(command -v qemu-system-aarch64)")/../share/qemu" && pwd)"
code="$firmware/edk2-aarch64-code.fd"
variables="$temporary/efi-vars.fd"
[[ -f "$code" && -f "$firmware/edk2-arm-vars.fd" ]] || {
  echo "QEMU aarch64 UEFI firmware is unavailable under $firmware" >&2
  exit 69
}
cp "$firmware/edk2-arm-vars.fd" "$variables"
qemu-img create -f qcow2 "$output" 80G

qemu-system-aarch64 \
  -name ale-windows11-arm64-builder \
  -machine virt,accel=hvf,highmem=on \
  -cpu host -smp 4 -m 8G \
  -drive "if=pflash,format=raw,readonly=on,file=$code" \
  -drive "if=pflash,format=raw,file=$variables" \
  -device ramfb -device qemu-xhci -device usb-kbd -device usb-tablet \
  -device usb-storage,drive=windows-install \
  -drive "if=none,id=windows-install,format=raw,media=cdrom,readonly=on,file=$windows_iso" \
  -device usb-storage,drive=virtio-drivers \
  -drive "if=none,id=virtio-drivers,format=raw,media=cdrom,readonly=on,file=$virtio_iso" \
  -device usb-storage,drive=ale-config \
  -drive "if=none,id=ale-config,format=raw,media=cdrom,readonly=on,file=$temporary/ale-config.iso" \
  -drive "if=virtio,id=system,format=qcow2,file=$output,discard=unmap" \
  -nic user,model=virtio-net-pci \
  -display cocoa

qemu-img check "$output"
echo "prepared Windows 11 ARM64 disk: $output"
