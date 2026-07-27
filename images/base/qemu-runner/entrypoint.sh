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

echo "starting the ALE guest from $disk_path"
echo "the screen is on container port 8006; the guest service is expected on ${guest_port}"

# tini becomes PID 1 and forwards signals, so the container dies with the machine rather
# than outliving it — an episode that leaks a running VM leaks its memory and its disk.
exec /usr/bin/tini -s -- /run/entry.sh
