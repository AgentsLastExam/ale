"""What a Docker sandbox in a Kata Containers microVM needs that a runc one does not.

Kata starts each container as a QEMU microVM with a guest kernel of its own. The VM takes
its networks at boot and sees none attached later, and Kata passes every frame between
the VM's tap and the container's veth with tc filters, past the namespace's netfilter. So
a Kata sandbox boots on a bridge network of its own, which is not internal, and tc filters
in its network namespace, outside the VM, route its Gateway and seal it. This module builds
those filters and the VM's size; the Provider applies them.
"""

from __future__ import annotations

from ale.core.errors import ProviderStartError
from ale.core.sandbox import SANDBOX_GATEWAY_PORT
from ale.run import guestd
from ale.run.providers.docker import tc
from ale.run.transport import GuestClient

__all__ = [
    "OOM_KILLS",
    "RUNTIME",
    "SEAL_PROBE",
    "TAP",
    "VETH",
    "filters",
    "memory_mb",
    "seal",
    "set_route",
    "unseal",
]

#: The containerd shim that starts each container in a Kata Containers microVM. Docker
#: runs it by this name from its PATH, so the daemon needs no setting and no restart.
RUNTIME = "io.containerd.kata.v2"
#: Where the guest kernel counts the container's processes it ended, ``oom_kill``, and its
#: limit reached, ``oom``: the microVM's memory is the Task's, so every kill is the
#: Sandbox's own.
OOM_KILLS = "/sys/fs/cgroup/memory.events"
#: The VM's tap and the container's veth in the sandbox's network namespace. Kata passes
#: every frame between them with tc filters, past the namespace's netfilter, so the
#: Provider's rules for a Kata sandbox are tc filters placed ahead of Kata's own.
TAP = "tap0_kata"
VETH = "eth0"
#: tc preferences: lower runs first, the seal's come next, and Kata's own sit at 49152.
_GATEWAY_PREF = 10
_PROXY_PREF = 11
_HOST_PREF = 12
#: The seal's drop filter on the VM's tap, whose presence says a sandbox is sealed, and
#: the tc command that shows it.
_SEAL_DROP_PREF = 21
SEAL_PROBE = tc.show(TAP, "ingress", _SEAL_DROP_PREF)


def memory_mb(memory_mb: int) -> int:
    """The VM size that leaves a Kata sandbox's processes the memory its Task declared.

    Kata makes the VM exactly the container's memory limit, and its guest kernel and agent
    take about 96 MiB plus 2.5 % of it (measured with Kata 4.3.1 from 512 MiB to 8 GiB).
    """
    return memory_mb + 96 + memory_mb // 40


def filters(
    peers: tc.Peers,
    *,
    gateway_port: str,
    proxy_port: str,
    isolated: bool,
    reattached: bool,
) -> list[str]:
    """The tc commands that give a Kata sandbox what netfilter gives a runc one.

    They act on frames between the VM and its veth, in the namespace the helper joins,
    outside the guest: root in the VM, which can write any frame, cannot lift them, and only
    frames between the sandbox's own addresses and the Host's pass them. The fixed Gateway
    port is rewritten to the Host's in both directions. An isolated sandbox reaches no other
    port of the Host's address and starts sealed. A reattaching run replaces only the
    Gateway and proxy filters, so the Host stays confined while it does.
    """
    host_ip = peers.host_ip
    commands = [f"test -e /sys/class/net/{TAP}", f"test -e /sys/class/net/{VETH}"]
    if isolated and not reattached:
        drop = f"protocol ip flower dst_ip {host_ip} action drop"
        commands += [*tc.replace([(TAP, "ingress", _HOST_PREF, drop)]), *seal(peers)]
    passed: list[tc.Filter] = []
    gone: list[tc.Filter] = []
    if gateway_port:
        rewrite = "action pedit ex munge tcp {field} set {port} pipe action csum tcp pipe"
        passed += [
            (
                TAP,
                "ingress",
                _GATEWAY_PREF,
                f"protocol ip flower {peers.to_host} ip_proto tcp "
                f"dst_port {SANDBOX_GATEWAY_PORT} "
                + rewrite.format(field="dport", port=gateway_port)
                + f" action mirred egress redirect dev {VETH}",
            ),
            (
                VETH,
                "ingress",
                _GATEWAY_PREF,
                f"protocol ip flower {peers.from_host} ip_proto tcp src_port {gateway_port} "
                + rewrite.format(field="sport", port=SANDBOX_GATEWAY_PORT)
                + f" action mirred egress redirect dev {TAP}",
            ),
        ]
    else:
        gone += [(device, "ingress", _GATEWAY_PREF, "") for device in (TAP, VETH)]
    if proxy_port and isolated:
        passed.append(
            (
                TAP,
                "ingress",
                _PROXY_PREF,
                f"protocol ip flower {peers.to_host} ip_proto tcp dst_port {proxy_port} "
                f"action mirred egress redirect dev {VETH}",
            )
        )
    else:
        gone.append((TAP, "ingress", _PROXY_PREF, ""))
    return commands + (tc.delete(gone) if reattached else []) + tc.replace(passed)


def _seal_filters(peers: tc.Peers) -> list[tc.Filter]:
    """The filters that pass a sealed Kata sandbox only its own frames to the Host's
    address and the Host's frames to it, from the bridge's MAC and address.

    Its own ARP for the Host's address passes too, so the guest keeps resolving it, and the
    Host's own ARP; nothing else does, IPv6 included. The Gateway and proxy filters run
    first and take their own frames.
    """
    to_veth = f"action mirred egress redirect dev {VETH}"
    to_tap = f"action mirred egress redirect dev {TAP}"
    return [
        (TAP, "ingress", 20, f"protocol arp flower {peers.arp} {to_veth}"),
        (TAP, "ingress", _SEAL_DROP_PREF, tc.DROP_ALL),
        (VETH, "ingress", 20, f"protocol ip flower {peers.from_host} {to_tap}"),
        (VETH, "ingress", 21, f"protocol arp flower {peers.host_arp} {to_tap}"),
        (VETH, "ingress", 22, tc.DROP_ALL),
    ]


def seal(peers: tc.Peers) -> list[str]:
    """Seal it, or keep it sealed: each filter replaces its like, so sealing a sandbox that
    is sealed never leaves it without its drops."""
    return tc.replace(_seal_filters(peers))


def unseal(peers: tc.Peers) -> list[str]:
    """Remove the seal, then check that no drop filter is left."""
    return tc.remove(_seal_filters(peers))


#: Run as root on the framework's Python in a Kata guest: ``del`` removes its default route,
#: ``add HOST`` restores it through the Host's address. Both are idempotent.
_ROUTE = """
import socket, struct, sys
action, gateway = sys.argv[1], sys.argv[2]
def attribute(kind, data):
    size = 4 + len(data)
    return struct.pack("=HH", size, kind) + data + bytes(-size % 4)
if action == "add":  # RTM_NEWROUTE, create or replace: main table, static, unicast
    kind, flags = 24, 0x1 | 0x4 | 0x400 | 0x100
    body = struct.pack("=BBBBBBBBI", socket.AF_INET, 0, 0, 0, 254, 4, 0, 1, 0)
    body += attribute(5, socket.inet_aton(gateway))
    body += attribute(4, struct.pack("=i", socket.if_nametoindex("eth0")))
else:  # RTM_DELROUTE of the main table's default route, any scope
    kind, flags = 25, 0x1 | 0x4
    body = struct.pack("=BBBBBBBBI", socket.AF_INET, 0, 0, 0, 254, 0, 255, 0, 0)
with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0) as link:
    link.send(struct.pack("=LHHLL", 16 + len(body), kind, flags, 1, 0) + body)
    reply = link.recv(4096)
error = struct.unpack("=i", reply[16:20])[0] if struct.unpack("=H", reply[4:6])[0] == 2 else 0
if error not in (0, -3):  # -ESRCH: there was no default route to delete
    sys.exit(f"could not {action} the default route: errno {-error}")
"""


async def set_route(client: GuestClient, host_ip: str, *, present: bool) -> None:
    """Remove a sealed Kata guest's default route, or restore it when it is unsealed.

    The tc filters keep a sealed sandbox in; this only makes what they drop fail at once,
    as it does on runc's internal network, which has no route off the Host. Dropped, a DNS
    lookup took 112 s to fail and a connection waited for its timeout.
    """
    action = "add" if present else "del"
    code, _, stderr, _ = await client.exec(
        [guestd.PYTHON["linux"], "-I", "-c", _ROUTE, action, host_ip], timeout_sec=30
    )
    if code != 0:
        raise ProviderStartError(f"could not {action} the Kata guest's route: {stderr.strip()}")
