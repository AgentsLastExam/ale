#!/usr/bin/env bash
# Start the virtual machine ALE was handed, and refuse clearly when it cannot.
#
# The checks below all failed at least once during bring-up, and each failed in a way that
# looked like something else: a missing device looked like a slow boot, an empty disk
# looked like a guest with no service, a broken backing chain looked like a network fault.
# Failing here with a specific exit code turns each of them into one line.
set -Eeuo pipefail

storage_dir="${STORAGE:-/storage}"
disk_name="${DISK_NAME:-data}"
disk_path="$storage_dir/$disk_name.qcow2"
guest_port="${ALE_GUEST_PORT:-7411}"

if [[ ! -c /dev/kvm || ! -r /dev/kvm || ! -w /dev/kvm ]]; then
  echo "the ALE qemu runner requires readable and writable /dev/kvm" >&2
  exit 69
fi

if [[ ! -s "$disk_path" ]]; then
  echo "the ALE qemu runner requires a non-empty guest disk at $disk_path" >&2
  exit 64
fi

# An overlay whose backing file resolves on the host but not in here reads as a valid
# qcow2 until something tries to open the chain. That is this check.
if ! qemu-img info --backing-chain "$disk_path" >/dev/null; then
  echo "the ALE qemu runner could not open the guest disk chain at $disk_path" >&2
  exit 65
fi

# The upstream startup treats this marker as an already-installed guest. ALE always
# supplies a pre-baked disk, so there is nothing to install for any guest OS.
touch "$storage_dir/windows.boot"

if [[ -n "${ALE_QEMU_VFIO_DEVICES:-}" ]]; then
  IFS=',' read -r -a vfio_devices <<<"$ALE_QEMU_VFIO_DEVICES"
  vfio_arguments=""
  for bdf in "${vfio_devices[@]}"; do
    if [[ ! "$bdf" =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$ ]]; then
      echo "invalid ALE_QEMU_VFIO_DEVICES entry: $bdf" >&2
      exit 64
    fi
    vfio_arguments+=" -device vfio-pci,host=$bdf"
  done
  export ARGUMENTS="${ARGUMENTS:-}${vfio_arguments}"
fi

echo "starting the ALE guest from $disk_path"
echo "the screen is on container port 8006; the guest service is expected on ${guest_port}"

# Docker publishes the runner's port from another subnet. Present those connections to
# the guest as its gateway so replies follow the same bridge back to the host.
iptables -t nat -A POSTROUTING -p tcp -d 172.30.0.2 --dport "$guest_port" -j MASQUERADE

# tini becomes PID 1 and forwards signals, so the container dies with the machine rather
# than outliving it — an episode that leaks a running VM leaks its memory and its disk.
exec /usr/bin/tini -s -- /run/entry.sh
