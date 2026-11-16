"""What a Docker sandbox under gVisor needs that a runc one does not.

gVisor's ``runsc`` runs a container on the Sentry, a kernel in user space, whose network
stack, netstack, takes the container's address and routes at boot and sends and receives
every frame through a packet socket on the container's veth: the namespace's netfilter never
sees them, and netstack never sees a network attached later. So a gVisor sandbox boots on a
bridge network of its own, which is not internal: tc filters on its veth's egress, in its
network namespace outside the sandbox, seal it, and opening egress removes them. ALE's own
chains in netstack, set by :mod:`ale.run.iptables`, route its Gateway and make what the seal
drops fail at once. This module builds both and the sandbox's size; the Provider applies
them.
"""

from __future__ import annotations

import json
from pathlib import Path

from ale.core.sandbox import SANDBOX_GATEWAY_PORT
from ale.run import guestd, iptables
from ale.run.providers.docker import tc

__all__ = [
    "ARGUMENTS",
    "BANNER",
    "BUILD",
    "DRIVERS",
    "FLAGS",
    "NET_RAW",
    "RUNTIME",
    "SEAL_PROBE",
    "VERSION",
    "VETH",
    "filters",
    "memory_mb",
    "netstack",
    "seal",
    "set_netstack",
    "unseal",
]

#: The runtime a gVisor sandbox runs under, and the one for a sandbox that needs raw
#: sockets, which ``runsc`` gives only to a whole sandbox, from its registration.
RUNTIME = "runsc"
NET_RAW = "runsc-net-raw"
#: The ``runtimeArgs`` each is registered with (images/runtimes/gvisor/README.md), no more:
#: ``--overlay2=none`` keeps the sandbox's writes where Docker sees them, as snapshots need,
#: and ``--allow-suid`` honours setuid programs, ``sudo`` among them, as runc does.
ARGUMENTS = {
    RUNTIME: ("--overlay2=none", "--allow-suid", "--nvproxy-allowed-driver-capabilities=all"),
    NET_RAW: (
        "--overlay2=none",
        "--allow-suid",
        "--nvproxy-allowed-driver-capabilities=all",
        "--net-raw",
    ),
}
#: The version of ALE's build of runsc this engine runs, ``VERSION`` in
#: images/runtimes/gvisor/build.sh, and what ``runsc --version`` reports of it.
VERSION = "0.1.0"
BUILD = f"+ale.{VERSION}"
#: The ``runsc`` command that lists the NVIDIA driver versions nvproxy supports.
DRIVERS = ("nvproxy", "list-supported-drivers")
#: What the kernel log of every gVisor sandbox starts with.
BANNER = "Starting gVisor"
#: Every runsc process is charged to the container's memory, so an application's process,
#: run in a stub, must be the one the OOM killer takes rather than the Sentry, which holds
#: the whole sandbox; under rootless Docker the default ties them.
FLAGS = ("--oom-score-adj", "500")
#: The container's veth in the sandbox's network namespace. netstack sends every frame
#: through it, so the seal is on its egress: tc's ingress runs after netstack's packet
#: socket has already read a frame.
VETH = "eth0"
_GATEWAY_PREF = 10
_PROXY_PREF = 11
_HOST_PREF = 12
_SEAL_DROP_PREF = 21
SEAL_PROBE = tc.show(VETH, "egress", _SEAL_DROP_PREF)


def memory_mb(memory_mb: int) -> int:
    """The container memory that leaves a gVisor sandbox's processes what its Task declared.

    The Sentry, the gofer and the stubs are charged to the container. Beside the framework's
    processes alone, one process got 16 to 32 MiB less than under runc with 24 MiB and 1 MiB
    per GiB added, at 512 MiB, 2, 4 and 8 GiB alike: 60 MiB covers the largest gap with 4 to
    spare.
    """
    return memory_mb + 60 + memory_mb // 1024


def filters(peers: tc.Peers, *, gateway_port: str, proxy_port: str, reattached: bool) -> list[str]:
    """The tc commands that confine an isolated gVisor sandbox, from outside it.

    The wire carries the Gateway's own port, since netstack rewrites the fixed one, and only
    a frame from the sandbox's own addresses to the Host's passes. The sandbox reaches no
    other port of the Host's address and starts sealed. A reattaching run replaces only the
    Gateway and proxy filters, so the Host stays confined while it does.
    """
    commands = [f"test -e /sys/class/net/{VETH}", f"tc qdisc replace dev {VETH} clsact"]
    if not reattached:
        drop = f"protocol ip flower dst_ip {peers.host_ip} action drop"
        commands += [*tc.replace([(VETH, "egress", _HOST_PREF, drop)]), *seal(peers)]
    ports = [(_GATEWAY_PREF, gateway_port), (_PROXY_PREF, proxy_port)]
    if reattached:
        commands += tc.delete((VETH, "egress", pref, "") for pref, port in ports if not port)
    return commands + tc.replace(
        (
            VETH,
            "egress",
            pref,
            f"protocol ip flower {peers.to_host} ip_proto tcp dst_port {port} action pass",
        )
        for pref, port in ports
        if port
    )


def _seal_filters(peers: tc.Peers) -> list[tc.Filter]:
    """The filters that let a sealed gVisor sandbox send only to the Host's address: its own
    ARP for that address, and what the filters before them pass."""
    return [
        (VETH, "egress", 20, f"protocol arp flower {peers.arp} action pass"),
        (VETH, "egress", _SEAL_DROP_PREF, tc.DROP_ALL),
    ]


def seal(peers: tc.Peers) -> list[str]:
    """Seal it, or keep it sealed: each filter replaces its like, so sealing a sandbox that
    is sealed never leaves it without its drop."""
    return tc.replace(_seal_filters(peers))


def unseal(peers: tc.Peers) -> list[str]:
    """Remove the seal, then check that its drop filter is gone."""
    return tc.remove(_seal_filters(peers))


def netstack(
    host_ip: str, *, gateway_port: str, ports: list[str], isolated: bool, sealed: bool
) -> dict[str, object]:
    """ALE's chains in a gVisor sandbox's netstack, as :mod:`ale.run.iptables` takes them.

    ``ALE_GATEWAY`` sends the fixed Gateway port to ``gateway_port``, when there is one.
    An isolated sandbox's ``ALE_HOST`` refuses every port of the Host's address but the
    episode's ``ports``, always; while it is sealed, ``ALE_SEAL`` refuses everything else it
    sends and ``ALE_INPUT`` drops what it did not ask for. Refused, a connection fails at
    once rather than waiting out the drop outside. ``FORWARD`` jumps too, for what a sandbox
    forwards between network namespaces of its own.
    """
    nat: dict[str, object] = {}
    if gateway_port:
        rule = {
            "dst": host_ip,
            "proto": "tcp",
            "dports": [SANDBOX_GATEWAY_PORT],
            "target": {"DNAT": f"{host_ip}:{gateway_port}"},
        }
        nat = {
            "chains": {iptables.GATEWAY_CHAIN: [rule]},
            "jumps": {"OUTPUT": [iptables.GATEWAY_CHAIN]},
        }
    confined: dict[str, object] = {}
    if isolated:
        episode: list[dict[str, object]] = []
        if ports:
            dports = [int(port) for port in ports]
            episode.append({"dst": host_ip, "proto": "tcp", "dports": dports, "target": "ACCEPT"})
        reject: list[dict[str, object]] = [
            {"proto": "tcp", "target": "REJECT_TCP_RESET"},
            {"target": "REJECT"},
        ]
        outward: list[dict[str, object]] = []
        inbound: list[dict[str, object]] = []
        if sealed:
            outward = [{"out": "lo", "target": "ACCEPT"}, *episode, *reject]
            inbound = [
                {"in": "lo", "target": "ACCEPT"},
                {"ctstate": ["ESTABLISHED", "RELATED"], "target": "ACCEPT"},
                {"target": "DROP"},
            ]
        chains = {
            "ALE_HOST": [*episode, *({"dst": host_ip} | refusal for refusal in reject)],
            "ALE_SEAL": outward,
            "ALE_INPUT": inbound,
        }
        outbound = ["ALE_HOST", "ALE_SEAL"]
        jumps = {"OUTPUT": outbound, "FORWARD": outbound, "INPUT": ["ALE_INPUT"]}
        confined = {"chains": chains, "jumps": jumps}
    return {"tables": {"nat": nat, "filter": confined}}


def set_netstack(container: str, state: dict[str, object]) -> list[str]:
    """The ``docker`` command that sets ``state`` in a sandbox's netstack.

    A privileged exec as root holds every capability there, ``CAP_NET_ADMIN`` among them,
    which a sandbox's own processes lack, but ``CAP_NET_RAW`` only under ``runsc-net-raw``,
    which is why the rules are written by the engine's own writer rather than an iptables
    the image may not carry.
    """
    source = Path(iptables.__file__).read_text()
    python = guestd.PYTHON["linux"]
    return [
        "exec",
        "--privileged",
        "-u",
        "0",
        container,
        python,
        "-I",
        "-c",
        source,
        json.dumps(state),
    ]
