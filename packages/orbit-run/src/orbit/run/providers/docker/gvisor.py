"""What a Docker sandbox under gVisor needs that a runc one does not.

gVisor's ``runsc`` runs a container on the Sentry, a kernel in user space, whose network
stack, netstack, takes the container's address and routes at boot and sends and receives
every frame through a packet socket on the container's veth: the namespace's netfilter never
sees them, and netstack never sees a network attached later. So a gVisor sandbox boots on a
bridge network of its own, which is not internal: tc filters on its veth's egress, in its
network namespace outside the sandbox, seal it, and opening egress removes them. Orbit's own
chains in netstack, set by :mod:`orbit.run.iptables`, route its Gateway and make what the
seal drops fail at once. This module builds both, the sandbox's size, and what a sandbox
that runs a container engine is given; the Provider applies them.
"""

from __future__ import annotations

import json
from pathlib import Path

from orbit.core.sandbox import SANDBOX_GATEWAY_PORT
from orbit.run import guestd, iptables
from orbit.run.providers.docker import tc

__all__ = [
    "ARGUMENTS",
    "BANNER",
    "BUILD",
    "BUILD_ID",
    "DRIVERS",
    "ENGINE_CAPABILITIES",
    "ENGINE_STORAGE",
    "FLAGS",
    "GPU_MEMORY_FLAG",
    "NESTED",
    "NET_RAW",
    "OOM_KILLS",
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

#: The runtime a gVisor sandbox runs under, the one for a sandbox that needs raw sockets,
#: and the one for a sandbox that runs a container engine, which ``runsc`` gives only to a
#: whole sandbox, from its registration.
RUNTIME = "runsc"
NET_RAW = "runsc-net-raw"
NESTED = "runsc-nested"
#: The ``runtimeArgs`` each is registered with (images/runtimes/gvisor/README.md), no more:
#: ``--overlay2=none`` keeps the sandbox's writes where Docker sees them, as snapshots need,
#: and ``--allow-suid`` honours setuid programs, ``sudo`` among them, as runc does.
#: ``--TESTONLY-nftables`` gives netstack the nf_tables a distribution's iptables and
#: container engines set their rules through; without ``--net-raw``, since a dockerd that
#: can open a packet socket but not write one fails to start a container.
ARGUMENTS = {
    RUNTIME: ("--overlay2=none", "--allow-suid", "--nvproxy-allowed-driver-capabilities=all"),
    NET_RAW: (
        "--overlay2=none",
        "--allow-suid",
        "--nvproxy-allowed-driver-capabilities=all",
        "--net-raw",
    ),
    NESTED: (
        "--overlay2=none",
        "--allow-suid",
        "--nvproxy-allowed-driver-capabilities=all",
        "--TESTONLY-nftables",
    ),
}
#: What root in a sandbox that runs a container engine holds beyond Docker's default
#: capabilities, all of them in the Sentry alone: ``NET_ADMIN`` for the engine's bridges and
#: rules, ``SYS_ADMIN`` for its mounts and namespaces, and ``SYS_RESOURCE`` for an init
#: script that raises its limits, as Debian's ``/etc/init.d/docker`` raises open files.
ENGINE_CAPABILITIES = ("NET_ADMIN", "SYS_ADMIN", "SYS_RESOURCE")
#: Where Docker, containerd and Podman keep their images and containers when run as root.
#: Each gets a Docker volume of its own: the engine's overlay layers mark what they delete
#: with whiteouts, which the Host's overlayfs, the sandbox's root, refuses to create.
ENGINE_STORAGE = ("/var/lib/docker", "/var/lib/containerd", "/var/lib/containers")
#: The version of Orbit's build of runsc this engine runs, ``VERSION`` in
#: images/runtimes/gvisor/build.sh; the id of that build, which ``build.sh --id`` prints and
#: which tells the builds of one version apart; and what ``runsc --version`` reports of it.
VERSION = "0.1.0"
BUILD_ID = "93effe41b277"
BUILD = f"+orbit.{VERSION}.{BUILD_ID}"
#: The ``runsc`` command that lists the NVIDIA driver versions nvproxy supports.
DRIVERS = ("nvproxy", "list-supported-drivers")
#: What the kernel log of every gVisor sandbox starts with.
BANNER = "Starting gVisor"
#: The flag of Orbit's runsc (patch 0004) that limits the device memory a sandbox allocates
#: through nvproxy on each of its GPUs, which ``runsc flags`` lists. A sandbox's annotation
#: sets it for that sandbox alone.
GPU_MEMORY_FLAG = "nvproxy-gpu-memory-limit"
#: Every runsc process is charged to the container's memory, so an application's process,
#: run in a stub, must be the one the OOM killer takes rather than the Sentry, which holds
#: the whole sandbox; under rootless Docker the default ties them.
FLAGS = ("--oom-score-adj", "500")
#: Where the Sentry counts, in ``oom_kill``, the processes it ended to keep the sandbox
#: below its cgroup's limit on the Host (patch 0009): the root of the cgroup v1 memory
#: hierarchy a Docker container sees, which the Host's cgroup never learns of.
OOM_KILLS = "/sys/fs/cgroup/memory/memory.oom_control"
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

    The Sentry, the gofer and the stubs are charged to the container, the Sentry keeps 16 MiB
    of it free (patch 0009), and the memory watcher runs beside guestd. Beside the framework's
    processes alone, one process got 24 to 72 MiB less than under runc with 60 MiB and 1 MiB
    per GiB added, at 512 MiB, 2, 4 and 8 GiB alike, as the Sentry's own memory varied from
    run to run: 108 MiB puts runc's share in the middle of that spread.
    """
    return memory_mb + 108 + memory_mb // 1024


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
    """Orbit's chains in a gVisor sandbox's netstack, as :mod:`orbit.run.iptables` takes them.

    ``ORBIT_GATEWAY`` sends the fixed Gateway port to ``gateway_port``, when there is one.
    An isolated sandbox's ``ORBIT_HOST`` refuses every port of the Host's address but the
    episode's ``ports``, always; while it is sealed, ``ORBIT_SEAL`` refuses everything else
    it sends and ``ORBIT_INPUT`` drops what it did not ask for. Refused, a connection fails
    at once rather than waiting out the drop outside. ``FORWARD`` jumps too, for what a
    sandbox forwards between network namespaces of its own.
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
            "ORBIT_HOST": [*episode, *({"dst": host_ip} | refusal for refusal in reject)],
            "ORBIT_SEAL": outward,
            "ORBIT_INPUT": inbound,
        }
        outbound = ["ORBIT_HOST", "ORBIT_SEAL"]
        jumps = {"OUTPUT": outbound, "FORWARD": outbound, "INPUT": ["ORBIT_INPUT"]}
        confined = {"chains": chains, "jumps": jumps}
    return {"tables": {"nat": nat, "filter": confined}}


def set_netstack(container: str, state: dict[str, object]) -> list[str]:
    """The ``docker`` command that sets ``state`` in a sandbox's netstack.

    A privileged exec as root holds every capability there, ``CAP_NET_ADMIN`` among them,
    which a sandbox's own processes lack unless it runs a container engine, but
    ``CAP_NET_RAW`` only under ``runsc-net-raw``, which is why the rules are written by the
    engine's own writer rather than an iptables the image may not carry.
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
