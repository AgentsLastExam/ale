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
windows_image_name="${ALE_WINDOWS_IMAGE_NAME:-Windows 11 Pro}"
windows_product_key="${ALE_WINDOWS_PRODUCT_KEY:-W269N-WFGWX-YVC9B-4J6C9-T83GX}"
qemu_root="${ALE_DARWIN_QEMU_ROOT:-$HOME/.cache/ale/darwin-qemu/v10.0.2-utm}"
qemu="$qemu_root/bin/qemu-system-aarch64-utm"
firmware="$qemu_root/share/qemu"

[[ "$windows_image_name" =~ ^[A-Za-z0-9._\ \(\)-]+$ ]] || {
  echo "ALE_WINDOWS_IMAGE_NAME contains unsupported XML characters" >&2
  exit 65
}
[[ "$windows_product_key" =~ ^[A-Z0-9]{5}(-[A-Z0-9]{5}){4}$ ]] || {
  echo "ALE_WINDOWS_PRODUCT_KEY must contain five groups of five characters" >&2
  exit 65
}

for binary in qemu-img hdiutil python3; do
  command -v "$binary" >/dev/null || {
    echo "$binary is required (brew install qemu)" >&2
    exit 69
  }
done
[[ "$(uname -m)" == "arm64" ]] || {
  echo "this HVF builder requires an Apple Silicon host" >&2
  exit 69
}
[[ -x "$qemu" ]] || {
  echo "missing pinned Darwin QEMU: run scripts/build-darwin-qemu.sh" >&2
  exit 69
}
for file in "$firmware/edk2-aarch64-code.fd" "$firmware/edk2-arm-vars.fd"; do
  [[ -f "$file" ]] || { echo "missing pinned QEMU firmware: $file" >&2; exit 69; }
done
[[ -f "$windows_iso" ]] || { echo "missing Windows ISO: $windows_iso" >&2; exit 66; }
[[ -f "$virtio_iso" ]] || { echo "missing VirtIO ISO: $virtio_iso" >&2; exit 66; }
[[ ! -e "$output" ]] || { echo "refusing to overwrite $output" >&2; exit 73; }

temporary="$(mktemp -d "${TMPDIR:-/tmp}/ale-winarm.XXXXXX")"
config_mount="$temporary/config-mount"
config_mounted=false
qemu_pid=""
cleanup() {
  if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
    kill "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
  fi
  if [[ "$config_mounted" == true ]]; then
    hdiutil detach "$config_mount" >/dev/null 2>&1 || true
  fi
  chmod -R u+w "$temporary" 2>/dev/null || true
  rm -rf "$temporary"
}
trap cleanup EXIT

config="$temporary/config"
boot="$temporary/boot"
mkdir -p "$config/guestd" "$boot" "$config_mount"
sed \
  -e "s/@@WINDOWS_IMAGE_NAME@@/$windows_image_name/g" \
  -e "s/@@WINDOWS_PRODUCT_KEY@@/$windows_product_key/g" \
  "$source_dir/Autounattend.xml" > "$config/Autounattend.xml"
cp "$source_dir/install.ps1" "$config/"
cp "$source_dir/bootstrap-system.ps1" "$config/"
cp "$source_dir/bootstrap-user.ps1" "$config/"
cp "$source_dir/startup.nsh" "$boot/"
cp "$repository/images/base/vm-windows10/prepare.ps1" "$config/prepare.ps1"
cp "$repository/images/base/vm-windows10/debloat.ps1" "$config/debloat.ps1"
cp "$repository"/packages/ale-run/src/ale/run/guestd/*.py "$config/guestd/"
touch "$config/ALE_CONFIG.TAG"

# Windows Setup discovers Autounattend.xml on this removable FAT32 device.
hdiutil create -size 64m -fs 'MS-DOS FAT32' -volname ALE_CONFIG \
  -layout NONE -type UDIF "$temporary/ale-config" >/dev/null
hdiutil attach -nobrowse -mountpoint "$config_mount" "$temporary/ale-config.dmg" >/dev/null
config_mounted=true
cp -R "$config/." "$config_mount/"
sync
hdiutil detach "$config_mount" >/dev/null
config_mounted=false

# UEFI executes startup.nsh from a separate one-shot disk. QMP removes this device after
# Windows PE starts, so installation reboots select the populated NVMe disk instead of
# entering the optical installer again. The official Microsoft ISO stays unmodified.
hdiutil create -size 64m -fs 'MS-DOS FAT32' -volname ALE_BOOT \
  -layout NONE -type UDIF "$temporary/ale-boot" >/dev/null
hdiutil attach -nobrowse -mountpoint "$config_mount" "$temporary/ale-boot.dmg" >/dev/null
config_mounted=true
cp -R "$boot/." "$config_mount/"
sync
hdiutil detach "$config_mount" >/dev/null
config_mounted=false

variables="$temporary/efi-vars.fd"
cp "$firmware/edk2-arm-vars.fd" "$variables"
qemu-img create -f qcow2 "$output" 80G

"$qemu" \
  -L "$firmware" \
  -name ale-windows11-arm64-builder \
  -machine virt,accel=hvf,highmem=off \
  -cpu host -smp "${ALE_WINDOWS_BUILD_CPUS:-4}" -m "${ALE_WINDOWS_BUILD_MEMORY_MB:-4096}" \
  -nodefaults \
  -drive "if=pflash,format=raw,readonly=on,file=$firmware/edk2-aarch64-code.fd" \
  -drive "if=pflash,format=qcow2,file=$variables" \
  -device virtio-ramfb \
  -device qemu-xhci,id=usb-bus \
  -device usb-kbd,bus=usb-bus.0 \
  -device usb-tablet,bus=usb-bus.0 \
  -drive "if=none,id=windows-install,format=raw,media=cdrom,readonly=on,file=$windows_iso" \
  -device usb-storage,drive=windows-install,bootindex=0,bus=usb-bus.0 \
  -drive "if=none,id=virtio-drivers,format=raw,media=cdrom,readonly=on,file=$virtio_iso" \
  -device usb-storage,drive=virtio-drivers,bus=usb-bus.0 \
  -drive "if=none,id=ale-config,format=raw,readonly=on,file=$temporary/ale-config.dmg" \
  -device usb-storage,drive=ale-config,removable=on,bus=usb-bus.0 \
  -drive "if=none,id=ale-boot,format=raw,readonly=on,file=$temporary/ale-boot.dmg" \
  -device usb-storage,drive=ale-boot,id=ale-boot-device,removable=on,bus=usb-bus.0 \
  -drive "if=none,id=system,format=qcow2,file=$output,discard=unmap" \
  -device nvme,drive=system,serial=ALEWIN11ARM64,bootindex=1 \
  -netdev user,id=network \
  -device virtio-net-pci,netdev=network \
  -qmp "unix:$temporary/qmp.sock,server=on,wait=off" \
  -display none -monitor none -serial none \
  > "$temporary/qemu.log" 2>&1 &
qemu_pid=$!

python3 "$source_dir/qmp_boot.py" "$temporary/qmp.sock"
timeout_seconds="${ALE_WINDOWS_BUILD_TIMEOUT_SECONDS:-7200}"
[[ "$timeout_seconds" =~ ^[1-9][0-9]*$ ]] || {
  echo "ALE_WINDOWS_BUILD_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 65
}
deadline_epoch=$(($(date +%s) + timeout_seconds))
echo "waiting for Windows ARM64 build (timeout: ${timeout_seconds}s)"
while kill -0 "$qemu_pid" 2>/dev/null; do
  if (( $(date +%s) >= deadline_epoch )); then
    echo "Windows ARM64 build timed out; QEMU log follows" >&2
    tail -100 "$temporary/qemu.log" >&2
    exit 70
  fi
  sleep 5
done
if ! wait "$qemu_pid"; then
  echo "Windows ARM64 QEMU failed; log follows" >&2
  tail -100 "$temporary/qemu.log" >&2
  exit 70
fi
qemu_pid=""

qemu-img check "$output"
disk_bytes="$(stat -f %z "$output")"
if (( disk_bytes < 1073741824 )); then
  echo "Windows installation did not populate the output disk: $output" >&2
  exit 70
fi
echo "prepared Windows 11 ARM64 disk: $output"
