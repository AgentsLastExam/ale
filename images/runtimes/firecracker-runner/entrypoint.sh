#!/usr/bin/env bash
# Start the microVM ALE described in /storage/vm.json, behind a network of its own.
#
# The guest is 172.30.0.2 on tap0; this namespace is 172.30.0.1, the only address a sealed
# guest may reach. Docker publishes the guest service's port to the Host, and the rules
# below send it on to the guest. What confines the guest is added later by the Provider
# with `docker exec`, because the framework's own phases need the network first.
set -Eeuo pipefail

storage=/storage
guest=172.30.0.2
host=172.30.0.1
guest_port="${ALE_GUEST_PORT:-7411}"

if [[ ! -c /dev/kvm || ! -r /dev/kvm || ! -w /dev/kvm ]]; then
  echo "the ALE Firecracker runner requires readable and writable /dev/kvm" >&2
  exit 69
fi
if [[ ! -c /dev/net/tun ]]; then
  echo "the ALE Firecracker runner requires /dev/net/tun" >&2
  exit 69
fi
if [[ ! -s "$storage/vm.json" || ! -s "$storage/sandbox.json" ]]; then
  echo "the ALE Firecracker runner requires vm.json and sandbox.json in $storage" >&2
  exit 64
fi
if [[ "$(cat /proc/sys/net/ipv4/ip_forward)" != 1 ]]; then
  echo "the ALE Firecracker runner requires --sysctl net.ipv4.ip_forward=1" >&2
  exit 69
fi

ip tuntap add dev tap0 mode tap
ip addr add "$host/24" dev tap0
ip link set tap0 up

# The framework's phases, and open Tasks throughout, reach out through the container's
# own interface.
iptables -w -t nat -A POSTROUTING -s "$guest/32" -o eth0 -j MASQUERADE
# The guest service, from the published port. Its connections are presented as coming
# from this namespace, so the guest needs no route back to Docker's subnet.
iptables -w -t nat -A PREROUTING -i eth0 -p tcp --dport "$guest_port" \
  -j DNAT --to-destination "$guest:$guest_port"
iptables -w -t nat -A POSTROUTING -o tap0 -p tcp -d "$guest" --dport "$guest_port" -j MASQUERADE

# The sandbox's writable disk: a sparse ext4 the guest lays over its read-only root. It
# starts with the sandbox's own settings and this namespace's resolvers, which the guest's
# init reads before anything else runs.
if [[ ! -e "$storage/rw.ext4" ]]; then
  seed=$(mktemp -d)
  mkdir -m 0700 "$seed/ale"
  cp "$storage/sandbox.json" "$seed/ale/sandbox.json"
  grep '^nameserver' /etc/resolv.conf > "$seed/ale/resolv.conf" || true
  truncate -s "${RW_DISK_MB:?}M" "$storage/rw.ext4"
  mke2fs -q -F -t ext4 -E lazy_itable_init=1,lazy_journal_init=1,root_owner=0:0 \
    -d "$seed" "$storage/rw.ext4"
  rm -rf "$seed"
fi

echo "starting the ALE microVM; the guest service is expected on $guest:$guest_port"
exec /usr/local/bin/firecracker --api-sock /run/firecracker.sock --config-file "$storage/vm.json"
