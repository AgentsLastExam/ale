"""Container backend.

Shells out to the docker CLI rather than binding an SDK: the CLI is stable, already
installed wherever docker is, and keeps this provider readable.

Network isolation is topological, not rule-based. Each run gets its own
``--internal`` bridge, which has no route off the host, and the gateway is attached to
that bridge. Deny-all with a single controlled egress therefore falls out of the
network's shape — there are no firewall rules to survive a daemon restart, and nothing
inside the sandbox can lift them.

Under the Kata runtime each container is a microVM, and under gVisor it runs on a kernel in
user space with a network stack of its own; neither sees a network attached after it boots,
nor lets the namespace's netfilter see its packets. There the sandbox boots on a bridge of
its own that is not internal, and tc filters in its network namespace, outside the sandbox,
seal it instead (``kata.py``, ``gvisor.py``). Each runtime's entry in :data:`RUNTIMES`
holds all that differs between them: its grants, GPUs, size, ``docker run`` flags and
confinement.

A Sandbox takes its GPUs whole, or shares each with others by the device memory its Task
names, admitted by what is free and watched while it lives (``gpu_sharing.py``). One that
names ``storage_mb`` is measured while it lives, and stopped once it has written more
(``storage.py``). Under rootless
Docker, whose bridges are in a network namespace of rootlesskit's, a listener there relays
each of its connections to the Host's Gateway and proxy (``rootless.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import csv
import io
import json
import logging
import os
import random
import re
import shutil
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import IPv4Network
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from orbit.core.capability import CapabilityProfile, GpuHold, GpuModel, SandboxNeeds, accepts_model
from orbit.core.config import GPU_MEMORY_MARGIN_MB
from orbit.core.errors import (
    GuestUnreachableError,
    MemoryExhaustedError,
    ProviderCapabilityError,
    ProviderStartError,
    StorageExhaustedError,
)
from orbit.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    DesktopResolution,
    ExecOutputSink,
    ExecResult,
    GpuAllocation,
    GpuDevice,
    Identity,
    ImageKind,
    ImageRef,
    PreparedTaskImage,
    Provider,
    ResolvedImage,
    ResourceAllocation,
    RetainedSandbox,
    Sandbox,
    SandboxCheck,
    SandboxFile,
    SandboxRequest,
    SandboxRole,
    SandboxState,
)
from orbit.core.taskspec import ContainerFeature, NetworkMode, OperatingSystem, Resources
from orbit.run import HELPER_RUNTIME, guestd, orbit_image
from orbit.run.framework import with_framework
from orbit.run.gpu import parse_nvidia_smi
from orbit.run.images import resolve_container_image, resolve_prepared_container_image
from orbit.run.iptables import GATEWAY_CHAIN
from orbit.run.process import run
from orbit.run.providers import machine_arch, memory, relay
from orbit.run.providers.desktop import CONTAINER_VARIABLE, require_resolution
from orbit.run.providers.docker import (
    gpu_sharing,
    gvisor,
    kata,
    rootless,
    storage,
    subnets,
    tc,
    volumes,
)
from orbit.run.providers.privileges import set_and_prove_elevation
from orbit.run.transport import GuestClient, StdioTransport

__all__ = [
    "DockerProvider",
    "DockerSandbox",
]

_log = logging.getLogger(__name__)

GUESTD_DIR = PurePosixPath("/opt/orbit/guestd")
#: Any image with iptables, and tc for a Kata or gVisor sandbox, can apply a sandbox's host
#: rules; Orbit's QEMU runner, which needs both for its own guest, is one.
NETFILTER_IMAGE = orbit_image("orbit-qemu-runner")

LABEL = "orbit.episode"
MANAGED_LABEL = "orbit.managed"
ROLE_LABEL = "orbit.role"
RETENTION_LABEL = "orbit.retention"
#: The network mode a container was started with, which reattaching confines it by.
NETWORK_LABEL = "orbit.network"
#: The MAC of the Host's end of a Kata or gVisor sandbox's own network, which its seal's
#: filters match: read before the sandbox starts, and kept for a reattaching run.
BRIDGE_MAC_LABEL = "orbit.bridge-mac"
GPU_LABEL = "orbit.gpus"
#: What a Sandbox that shares its GPUs reserved on each, in MiB.
GPU_MEMORY_LABEL = "orbit.gpu_memory_mb"
PREPARED_LABEL = "orbit.prepared"
#: On a container, the bytes written over its Task's image that the snapshot it booted from
#: holds, which its ``storage_mb`` counts; on a snapshot, all it holds over that image.
WRITTEN_LABEL = "orbit.written"
#: Set on a container that mounts its request's ``shown`` directory, which a reattaching run
#: finds mounted rather than copies.
SHOWN_LABEL = "orbit.shown"
HANDLE_PREFIX = "docker:"
#: A snapshot is a local image in this repository, tagged with its Sandbox's ID.
SNAPSHOT_REPOSITORY = "orbit-snapshot"
#: Prints the owner and mode of the directory it is given, which a volume artifact's view
#: restores at its root.
_STAT_ROOT = (
    "import os,stat,sys; s=os.stat(sys.argv[1]); print(s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode))"
)
#: Gives a view's root the owner and mode its volume had.
_SET_ROOT = "import os,sys; p,u,g,m=sys.argv[1:]; os.chown(p, int(u), int(g)); os.chmod(p, int(m))"
SNAPSHOT_PREFIX = f"{HANDLE_PREFIX}snapshot:"

#: The name a sandbox uses for its gateway. It is mapped to the host's address *on the
#: container's own bridge* rather than to docker's usual host-gateway: an ``--internal``
#: network has no default route at all, so the only address that works is one inside the
#: sandbox's own subnet. That is the provider's half of the deny-all bargain — no route
#: off the host, and exactly one destination still reachable.
HOST_ALIAS = "orbit-gateway.internal"


_SHIM = re.compile(r"io\.containerd\.(\w+)\.(v\d+)")

#: Prints the kernel log of the Sandbox it runs in (``syslog(SYSLOG_ACTION_READ_ALL)``), on
#: the framework's Python, whatever the image has.
_KERNEL_LOG = """
import ctypes
log = ctypes.create_string_buffer(1 << 20)
size = ctypes.CDLL(None, use_errno=True).klogctl(3, log, len(log))
print(log.raw[: max(size, 0)].decode("utf-8", "replace"))
"""

_NVIDIA_QUERY = "uuid,name,pci.bus_id,driver_version"
_HOST_NVIDIA_QUERY = f"index,{_NVIDIA_QUERY},memory.total,memory.reserved,memory.free"


async def _gpu_holders() -> list[gpu_sharing.Holder]:
    """Every running Orbit Sandbox that holds GPUs, by its labels, which are the record of what
    Orbit holds through this Docker daemon.

    Each line is ``<id> <name> <gpus label>/<gpu memory label>``. Every Sandbox is started with
    both labels, empty when it holds no GPU or holds them whole, so an image's labels never
    stand in for them; Orbit's helpers have neither, and hold nothing.
    """
    code, output, stderr = await _docker(
        "ps",
        "--no-trunc",
        "--filter",
        f"label={MANAGED_LABEL}=true",
        "--format",
        f'{{{{.ID}}}} {{{{.Names}}}} {{{{.Label "{GPU_LABEL}"}}}}/'
        f'{{{{.Label "{GPU_MEMORY_LABEL}"}}}}',
        timeout=30,
    )
    if code != 0:
        raise ProviderStartError(f"could not list Orbit's Sandboxes: {stderr.strip()}")
    found: list[gpu_sharing.Holder] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        container, name, labels = line.split()
        gpus, _, memory = labels.partition("/")
        if gpus:
            found.append(
                gpu_sharing.Holder(
                    container, name, tuple(gpus.split(",")), int(memory) if memory else None
                )
            )
    return found


#: How long ``docker run`` may take to start a container, a sandbox or a helper: on a Host
#: under a load of about 200, creating one and attaching it to its network took 65 s.
_START_SEC = 300.0
#: How long Docker may take to remove a container or a network, under such a load: removing
#: a killed sandbox took 31 s there.
_REMOVE_SEC = 300.0


async def _docker(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
    return await run("docker", *argv, timeout=timeout)


def _parse_nvidia_smi(output: str) -> tuple[GpuDevice, ...]:
    return parse_nvidia_smi(output)


def _mib(value: str) -> int | None:
    """A memory column of nvidia-smi's, which says ``[N/A]`` where it is not supported."""
    value = value.strip()
    return int(value) if value.isdigit() else None


def _parse_host_nvidia_smi(output: str) -> dict[int, GpuDevice]:
    devices: dict[int, GpuDevice] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            raw_index, device_row = line.split(",", 1)
            index = int(raw_index.strip())
            device_row, total, reserved, free = device_row.rsplit(",", 3)
        except ValueError as exc:
            raise ProviderCapabilityError(
                f"could not parse nvidia-smi's GPU row: {line!r}"
            ) from exc
        if index < 0 or index in devices:
            raise ProviderCapabilityError(f"invalid or duplicate nvidia-smi GPU index: {index}")
        devices[index] = parse_nvidia_smi(device_row)[0].model_copy(
            update={
                "memory_total_mb": _mib(total),
                "memory_reserved_mb": _mib(reserved),
                "memory_free_mb": _mib(free),
            }
        )
    if not devices:
        raise ProviderCapabilityError("nvidia-smi reported no physical GPUs")
    return devices


def _eligible_gpus(
    host_devices: dict[int, GpuDevice], configured: tuple[int, ...], resources: Resources
) -> tuple[str, ...]:
    """The UUIDs of the host GPUs the Task accepts, among the configured indices or, when
    none are configured, among all of them."""
    indices = configured or tuple(sorted(host_devices))
    unknown = sorted(set(indices) - set(host_devices))
    if unknown:
        raise ProviderCapabilityError(
            "configured Docker GPU indices were not discovered: "
            + ", ".join(str(index) for index in unknown)
        )
    eligible = tuple(
        host_devices[index].id
        for index in indices
        if accepts_model(resources.gpu_types, host_devices[index].model)
    )
    if resources.gpu_types and len(eligible) < resources.gpus:
        usable = Counter(host_devices[index].model for index in indices)
        raise ProviderCapabilityError(
            f"the Task needs {resources.gpus} GPUs of gpu_types {list(resources.gpu_types)}; "
            "Docker may use " + ", ".join(f"{count} {model}" for model, count in usable.items())
        )
    return eligible


def _gpu_run_args(uuids: tuple[str, ...]) -> list[str]:
    # Docker reads --gpus as one CSV record, so an unquoted comma would make the second
    # UUID a count field; the quotes keep the whole device list in one field.
    # The NVIDIA runtime hook mounts only the driver files of the requested capabilities.
    # A Host on the CDI path injects the whole driver whatever is requested, so all gives
    # a sandbox on the hook path the same driver.
    return [
        "--gpus",
        f'"device={",".join(uuids)}"',
        "--env",
        "NVIDIA_DRIVER_CAPABILITIES=all",
    ]


async def _in_namespace(container: str, script: str) -> tuple[int, str, str]:
    """Run a shell script with NET_ADMIN in the sandbox's network namespace on the Host,
    where the sandbox has none."""
    return await _docker(
        "run", "--rm", *HELPER_RUNTIME, "--network", f"container:{container}",
        "--cap-add", "NET_ADMIN", "--entrypoint", "sh", NETFILTER_IMAGE, "-c", script,
        timeout=_START_SEC,
    )  # fmt: skip


async def _netfilter_helper(container: str, commands: list[str], purpose: str) -> None:
    code, _, stderr = await _in_namespace(container, " && ".join(commands))
    if code != 0:
        raise ProviderStartError(f"could not {purpose}: {stderr.strip()}")


async def _remove_network(network: str) -> None:
    """Remove a sandbox's own network, saying where the operator looks when it stays: it
    holds its subnet, and nothing else removes it."""
    code, _, stderr = await _docker("network", "rm", network, timeout=_REMOVE_SEC)
    if code != 0 and "not found" not in stderr:
        _log.warning("could not remove network %s: %s", network, stderr.strip())


async def _discard_network(network: str) -> None:
    """Remove a network Orbit created for a sandbox that did not start, even while the task
    that created it is cancelled."""
    with contextlib.suppress(BaseException):
        await asyncio.shield(_docker("network", "rm", network, timeout=_REMOVE_SEC))


async def _bridge_host_ip(network: str | None) -> str:
    """The host's address on a container's network, the default bridge's for ``None``.

    On an isolated bridge this is the only host address a sandbox can dial, which is
    why it is read from the network rather than assumed.
    """
    network = network or "bridge"
    code, out, stderr = await _docker(
        "network", "inspect", network, "--format", "{{(index .IPAM.Config 0).Gateway}}"
    )
    if code != 0 or not out.strip():
        raise ProviderStartError(f"could not read the gateway address of {network}: {stderr}")
    return out.strip()


#: Run by bash in a helper on a network: sends one datagram to the Host's address, ``$1``,
#: and prints the MAC its neighbour entry resolves to.
_NEIGHBOUR_MAC = r"""
echo > "/dev/udp/$1/9" || exit 1
for _ in $(seq 50); do
    while read -r address _ flags mac _; do
        [ "$address" = "$1" ] && [ "$flags" = 0x2 ] && { echo "$mac"; exit 0; }
    done < /proc/net/arp
    sleep 0.1
done
echo "no neighbour entry for $1" >&2
exit 1
"""
_MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")


async def _bridge_mac(network: str, host_ip: str) -> str:
    """The MAC of the Host's end of ``network``, as a container on it resolves ``host_ip``.

    Read by a helper on the network itself, removed before the caller goes on, so that it is
    never a neighbour of the sandbox started there next. It needs no view of the bridge's
    device, which a helper on ``--network host`` lacks under rootless Docker whose network
    namespace rootlesskit detaches (``--detach-netns``).
    """
    helper = f"orbit-mac-{uuid.uuid4().hex[:12]}"
    try:
        code, out, stderr = await _docker(
            "run", "--rm", "--name", helper, *HELPER_RUNTIME, "--network", network,
            "--entrypoint", "bash", NETFILTER_IMAGE, "-c", _NEIGHBOUR_MAC, "bash", host_ip,
            timeout=_START_SEC,
        )  # fmt: skip
    finally:
        with contextlib.suppress(BaseException):
            await asyncio.shield(_docker("rm", "-f", helper, timeout=_REMOVE_SEC))
    mac = out.strip()
    if code != 0 or not _MAC.fullmatch(mac):
        raise ProviderStartError(
            f"could not read the MAC of {network}'s bridge: {stderr.strip() or mac}"
        )
    return mac


class _Networks:
    """Docker's networks as the Provider's sandboxes use them, each fact read once: the
    Host's address on the default bridge, at which an ``open`` runc sandbox reaches its
    Gateway, the ``resolv.conf`` Docker writes a container there, which names the Host's
    upstream servers, and the subnets a sandbox's own network may take."""

    def __init__(self) -> None:
        self._host_ip: str | None = None
        self._resolv_conf: str | None = None
        self._free: list[IPv4Network] | None = None
        self._reading = asyncio.Lock()

    async def host_ip(self) -> str:
        if self._host_ip is None:
            self._host_ip = await _bridge_host_ip(None)
        return self._host_ip

    async def resolv_conf(self) -> str:
        """As Docker wrote it for a helper on the default bridge: one helper, however many
        sandboxes ask at once, each of which would otherwise start its own."""
        if self._resolv_conf is None:
            async with self._reading:
                if self._resolv_conf is None:
                    code, out, stderr = await _docker(
                        "run", "--rm", *HELPER_RUNTIME, "--network", "bridge",
                        "--entrypoint", "cat", NETFILTER_IMAGE, "/etc/resolv.conf",
                        timeout=_START_SEC,
                    )  # fmt: skip
                    if code != 0 or "nameserver" not in out:
                        raise ProviderStartError(
                            f"could not read the default bridge's resolv.conf: {stderr.strip()}"
                        )
                    self._resolv_conf = out
        return self._resolv_conf

    async def create(self, name: str, *, internal: bool = False) -> str:
        """Create a sandbox's own network, ``name``, on a subnet of its own; the Host's
        address on it.

        Not from Docker's address pools, which give out about 31 networks: a /29 of
        :data:`subnets.BLOCK` that no route in the daemon's network namespace overlaps,
        chosen at random, so that the Runs that create networks on one daemon at once seldom
        choose alike. Docker refuses one that overlaps another network, under its own lock,
        and the next is tried.

        A loaded daemon queues the networks created at once, so each may take as long as a
        ``docker run``; one whose CLI timed out or was cancelled is removed, since the daemon
        may still finish it.
        """
        candidates = await self._free_subnets()
        tried = random.sample(candidates, min(len(candidates), subnets.ATTEMPTS))
        for subnet in tried:
            host_ip = str(subnet[1])
            try:
                code, _, stderr = await _docker(
                    "network", "create", *(["--internal"] if internal else []),
                    "--subnet", str(subnet), "--gateway", host_ip, name,
                    timeout=_START_SEC,
                )  # fmt: skip
            except BaseException:
                await _discard_network(name)
                raise
            if code == 0:
                return host_ip
            if subnets.OVERLAPS not in stderr:
                raise ProviderStartError(
                    f"could not create the sandbox's network: {stderr.strip()}"
                )
        raise ProviderStartError(
            f"could not create the sandbox's network: each of {len(tried)} subnets of "
            f"{subnets.BLOCK} chosen at random overlapped another of Docker's networks"
        )

    async def _free_subnets(self) -> list[IPv4Network]:
        """The /29s of :data:`subnets.BLOCK` that no route in the daemon's network namespace
        overlaps, read once by a runc helper on ``--network host``.

        That namespace holds the bridges of a rootful daemon or a rootless one; when
        rootlesskit detaches its namespace it is the Host's, and the bridges' namespace then
        routes only rootlesskit's ``10.0.2.0/24``, outside the block. Every sandbox's network
        waits on this read, which may take as long as a ``docker run``.
        """
        if self._free is None:
            async with self._reading:
                if self._free is None:
                    code, out, stderr = await _docker(
                        "run", "--rm", *HELPER_RUNTIME, "--network", "host",
                        "--entrypoint", "ip", NETFILTER_IMAGE, *subnets.ROUTES,
                        timeout=_START_SEC,
                    )  # fmt: skip
                    if code != 0:
                        raise ProviderStartError(
                            f"could not read the routes of Docker's network namespace: "
                            f"{stderr.strip()}"
                        )
                    self._free = subnets.free(out)
        if not self._free:
            raise ProviderStartError(
                f"no sandbox network can be created: routes in Docker's network namespace "
                f"cover all of {subnets.BLOCK}"
            )
        return self._free


class _RuncConfinement:
    """A runc sandbox, sealed by an internal network of its own while it is isolated.

    An internal bridge has no route off the Host. Opening egress attaches the default
    bridge beside it, so the Gateway's address never moves, and closing it detaches that
    bridge again.
    """

    def __init__(self, network: str | None, host_ip: str) -> None:
        #: The sandbox's internal network; None for an ``open`` Task, on the default bridge.
        self.network = network
        #: The Host's address on that network, at which it reaches its Gateway.
        self.host_ip = host_ip

    @classmethod
    async def start(cls, sandbox_id: str, request: SandboxRequest, networks: _Networks) -> Self:
        """The per-episode network.

        ``block``, ``allowlist`` and ``blocklist`` all use ``--internal``: the bridge has no
        route off the host, so the only reachable thing is the gateway. They differ in what
        the gateway will then forward, not in what the sandbox can dial — which is why
        either list needs no privileged host rules and no tooling inside an image we do not
        control. ``open`` uses the default bridge.
        """
        if request.network.mode is NetworkMode.OPEN:
            return cls(None, await networks.host_ip())
        network = f"orbit-net-{sandbox_id}"
        return cls(network, await networks.create(network, internal=True))

    @classmethod
    async def found(
        cls, own: tuple[str, ...], networks: _Networks, labels: Mapping[str, str]
    ) -> Self:
        """A running sandbox's, from the Orbit networks it is attached to."""
        if not own:
            return cls(None, await networks.host_ip())
        return cls(own[0], await _bridge_host_ip(own[0]))

    def run_args(self, isolated: bool) -> list[str]:
        return ["--network", self.network] if self.network else []

    async def wire(
        self, container: str, request: SandboxRequest, *, reattached: bool = False
    ) -> None:
        """Route the fixed Gateway port to the Host, and confine an isolated sandbox to it.

        A short-lived helper with NET_ADMIN joins the sandbox's network namespace. The
        sandbox itself never holds NET_ADMIN, so neither its agent nor root inside it can
        lift the rules. Failure stops provisioning.

        The Gateway is dialled at :data:`SANDBOX_GATEWAY_PORT` and forwarded to the port the
        Host serves it on, in a chain of its own that a later run's Gateway replaces.

        An internal bridge has no route off the host, but the host's own address on it
        answers on every port a host service binds to all interfaces: sshd, databases,
        another run's Gateway. Every connection to that address is rejected except the
        episode's ports. A retained sandbox reattached by a later run keeps its rules and
        gains that run's ports, inserted ahead of the rejection.
        """
        port = _url_port(request.gateway_url)
        routed = bool(port) and _sandbox_gateway(request.gateway_url) != request.gateway_url
        if not routed and not self.network:
            return
        host_ip = self.host_ip
        rules: list[str] = []
        if routed:
            rules += [
                f"{{ iptables -w -t nat -N {GATEWAY_CHAIN} || "
                f"iptables -w -t nat -F {GATEWAY_CHAIN}; }}",
                f"{{ iptables -w -t nat -C OUTPUT -j {GATEWAY_CHAIN} || "
                f"iptables -w -t nat -A OUTPUT -j {GATEWAY_CHAIN}; }}",
                f"iptables -w -t nat -A {GATEWAY_CHAIN} -d {host_ip} -p tcp "
                f"--dport {SANDBOX_GATEWAY_PORT} -j DNAT --to-destination {host_ip}:{port}",
            ]
        if self.network:
            ports = sorted(
                {
                    port
                    for url in (request.gateway_url, request.proxy_url)
                    if (port := _url_port(url))
                }
            )
            position = "-I OUTPUT 1" if reattached else "-A OUTPUT"
            if ports:
                rules.append(
                    f"iptables -w {position} -d {host_ip} -p tcp -m multiport "
                    f"--dports {','.join(ports)} -j ACCEPT"
                )
            if not reattached:
                rules.append(f"iptables -w -A OUTPUT -d {host_ip} -j REJECT")
        if not rules:
            return
        await _netfilter_helper(container, rules, "route the sandbox's access to the host")

    async def sealed(self, container: str, attached: Mapping[str, object]) -> bool:
        """Whether a sandbox found running is sealed: whether it is off the default bridge."""
        return "bridge" not in attached

    async def seal_guest(self, client: GuestClient) -> None:
        """Nothing in the guest: its internal network has no route off the Host to remove."""

    async def open(self, container: str, client: GuestClient) -> None:
        code, _, stderr = await _docker("network", "connect", "bridge", container)
        if code != 0 and "already exists" not in stderr:
            raise ProviderStartError(f"could not open egress: {stderr.strip()}")

    async def close(self, container: str, client: GuestClient) -> None:
        code, _, stderr = await _docker("network", "disconnect", "bridge", container)
        if code != 0 and "is not connected" not in stderr:
            # Loud, because the alternative is an agent measured with a network it was
            # never meant to have and a lock file that says otherwise.
            raise ProviderStartError(f"could not close egress: {stderr.strip()}")

    async def remove(self) -> None:
        if self.network:
            await _remove_network(self.network)


#: Run as root on the framework's Python: replaces ``/etc/resolv.conf`` with ``argv[1]``.
_WRITE_RESOLV_CONF = "import sys; open('/etc/resolv.conf', 'w').write(sys.argv[1])"


class _OnOwnBridge:
    """A sandbox whose runtime takes its network at boot and keeps its packets from the
    namespace's netfilter, so it boots on a bridge network of its own, which no other
    container joins, and is sealed by tc filters outside it.

    The network is not internal: the runtime never sees a network attached later, so opening
    egress removes the seal and the sandbox leaves through this network's NAT.
    """

    def __init__(self, networks: _Networks, network: str, host_ip: str, host_mac: str) -> None:
        self.networks = networks
        #: The sandbox's network, ``orbit-net-<sandbox id>``.
        self.network = network
        #: The Host's address on it, the one address the seal admits, and its bridge's MAC.
        self.host_ip = host_ip
        self.host_mac = host_mac
        self._peers: tc.Peers | None = None

    @classmethod
    async def start(cls, sandbox_id: str, request: SandboxRequest, networks: _Networks) -> Self:
        """Its network, whatever its Task's network mode, with what the seal needs of it
        read before the sandbox starts: only the seal's own helper runs between the two."""
        network = f"orbit-net-{sandbox_id}"
        host_ip = await networks.create(network)
        try:
            return cls(networks, network, host_ip, await _bridge_mac(network, host_ip))
        except BaseException:
            await _discard_network(network)
            raise

    @classmethod
    async def found(
        cls, own: tuple[str, ...], networks: _Networks, labels: Mapping[str, str]
    ) -> Self:
        """A running sandbox's, from the Orbit network it is attached to and the bridge's MAC
        it was started with: a helper on its network now would be its neighbour."""
        if len(own) != 1:
            raise ProviderCapabilityError("the container is on no network of its own")
        if not _MAC.fullmatch(host_mac := labels.get(BRIDGE_MAC_LABEL, "")):
            raise ProviderCapabilityError("the container records no MAC of its network's bridge")
        network = own[0]
        return cls(networks, network, await _bridge_host_ip(network), host_mac)

    def run_args(self, isolated: bool) -> list[str]:
        return ["--network", self.network, "--label", f"{BRIDGE_MAC_LABEL}={self.host_mac}"]

    async def remove(self) -> None:
        await _remove_network(self.network)

    async def peers(self, container: str) -> tc.Peers:
        """The sandbox's addresses on its network, which Docker gave it, and the Host's."""
        if self._peers is None:
            on = f'(index .NetworkSettings.Networks "{self.network}")'
            code, out, stderr = await _docker(
                "inspect", "--format", f"{{{{{on}.IPAddress}}}} {{{{{on}.MacAddress}}}}",
                container, timeout=30,
            )  # fmt: skip
            fields = out.split()
            if code != 0 or len(fields) != 2:
                raise ProviderStartError(f"could not read {container}'s address: {stderr}")
            self._peers = tc.Peers(
                host_ip=self.host_ip, host_mac=self.host_mac, ip=fields[0], mac=fields[1]
            )
        return self._peers

    async def _resolve(self, container: str) -> None:
        """Give a sandbox that has just started, and been sealed if it is isolated, the
        default bridge's ``resolv.conf``; writing it needs no network.

        On any other network Docker names its embedded DNS server, ``127.0.0.11``, which it
        serves through netfilter in the sandbox's namespace, out of reach of a VM's guest
        and of netstack. Written in place, the file stays the container's own, as Docker's
        is.
        """
        resolv_conf = await self.networks.resolv_conf()
        code, _, stderr = await _docker(
            "exec", "-u", "0", container,
            guestd.PYTHON["linux"], "-I", "-c", _WRITE_RESOLV_CONF, resolv_conf,
            timeout=60,
        )  # fmt: skip
        if code != 0:
            raise ProviderStartError(f"could not write the sandbox's resolv.conf: {stderr}")


class _KataConfinement(_OnOwnBridge):
    """A sandbox in a Kata microVM, sealed by tc filters outside the VM while it is
    isolated.

    The filters in its namespace drop every frame but its own to the Host's address and the
    Host's to it. Its default route goes while it is sealed, so what they drop fails at
    once.
    """

    def run_args(self, isolated: bool) -> list[str]:
        # NET_ADMIN only to drop the guest's default route while it is sealed: what seals it
        # is outside the VM, where the capability does not reach.
        return super().run_args(isolated) + (["--cap-add", "NET_ADMIN"] if isolated else [])

    async def wire(
        self, container: str, request: SandboxRequest, *, reattached: bool = False
    ) -> None:
        """Route the fixed Gateway port to the Host and start an isolated sandbox sealed."""
        routed = _sandbox_gateway(request.gateway_url) != request.gateway_url
        commands = kata.filters(
            await self.peers(container),
            gateway_port=_url_port(request.gateway_url) if routed else "",
            proxy_port=_url_port(request.proxy_url),
            isolated=request.network.mode is not NetworkMode.OPEN,
            reattached=reattached,
        )
        await _netfilter_helper(container, commands, "confine the Kata sandbox's network")
        if not reattached:
            await self._resolve(container)

    async def sealed(self, container: str, attached: Mapping[str, object]) -> bool:
        """Whether a sandbox found running is sealed: whether the seal's drop filter is on
        the VM's tap."""
        return await _shows_drop(container, kata.SEAL_PROBE)

    async def seal_guest(self, client: GuestClient) -> None:
        await kata.set_route(client, self.host_ip, present=False)

    async def open(self, container: str, client: GuestClient) -> None:
        peers = await self.peers(container)
        await _netfilter_helper(container, kata.unseal(peers), "open egress")
        await kata.set_route(client, self.host_ip, present=True)

    async def close(self, container: str, client: GuestClient) -> None:
        peers = await self.peers(container)
        await _netfilter_helper(container, kata.seal(peers), "close egress")
        await kata.set_route(client, self.host_ip, present=False)


class _GvisorConfinement(_OnOwnBridge):
    """A sandbox under gVisor, sealed by tc filters on its veth's egress while it is
    isolated.

    Orbit's chains in its netstack route the Gateway and refuse what the filters drop, so
    that fails at once. The sandbox's own processes lack ``CAP_NET_ADMIN`` there unless it
    runs a container engine; the filters outside are what binds it.
    """

    def __init__(self, networks: _Networks, network: str, host_ip: str, host_mac: str) -> None:
        super().__init__(networks, network, host_ip, host_mac)
        self._isolated = False
        #: The Gateway's own port, when the fixed one is routed to it, and every port of
        #: the Host's address an isolated sandbox reaches, as the wiring run asked.
        self._gateway_port = ""
        self._ports: list[str] = []
        #: The seal a reattaching run found, until egress next changes.
        self._found_sealed: bool | None = None

    async def wire(
        self, container: str, request: SandboxRequest, *, reattached: bool = False
    ) -> None:
        """Route the fixed Gateway port to the Host and start an isolated sandbox sealed.

        A reattaching run keeps the seal the container has, and replaces the rest.
        """
        routed = _sandbox_gateway(request.gateway_url) != request.gateway_url
        self._isolated = request.network.mode is not NetworkMode.OPEN
        self._gateway_port = _url_port(request.gateway_url) if routed else ""
        self._ports = []
        sealed = False
        if self._isolated:
            proxy_port = _url_port(request.proxy_url)
            self._ports = [port for port in (self._gateway_port, proxy_port) if port]
            commands = gvisor.filters(
                await self.peers(container),
                gateway_port=self._gateway_port,
                proxy_port=proxy_port,
                reattached=reattached,
            )
            await _netfilter_helper(container, commands, "confine the gVisor sandbox's network")
            sealed = not reattached or await self.sealed(container, {})
        await self._netstack(container, sealed=sealed)
        if not reattached:
            await self._resolve(container)

    async def sealed(self, container: str, attached: Mapping[str, object]) -> bool:
        """Whether a sandbox found running is sealed: whether the seal's drop filter is on
        its veth. Read once by a reattaching run."""
        if self._found_sealed is None:
            self._found_sealed = await _shows_drop(container, gvisor.SEAL_PROBE)
        return self._found_sealed

    async def seal_guest(self, client: GuestClient) -> None:
        """Nothing more: wiring sealed its netstack too."""

    async def open(self, container: str, client: GuestClient) -> None:
        self._found_sealed = None
        # The inside first, so its rules never refuse what the outside already passes.
        await self._netstack(container, sealed=False)
        unseal = gvisor.unseal(await self.peers(container))
        await _netfilter_helper(container, unseal, "open egress")

    async def close(self, container: str, client: GuestClient) -> None:
        self._found_sealed = None
        # The outside first, so the sandbox is never open while its rules say it is sealed.
        seal = gvisor.seal(await self.peers(container))
        await _netfilter_helper(container, seal, "close egress")
        await self._netstack(container, sealed=True)

    async def _netstack(self, container: str, *, sealed: bool) -> None:
        """Set Orbit's chains in the sandbox's netstack; nothing when there is none to set."""
        if not self._isolated and not self._gateway_port:
            return
        state = gvisor.netstack(
            self.host_ip,
            gateway_port=self._gateway_port,
            ports=self._ports,
            isolated=self._isolated,
            sealed=sealed,
        )
        code, _, stderr = await _docker(*gvisor.set_netstack(container, state), timeout=60)
        if code != 0:
            raise ProviderStartError(
                f"could not set Orbit's chains in the gVisor sandbox: {stderr.strip()}"
            )


_Confinement = _RuncConfinement | _KataConfinement | _GvisorConfinement


async def _shows_drop(container: str, probe: str) -> bool:
    """Whether the seal's drop filter that ``probe`` shows is in a sandbox's namespace."""
    code, shown, stderr = await _in_namespace(container, probe)
    if code != 0:
        raise ProviderStartError(f"could not read the sandbox's seal: {stderr.strip()}")
    return "action drop" in shown


@dataclass(frozen=True)
class Grant:
    """What a Sandbox that asks for a container feature is started with beyond its leaf's
    own flags: Linux capabilities, the registered runtime it needs instead of the leaf's,
    and directories of it that each get a Docker volume of their own, which goes with the
    Sandbox."""

    capabilities: tuple[str, ...] = ()
    runtime: str | None = None
    volumes: tuple[str, ...] = ()


def _as_declared(memory_mb: int) -> int:
    return memory_mb


@dataclass(frozen=True)
class Runtime:
    """An OCI runtime a Docker leaf starts every one of its Sandboxes with."""

    registered: str
    """The name Docker's daemon knows it by, which ``--runtime`` takes and
    ``docker inspect`` reports as the container's. A containerd shim's name,
    ``io.containerd.<shim>.<version>``, Docker also runs from its PATH as
    ``containerd-shim-<shim>-<version>``."""
    grants: Mapping[ContainerFeature, Grant]
    """The container features it gives a Sandbox that asks for them, as measured (see the
    Providers README), each with what that Sandbox gets for it."""
    gpus: bool
    """Whether Docker's GPU injection reaches its Sandboxes."""
    confinement: type[_Confinement]
    """How its Sandboxes are kept to the Host's address while they are isolated."""
    devices: tuple[str, ...] = ()
    """The Host devices it needs."""
    memory_mb: Callable[[int], int] = _as_declared
    """The container memory that leaves a Sandbox's processes what its Task declared."""
    flags: tuple[str, ...] = ()
    """``docker run`` flags every one of its Sandboxes gets."""
    arguments: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """For each name it is registered under by path, the ``runtimeArgs`` it is registered
    with, no more and no fewer."""
    build: str = ""
    """What the version ``<runtime> --version`` reports ends in for the build of it this
    engine runs, for one Orbit builds itself."""
    drivers: tuple[str, ...] = ()
    """The command of it that lists the NVIDIA driver versions it supports, for one that
    proxies the Host's driver rather than passing it through."""
    banner: str = ""
    """What the kernel log of each of its Sandboxes starts with, which shows a Sandbox runs
    on it, together with Docker seeing what the Sandbox writes."""
    guest_kernel: bool = False
    """Whether each of its Sandboxes runs a kernel of its own in a VM, whose ``docker exec``
    sessions outlive that VM killed from outside."""
    oom_kills: str = ""
    """For a runtime whose own kernel ends a Sandbox's processes for its memory without Docker
    recording it, as Kata's guest and gVisor's Sentry do, the file in the Sandbox in which
    that kernel counts them, ``oom_kill``, which a watcher there reads
    ([memory](../memory.py))."""
    oom_counter: str = ""
    """What keeps those counts, as the message of a Sandbox lost after one names it."""
    gpu_memory_flag: str = ""
    """For a runtime that can hold a Sandbox to so many bytes of device memory on each of its
    GPUs, its flag that does, which ``<runtime> flags`` lists and a Sandbox's annotation
    ``dev.gvisor.flag.<flag>`` sets for it alone; a shared Sandbox of one without is only
    watched."""

    @property
    def feature_sets(self) -> tuple[frozenset[ContainerFeature], ...]:
        """The container features one Sandbox of it can have together, one set per profile:
        a Sandbox runs under one registered runtime, so features that each need another
        come in profiles of their own."""
        common = frozenset(feature for feature, grant in self.grants.items() if not grant.runtime)
        runtimes = dict.fromkeys(grant.runtime for grant in self.grants.values() if grant.runtime)
        return tuple(
            common | {feature for feature, grant in self.grants.items() if grant.runtime == name}
            for name in runtimes
        ) or (common,)

    @property
    def gpu_hold(self) -> GpuHold:
        """How a Sandbox of it that names ``gpu_memory_mb`` holds its GPUs."""
        if not self.gpus:
            return GpuHold.WHOLE
        return GpuHold.LIMIT if self.gpu_memory_flag else GpuHold.WATCHED

    def granted(self, features: Sequence[ContainerFeature]) -> tuple[str, Grant]:
        """The registered runtime a Sandbox that asks for ``features`` runs under, the one a
        feature of them needs or else the leaf's own, and the capabilities and volumes it
        is started with: the union of their grants."""
        grants = [self.grants[feature] for feature in features]
        runtimes = {grant.runtime for grant in grants if grant.runtime}
        assert len(runtimes) <= 1, f"{features} need runtimes {runtimes}"
        union = Grant(
            capabilities=tuple(sorted({name for grant in grants for name in grant.capabilities})),
            volumes=tuple(sorted({path for grant in grants for path in grant.volumes})),
        )
        return (runtimes.pop() if runtimes else self.registered), union

    def names(self, needs: Sequence[SandboxNeeds]) -> list[str]:
        """The registered names Sandboxes of ``needs`` run under; with none, every one."""
        wanted = (
            {self.granted(need.resources.container_features)[0] for need in needs}
            if needs
            else {
                self.registered,
                *(grant.runtime for grant in self.grants.values() if grant.runtime),
            }
        )
        return sorted(wanted)


#: Each Docker leaf's runtime, by the part of its provider id after ``docker:``. runc gives
#: raw sockets to every container, as Docker does by default; Kata's microVM runs no seccomp
#: filter, and ``mount`` adds ``CAP_SYS_ADMIN``. Docker's GPU injection does not reach a
#: Kata microVM. gVisor runs a Sandbox that needs raw sockets, or that runs a container
#: engine, under the runtime registered for it, with the engine's capabilities and its
#: storage on volumes.
RUNTIMES: dict[str, Runtime] = {
    "runc": Runtime(
        "runc", {ContainerFeature.RAW_SOCKETS: Grant()}, gpus=True, confinement=_RuncConfinement
    ),
    "kata": Runtime(
        kata.RUNTIME,
        {
            ContainerFeature.PERF: Grant(),
            ContainerFeature.BPF: Grant(),
            ContainerFeature.IO_URING: Grant(),
            ContainerFeature.RAW_SOCKETS: Grant(),
            ContainerFeature.USER_NAMESPACES: Grant(),
            ContainerFeature.MOUNT: Grant(capabilities=("SYS_ADMIN",)),
        },
        gpus=False,
        confinement=_KataConfinement,
        devices=("/dev/kvm",),
        memory_mb=kata.memory_mb,
        guest_kernel=True,
        oom_kills=kata.OOM_KILLS,
        oom_counter="its cgroup",
    ),
    "gvisor": Runtime(
        gvisor.RUNTIME,
        {
            ContainerFeature.USER_NAMESPACES: Grant(),
            ContainerFeature.FUSE: Grant(),
            ContainerFeature.MOUNT: Grant(capabilities=("SYS_ADMIN",)),
            ContainerFeature.RAW_SOCKETS: Grant(runtime=gvisor.NET_RAW),
            ContainerFeature.NESTED_CONTAINERS: Grant(
                capabilities=gvisor.ENGINE_CAPABILITIES,
                runtime=gvisor.NESTED,
                volumes=gvisor.ENGINE_STORAGE,
            ),
        },
        gpus=True,
        confinement=_GvisorConfinement,
        memory_mb=gvisor.memory_mb,
        flags=gvisor.FLAGS,
        arguments=gvisor.ARGUMENTS,
        build=gvisor.BUILD,
        drivers=gvisor.DRIVERS,
        banner=gvisor.BANNER,
        gpu_memory_flag=gvisor.GPU_MEMORY_FLAG,
        oom_kills=gvisor.OOM_KILLS,
        oom_counter="the Sentry",
    ),
}


def _runtime_named(registered: str) -> Runtime:
    """The runtime a container ``docker inspect`` reports was started under belongs to."""
    for runtime in RUNTIMES.values():
        if registered in runtime.names(()):
            return runtime
    raise ProviderCapabilityError(f"no Docker leaf starts containers under runtime {registered!r}")


#: How long an ended guestd session waits for its container to stop. A container whose
#: sandbox died has stopped, or stops within seconds: under rootless Docker ``docker wait``
#: returned up to 10.5 s after the session ended. One still running lost only the session.
_DEATH_SEC = 30.0
#: How long a lost session then waits for the Sandbox's memory watcher to end too, as its
#: session does once its container is gone, so that what it saw last has arrived.
_SETTLE_SEC = 5.0


class _Session(StdioTransport):
    """guestd over ``docker exec -i``, with the client that speaks to it, whose stream ending
    unasked, once guestd has answered, says whether the sandbox died and whether an OOM kill
    came first."""

    def __init__(
        self,
        argv: Sequence[str],
        container: str,
        memory_mb: int,
        watcher: memory.GuestEvents | None = None,
    ) -> None:
        super().__init__(argv)
        self.container = container
        #: The memory its Task declares.
        self.memory_mb = memory_mb
        #: Under a runtime whose own kernel kills in the Sandbox, Kata's guest or gVisor's
        #: Sentry, what counts those OOM kills, which Docker does not record.
        self.watcher = watcher
        self.client = GuestClient(self)
        self._answered = False
        self._closing = False
        #: What the end of its stream was found to mean, which a later call then raises too.
        self._explained: GuestUnreachableError | None = None
        #: What Orbit itself stopped the Sandbox for, which the end of the stream then
        #: raises.
        self.stopped: GuestUnreachableError | None = None

    async def send(self, data: bytes) -> None:
        # Nothing more is asked of a Sandbox Orbit stops, while the stop takes effect.
        if self.stopped is not None:
            raise self.stopped
        await super().send(data)

    async def recv(self) -> str:
        line = await super().recv()
        self._answered = True
        return line

    async def close(self) -> None:
        # Orbit ends the session before it destroys, keeps or snapshots the Sandbox, so an
        # end it causes is never taken for a death.
        self._closing = True
        await super().close()

    def abandon(self) -> None:
        """End the stream as its sandbox's death does, for a container that stopped while
        its ``docker exec`` runs on."""
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()

    async def explain(self, error: GuestUnreachableError) -> GuestUnreachableError:
        """A guestd that never answered failed to start, which its stderr, in ``error``,
        says at once; a session that did and ended unasked lost its sandbox, or guestd in
        it, which is a loss to memory after an OOM kill in it; or Orbit stopped it."""
        if self.stopped is not None:
            return self.stopped
        if self._closing or not self._answered:
            return error
        if self._explained is None:
            self._explained = await self._explain(error)
        return self._explained

    async def _explain(self, error: GuestUnreachableError) -> GuestUnreachableError:
        if (state := await _state(self.container)) is None:
            return error
        running, code = bool(state.get("Running")), state.get("ExitCode")
        lost = "guestd in the sandbox ended" if running else "the sandbox itself died"
        exited = "" if running else f", and its main process exited with code {code}"
        # Docker records the Host's OOM kills, and a watcher those of the runtime's own kernel.
        evidence = ["Docker recorded OOMKilled"] if state.get("OOMKilled") else []
        if self.watcher is not None:
            await self.watcher.settled(_SETTLE_SEC)
            if (counted := self.watcher.oom_killed()) is not None:
                evidence.append(counted)
        if evidence:
            explained: GuestUnreachableError = MemoryExhaustedError(
                f"{lost} after an OOM kill in it (its Task declares {self.memory_mb} MiB): "
                f"{'; '.join(evidence)}{exited} ({error})"
            )
        elif running:
            return error
        elif code == 137:
            explained = GuestUnreachableError(f"{lost}: it was killed from outside ({error})")
        else:
            explained = GuestUnreachableError(
                f"{lost}: its main process exited with code {code} ({error})"
            )
        explained.__cause__ = error
        return explained


async def _state(container: str) -> dict[str, Any] | None:
    """Docker's state of ``container`` once it has stopped, or once it has run on for
    :data:`_DEATH_SEC`; None once it is gone.

    ``docker wait`` returns once it has stopped, while its state, read when the session
    ended, could still say it runs.
    """
    try:
        code, _, _ = await _docker("wait", container, timeout=_DEATH_SEC)
    except ProviderStartError:
        code = 0  # still running: only guestd was lost
    if code != 0:
        return None
    code, raw, _ = await _docker("inspect", "--format", "{{json .State}}", container, timeout=30)
    return json.loads(raw) if code == 0 else None


async def _watcher(container: str, runtime: Runtime) -> memory.GuestEvents | None:
    """What counts the OOM kills in ``container`` that Docker does not record: under a runtime
    whose own kernel kills in it, a watcher there."""
    if not runtime.oom_kills:
        return None
    return await memory.GuestEvents.start(
        [
            "docker", "exec", "-i", "--user", "0", "--workdir", "/", container,
            guestd.PYTHON["linux"], "-I", "-c", memory.WATCHER, runtime.oom_kills,
        ],
        counter=runtime.oom_counter,
    )  # fmt: skip


def _verify_docker_gpu(
    selected: tuple[GpuDevice, ...], observed: tuple[GpuDevice, ...]
) -> tuple[GpuDevice, ...]:
    """The selected GPUs as the Host reported them when it admitted the Sandbox, once the
    sandbox shows exactly those: gVisor's nvproxy reports no PCI address to nvidia-smi in the
    sandbox."""
    actual = tuple(sorted(device.id for device in observed))
    expected = tuple(sorted(device.id for device in selected))
    if actual != expected:
        raise ProviderCapabilityError(
            f"Docker GPU identity mismatch: selected {expected}, observed {actual}"
        )
    return selected


class DockerSandbox(Sandbox):
    """One container, driven through the guest service."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        request: SandboxRequest,
        container: str,
        container_id: str,
        confinement: _Confinement,
        session: _Session,
        file_session: _Session,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        agent_user: str,
        agent_home: str,
        sealed: bool = False,
        watchdog: gpu_sharing.Watchdog,
        host_ports: rootless.HostPorts,
        guest_kernel: bool = False,
        written: int = 0,
        mounts: tuple[volumes.Mount, ...] = (),
        meter: volumes.Meter | None = None,
    ) -> None:
        super().__init__(
            sandbox_id=sandbox_id,
            request=request,
            resolved_image=resolved_image,
            allocation=allocation,
            agent_user=agent_user,
            agent_home=agent_home,
        )
        self.container = container
        #: The container's full ID, by which the Host's cgroups name it.
        self.container_id = container_id
        #: How the sandbox is kept to the Host's address while it is sealed: an internal
        #: network under runc, tc filters outside it under Kata and gVisor.
        self.confinement = confinement
        self._client = session.client
        self._file_client = file_session.client
        #: The guestd sessions of both clients, whose end says why Orbit stopped the
        #: Sandbox.
        self._sessions = (session, file_session)
        #: What counts the OOM kills in it where Docker records none, which both sessions
        #: consult.
        self._watcher = session.watcher
        #: The watchdog of its Provider, which watches it while it shares GPUs and this
        #: process drives it.
        self._watchdog = watchdog
        #: What measures what it writes while it names ``storage_mb`` and this process drives
        #: it.
        self._storage: storage.Watch | None = None
        #: What the snapshot it booted from holds over its Task's image, in bytes, which its
        #: ``storage_mb`` counts as a disk of that size would; 0 for one of the image itself.
        self.written = written
        #: Set while it is committed as a snapshot: a commit cut short may have finished in
        #: the daemon, and destroying the Sandbox then removes the image it left.
        self._committing = False
        #: Its volumes, the layers of them its snapshot keeps once it is saved, and what
        #: measures what it writes to them.
        self.mounts = mounts
        self._kept: frozenset[str] = frozenset()
        self._meter = meter
        self.volumes = tuple(mount.path for mount in mounts if not mount.copied)
        #: The Host's ports of its Provider, of which it holds its own until it ends here.
        self._host_ports = host_ports
        #: The context of the phase that last ran a command in it, in which what the watchdog
        #: and the storage watch do to it is logged, so that the episode's execution trace
        #: records it.
        self._context = contextvars.copy_context()
        self._sealed = sealed
        #: The loopback address guestd relays the agent to the egress proxy from, while sealed.
        self._relay: str | None = None
        self.state = SandboxState.READY
        #: Under Kata a ``docker exec`` outlives a microVM killed from outside, as by the
        #: Host's OOM killer, so the sessions end once the container has stopped.
        self._stopping = asyncio.ensure_future(self._end_sessions()) if guest_kernel else None

    async def _end_sessions(self) -> None:
        while True:
            try:
                await _docker("wait", self.container, timeout=3600)
                break
            except ProviderStartError:
                continue  # still running an hour later
        for session in self._sessions:
            session.abandon()

    async def _release(self) -> None:
        """Stop watching it and let go of the Host's ports, then end its sessions: Orbit's own
        end is no death."""
        self._unwatch()
        self._host_ports.release(self.container)
        if self._stopping is not None:
            self._stopping.cancel()
            await asyncio.gather(self._stopping, return_exceptions=True)
        await asyncio.gather(
            self._client.close(), self._file_client.close(), return_exceptions=True
        )
        if self._watcher is not None:
            await self._watcher.close()

    def _as(self, identity: Identity) -> str | None:
        """The unix user for a role, or ``None`` to keep the container's default.

        Framework work stays as the image's own user, which for a sandbox image is root:
        installing the guest service and collecting artifacts have to work whatever the
        agent did to its own files.
        """
        return self.agent_user if identity is Identity.AGENT else None

    @property
    def handle(self) -> str:
        return f"{HANDLE_PREFIX}{self.container}"

    @property
    def gateway_url(self) -> str | None:
        """The gateway as this container can reach it.

        The host binds to every interface; a container cannot use that address, so the
        URL is rewritten here rather than in the gateway — which is exactly the point of
        the gateway knowing nothing about providers.
        """
        return _sandbox_gateway(self.request.gateway_url)

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        identity: Identity = Identity.FRAMEWORK,
        output_sink: ExecOutputSink | None = None,
    ) -> ExecResult:
        if self._sealed and self._relay:
            env = relay.variables(self._relay, HOST_ALIAS) | (env or {})
        self._context = contextvars.copy_context()
        loop = asyncio.get_running_loop()
        started = loop.time()
        exit_code, stdout, stderr, timed_out = await self._client.exec(
            argv,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
            run_as=self._as(identity),
            output_sink=output_sink,
        )
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((loop.time() - started) * 1000),
            timed_out=timed_out,
        )

    async def write_file(
        self, path: PurePosixPath | str, data: bytes, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        await self._client.write_file(str(path), data, run_as=self._as(identity))

    async def read_file(
        self, path: PurePosixPath | str, *, offset: int = 0, length: int | None = None
    ) -> bytes:
        return await self._file_client.read_file(str(path), offset=offset, length=length)

    async def list_files(self, path: str) -> tuple[SandboxFile, ...]:
        return await self._file_client.list_files(path)

    async def file_digest(self, path: str, *, length: int) -> str:
        return await self._file_client.file_digest(path, length=length)

    async def upload_dir(
        self, source: str, target: PurePosixPath | str, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        await self._client.upload_dir(source, str(target), run_as=self._as(identity))

    async def screenshot(self) -> bytes:
        return await self._client.screenshot()

    async def inject_input(self, actions: Sequence[object]) -> int:
        # Accept either raw dicts or DesktopAction models, so callers need no adapter.
        payload = [
            action if isinstance(action, dict) else action.model_dump(mode="json")  # type: ignore[union-attr]
            for action in actions
        ]
        return await self._client.inject_input(payload)

    async def open_egress(self) -> None:
        """Let the sandbox off the Host for the framework's own phases.

        The Gateway stays where it was throughout: an agent handed one URL at the start of
        an episode must not find it stale halfway through.
        """
        if self.request.network.mode is NetworkMode.OPEN:
            return  # the task declared `open`; it already goes out
        # Open, the account's programs go straight out again.
        await relay.stop(self._client, self.request)
        self._relay = None
        await self.confinement.open(self.container, self._client)
        self._sealed = False

    async def close_egress(self) -> None:
        """Seal it again, leaving only the Gateway and the proxy on the Host's address."""
        if self.request.network.mode is NetworkMode.OPEN:
            return
        await self.confinement.close(self.container, self._client)
        await self._start_relay()
        self._sealed = True

    async def _start_relay(self) -> None:
        self._relay = await relay.start(
            self._client,
            self.request,
            self.allocation,
            proxy=_reachable(self.request.proxy_url) or "",
            host=HOST_ALIAS,
        )

    def watch(self) -> None:
        """Have the watchdog watch what it uses of its GPUs, when it shares them, and measure
        what it writes, when it names ``storage_mb``."""
        gpu = self.allocation.gpu
        if gpu is not None and gpu.memory_mb is not None:
            self._watchdog.watch(
                gpu_sharing.Watched(self.container_id, gpu.memory_mb, self.overran)
            )
        if self.allocation.storage_mb is not None:
            self._storage = storage.Watch(
                self.container_id,
                self.allocation.storage_mb,
                self.wrote_past,
                base=self.written,
                volumes=self._meter.written if self._meter is not None else None,
            )

    def _unwatch(self) -> None:
        self._watchdog.unwatch(self.container_id)
        if self._storage is not None:
            self._storage.stop()

    def _stop_for(self, reason: GuestUnreachableError) -> None:
        """Have every call from now on, and each that waits on it now, raise ``reason``:
        Orbit is about to stop the Sandbox."""
        for session in self._sessions:
            session.stopped = reason

    async def wrote_past(self, written: int) -> None:
        """Act on the storage watch's finding that it wrote more over its image than its
        Task's ``storage_mb``: log so in the phase of the command that ran last, and kill it,
        so that its next call, and one that waits on it now, raises
        :class:`StorageExhaustedError`.

        Logged before the kill, which ends the episode's command at once: the episode may
        destroy the Sandbox before the kill returns.
        """
        storage_mb = self.allocation.storage_mb
        written_mb = -(-written >> 20)  # rounded up, so that it shows past the limit
        writer = "it and the snapshot it booted from" if self.written else "it"
        found = (
            f"{writer} wrote {written_mb} MiB over its image, past the {storage_mb} MiB its "
            "Task declares"
        )
        message = f"Orbit's storage watch stopped the Sandbox: {found}"
        _log.info("%s: %s", self.container, message)
        data = {"written_mb": written_mb, "storage_mb": storage_mb}
        self._context.run(
            logging.getLogger("orbit.execution").warning, message, extra={"orbit_data": data}
        )
        self._watchdog.unwatch(self.container_id)
        self._stop_for(StorageExhaustedError(f"Orbit stopped the sandbox: {found}"))
        await _docker("kill", self.container, timeout=60)

    async def overran(self, overrun: gpu_sharing.Overrun) -> None:
        """Act on the watchdog's finding that it uses more device memory on a GPU than it
        reserved.

        Where nothing limits the memory, its largest process on that GPU is killed, and only
        that one, as the OOM killer kills a process past a memory limit: the episode goes on
        and the agent sees its process killed. The kill joins the Sandbox's PID namespace
        from a helper container, once the PID's container and start time show it is still the
        process that used the memory, and the Sandbox stays watched. Where its runtime limits
        the memory, the limit's accounting failed, which is Orbit's failure: the whole
        Sandbox stops, and its episode ends in an environment error that says what was
        found.
        """
        gpu = self.allocation.gpu
        assert gpu is not None and gpu.memory_mb is not None
        found = (
            f"the Sandbox used {overrun.used_mb} MiB of GPU {overrun.gpu}, more than its "
            f"gpu_memory_mb of {gpu.memory_mb} MiB"
        )
        if gpu.hold is GpuHold.LIMIT:
            self._unwatch()
            self._stop_for(
                GuestUnreachableError(
                    f"the sandbox itself died: Orbit's GPU memory watchdog stopped it: {found}, "
                    f"past {self.allocation.leaf}'s limit"
                )
            )
            await _docker("kill", self.container, timeout=60)
            return
        # Logged in the phase of the command that ran when the watchdog found it: the helper
        # returns once its container is removed, by when the episode may have gone on.
        context = self._context
        largest = max(overrun.processes, key=lambda process: process.used_mb)
        now = gpu_sharing.host_process(largest.pid, largest.gpu, largest.used_mb)
        if now is None or (now.container, now.started) != (largest.container, largest.started):
            return  # it ended meanwhile; the next look sees what uses the GPU then
        code, _, stderr = await _docker(
            "run", "--rm", *HELPER_RUNTIME, "--pid", f"container:{self.container_id}",
            "--network", "none", "--entrypoint", "sh", NETFILTER_IMAGE,
            "-c", gpu_sharing.KILL, "sh", str(largest.inner_pid), largest.started,
            timeout=_START_SEC,
        )  # fmt: skip
        if code != 0:
            if self.state is not SandboxState.DESTROYED:
                _log.warning(
                    "could not kill process %s of %s: %s", largest.pid, self.container, stderr
                )
            return
        message = (
            f"Orbit's GPU memory watchdog killed process {largest.inner_pid} ({largest.name}), "
            f"which held {largest.used_mb} MiB: {found}, which {self.allocation.leaf} does not "
            "enforce"
        )
        data = {
            "process": largest.inner_pid,
            "name": largest.name,
            "process_mb": largest.used_mb,
            "gpu": overrun.gpu,
            "sandbox_mb": overrun.used_mb,
            "gpu_memory_mb": gpu.memory_mb,
        }
        _log.info("%s: %s", self.container, message)
        context.run(
            logging.getLogger("orbit.execution").warning, message, extra={"orbit_data": data}
        )

    async def _volume_roots(self) -> dict[str, volumes.Root]:
        """The owner and mode of each volume artifact's root, which a view of it restores."""
        roots = {}
        for mount in self.mounts:
            result = await self.exec(
                [guestd.PYTHON["linux"], "-I", "-c", _STAT_ROOT, mount.path], timeout_sec=60
            )
            if not result.ok:
                raise ProviderStartError(f"could not read {mount.path}: {result.stderr.strip()}")
            uid, gid, mode = (int(field) for field in result.stdout.split())
            roots[mount.path] = volumes.Root(uid, gid, mode)
        return roots

    async def destroy(self) -> None:
        """Idempotent: teardown also runs on failure paths, sometimes twice."""
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED
        await self._release()
        try:
            code, _, stderr = await _docker("rm", "-f", "-v", self.container, timeout=_REMOVE_SEC)
            if code != 0:
                # Its container keeps its disk until removed: say so where the operator looks.
                _log.warning("could not remove %s: %s", self.container, stderr.strip())
            if self._committing:
                # A snapshot that was never handed back is no one's: it goes with its Sandbox.
                # Its container went first, which fails a commit of it still in the daemon, so
                # only an image the daemon finished can be left (see AGENTS.md).
                reference = f"{SNAPSHOT_REPOSITORY}:{self.sandbox_id}"
                code, _, stderr = await _docker("rmi", reference, timeout=120)
                if code != 0 and "no such image" not in stderr.lower():
                    _log.warning("could not remove %s: %s", reference, stderr.strip())
                # The layers its snapshot would have kept are no one's either.
                self._kept = frozenset()
            try:
                if self._meter is not None:
                    await self._meter.stop()
                await volumes.remove(self.mounts, kept=self._kept)
            except ProviderStartError as exc:
                _log.warning("%s: %s", self.container, exc)
        finally:
            # Even when the container's removal failed or timed out: the network is the
            # sandbox's alone, and one Docker still finds attached is logged by name.
            await self.confinement.remove()

    async def retain(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        await self._release()
        gpu_devices = (
            self.allocation.gpu.provider_addresses if self.allocation.gpu is not None else ()
        )
        return RetainedSandbox(
            provider="docker",
            handle=self.handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"orbit sandbox destroy {self.handle}",
            gpu_devices=gpu_devices,
        )

    async def snapshot(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        """Stop the container and commit its filesystem as a labelled local image.

        The commit keeps no volume: the image's label records each volume's stack of layers,
        which the snapshot keeps (volumes.py). The image keeps the container's labels and
        command; a snapshot holds no GPU and reserves no memory. A commit that would stack
        more layers than :data:`MAX_LAYER_DEPTH` allows is flattened to one, so that an
        episode saves any number of snapshots in a row.
        """
        roots = await self._volume_roots()
        # The relay ends with this container; the image keeps the account's own settings.
        await relay.stop(self._client, self.request)
        self._relay = None
        await self._release()
        code, _, stderr = await _docker("stop", "--time", "30", self.container, timeout=120)
        if code != 0:
            raise ProviderStartError(f"could not stop {self.container}: {stderr.strip()}")
        # What it holds over its Task's image: what it wrote, over what it booted from.
        layer = await storage.written(self.container)
        if layer is None:
            raise ProviderStartError(f"{self.container} is gone; its disk cannot be saved")
        if self._meter is not None:
            layer += await self._meter.written()
        changes = [
            f"LABEL {RETENTION_LABEL}=snapshot",
            f'LABEL {GPU_LABEL}=""',
            f'LABEL {GPU_MEMORY_LABEL}=""',
            f"LABEL {PREPARED_LABEL}={self.request.prepared_image.prepared_identity}",
            f"LABEL {WRITTEN_LABEL}={self.written + layer}",
        ]
        if self.mounts:
            label, self._kept = await volumes.freeze(
                self.sandbox_id, self.mounts, roots, NETFILTER_IMAGE
            )
            changes.append(f"LABEL {volumes.LAYERS_LABEL}={label}")
        tag = f"{SNAPSHOT_REPOSITORY}:{self.sandbox_id}"
        self._committing = True
        # With the writable layer of a Sandbox booted from it, a commit stacks two more.
        if await _layers(self.container) + 2 > MAX_LAYER_DEPTH:
            await _flatten(self.container, tag, changes)
        else:
            flags = [part for change in changes for part in ("--change", change)]
            code, _, stderr = await _docker("commit", *flags, self.container, tag, timeout=1800)
            if code != 0:
                raise ProviderStartError(f"could not commit {self.container}: {stderr.strip()}")
        self._committing = False
        handle = f"{SNAPSHOT_PREFIX}{self.sandbox_id}"
        return RetainedSandbox(
            provider="docker",
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"orbit sandbox destroy {handle}",
        )


#: The most layers Docker's layer store chains under any graph driver (moby's
#: ``maxLayerDepth``): each commit adds one, and a snapshot that would pass it is flattened.
MAX_LAYER_DEPTH = 125
#: Upper bound on flattening a snapshot, which writes its whole filesystem again.
FLATTEN_TIMEOUT_SEC = 3600


async def _layers(container: str) -> int:
    """How many layers the image ``container`` runs stacks."""
    code, image, stderr = await _docker("inspect", "--format", "{{.Image}}", container, timeout=60)
    if code == 0:
        code, raw, stderr = await _docker(
            "image", "inspect", "--format", "{{len .RootFS.Layers}}", image.strip(), timeout=60
        )
        if code == 0 and raw.strip().isdigit():
            return int(raw.strip())
    raise ProviderStartError(f"could not count the layers under {container}: {stderr.strip()}")


async def _flatten(container: str, tag: str, changes: Sequence[str]) -> None:
    """Commit the stopped ``container`` as ``tag`` in a single layer: its filesystem, as a
    commit would keep it, exported and imported whole, with the container's configuration
    and ``changes`` after it, so that a snapshot chain starts again from one layer."""
    code, raw, stderr = await _docker("inspect", "--format", "{{json .Config}}", container)
    if code != 0:
        raise ProviderStartError(f"could not read {container}'s configuration: {stderr.strip()}")
    flags = [
        part
        for change in (*_config_changes(json.loads(raw)), *changes)
        for part in ("--change", change)
    ]
    read, write = os.pipe()
    processes: list[asyncio.subprocess.Process] = []
    try:
        try:
            processes.append(
                await asyncio.create_subprocess_exec(
                    "docker",
                    "export",
                    container,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=write,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
            processes.append(
                await asyncio.create_subprocess_exec(
                    "docker",
                    "import",
                    *flags,
                    "-",
                    tag,
                    stdin=read,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
        finally:
            os.close(read)
            os.close(write)
        async with asyncio.timeout(FLATTEN_TIMEOUT_SEC):
            errors = [error for _, error in await asyncio.gather(
                *(process.communicate() for process in processes)
            )]  # fmt: skip
    except BaseException:
        for process in processes:
            if process.returncode is None:
                process.kill()
        await asyncio.gather(*(process.wait() for process in processes), return_exceptions=True)
        raise
    failed = [
        error.decode("utf-8", "replace").strip()
        for process, error in zip(processes, errors, strict=True)
        if process.returncode
    ]
    if failed:
        raise ProviderStartError(f"could not flatten {container}: {'; '.join(failed)}")


def _config_changes(config: Mapping[str, Any]) -> list[str]:
    """``docker import`` changes that give an image ``config``, a container's, as a commit
    of it keeps it: its environment, command, account, directory, ports, volumes, labels,
    stop signal and health check."""
    changes = [
        f"ENV {key}={_dockerfile_string(value)}"
        for key, _, value in (item.partition("=") for item in config.get("Env") or ())
    ]
    for key, instruction in (("Entrypoint", "ENTRYPOINT"), ("Cmd", "CMD")):
        if config.get(key) is not None:
            changes.append(f"{instruction} {json.dumps(config[key])}")
    if config.get("User"):
        changes.append(f"USER {config['User']}")
    if config.get("WorkingDir"):
        changes.append(f"WORKDIR {config['WorkingDir'].replace('$', chr(92) + '$')}")
    changes += [f"EXPOSE {port}" for port in sorted(config.get("ExposedPorts") or {})]
    if config.get("Volumes"):
        changes.append(f"VOLUME {json.dumps(sorted(config['Volumes']))}")
    changes += [
        f"LABEL {_dockerfile_string(key)}={_dockerfile_string(value)}"
        for key, value in sorted((config.get("Labels") or {}).items())
    ]
    if config.get("StopSignal"):
        changes.append(f"STOPSIGNAL {config['StopSignal']}")
    if health := config.get("Healthcheck"):
        changes.append(_healthcheck(health))
    return changes


def _dockerfile_string(value: str) -> str:
    """``value`` as a Dockerfile string, which no variable in it expands."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def _healthcheck(health: Mapping[str, Any]) -> str:
    test = list(health.get("Test") or ())
    if not test or test[0] == "NONE":
        return "HEALTHCHECK NONE"
    options = [
        f"--{option}={health[key] // 1_000_000}ms"
        for option, key in (
            ("interval", "Interval"),
            ("timeout", "Timeout"),
            ("start-period", "StartPeriod"),
            ("start-interval", "StartInterval"),
        )
        if health.get(key)
    ]
    if health.get("Retries"):
        options.append(f"--retries={health['Retries']}")
    command = json.dumps(test[1:]) if test[0] == "CMD" else test[1]
    return " ".join(("HEALTHCHECK", *options, "CMD", command))


class DockerProvider(Provider):
    """Supplies container sandboxes."""

    name = "docker"

    def __init__(
        self,
        *,
        gpus: tuple[int, ...] = (),
        gpu_memory_margin_mb: int = GPU_MEMORY_MARGIN_MB,
        gpu_lock_dir: Path = Path("/tmp"),
        runtime: str = "runc",
    ) -> None:
        self.gpus = gpus
        #: What a GPU must have free beyond a shared Sandbox's need to admit it.
        self.gpu_memory_margin_mb = gpu_memory_margin_mb
        #: Where the lock file of each Docker daemon's GPU admission is, which every user of
        #: the Host can lock.
        self.gpu_lock_dir = gpu_lock_dir
        #: A key of :data:`RUNTIMES`: ``kata`` starts each sandbox in a microVM of its own.
        self.runtime = runtime
        # An image's conformance cannot change while it is pinned to a digest, and the
        # check costs a container start — so a sweep of ten thousand episodes pays for it
        # once rather than ten thousand times.
        self._checked: dict[str, list[str]] = {}
        self._gpu_models: tuple[GpuModel, ...] | None = None
        self._daemon: str | None = None
        self._networks = _Networks()
        self._watchdog = gpu_sharing.Watchdog()
        self._host_ports = rootless.HostPorts(self._rootless_bridges)

    @property
    def id(self) -> str:
        return f"{self.name}:{self.runtime}"

    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        runtime = RUNTIMES[self.runtime]
        models = await self._models() if runtime.gpus else ()
        profiles = []
        for features in runtime.feature_sets:
            # A commit keeps no volume and what a Sandbox writes to one is not measured
            # (storage.py), so a Sandbox with storage on one saves no snapshot and is held to
            # no storage_mb.

            profiles.append(
                CapabilityProfile(
                    provider=self.id,
                    kind=ImageKind.CONTAINER,
                    os=OperatingSystem.LINUX,
                    arch=machine_arch(),
                    gui=True,  # depends on the image; the GUI base image provides a desktop
                    network_modes=frozenset(NetworkMode),
                    snapshot=True,
                    gpus=bool(models),
                    gpu_models=models,
                    gpu_hold=runtime.gpu_hold if models else GpuHold.WHOLE,
                    storage_limit=True,
                    container_features=features,
                    volumes=True,
                )
            )
        return tuple(profiles)

    async def _models(self) -> tuple[GpuModel, ...]:
        """The models of the Host GPUs it may lease, read once: a Host's GPUs are a fact of
        it, which a Run selects by. None where nvidia-smi finds no GPU. Each model's memory is
        the most a shared Sandbox is admitted with on an empty GPU of it: its total less what
        the driver reserves and the margin, so that selection accepts no need admission never
        could."""
        if self._gpu_models is None:
            try:
                devices = await self._host_gpus()
            except ProviderCapabilityError:
                devices = {}
            indices = self.gpus or tuple(devices)
            self._gpu_models = tuple(
                dict.fromkeys(
                    GpuModel(name=devices[index].model, memory_mb=self._most(devices[index]))
                    for index in indices
                    if index in devices
                )
            )
        return self._gpu_models

    def _most(self, device: GpuDevice) -> int | None:
        """The most a shared Sandbox is admitted with on ``device`` when nothing else uses it;
        None where nvidia-smi does not report its memory."""
        total, reserved = device.memory_total_mb, device.memory_reserved_mb
        if total is None or reserved is None:
            return None
        return max(total - reserved - self.gpu_memory_margin_mb, 1)

    async def preflight(self, needs: Sequence[SandboxNeeds] = ()) -> SandboxCheck | None:
        """Find each registered name the Sandboxes of ``needs`` run under, every one its
        profile declares when none are given, registered with exactly the arguments the
        runtime takes, and the leaf's devices. A runtime Orbit builds must be this engine's
        build, and one that proxies the NVIDIA driver must support the Host's when GPUs may
        be attached: a Sandbox needs them, or, with no needs, its profiles offer them. One
        that limits a shared Sandbox's GPU memory must list the flag that does when a Sandbox
        may share GPUs. What a Sandbox of it must show is returned as a check."""
        if shutil.which("docker") is None:
            raise ProviderStartError("docker is not installed — see `just doctor`")
        code, raw, stderr = await _docker("info", "--format", "{{json .Runtimes}}", timeout=30)
        if code != 0:
            raise ProviderStartError(f"docker daemon unreachable: {stderr.strip()}")
        known: dict[str, dict[str, object]] = json.loads(raw)
        runtime = RUNTIMES[self.runtime]
        names = runtime.names(needs)
        for name in names:
            shim = _SHIM.fullmatch(name)
            if name not in known and not (
                shim and shutil.which(f"containerd-shim-{shim[1]}-{shim[2]}")
            ):
                raise ProviderStartError(
                    f"{self.id} needs the {name} runtime, which Docker does not have here; "
                    "see the Providers README"
                )
            wanted = runtime.arguments.get(name)
            given = [str(arg) for arg in known.get(name, {}).get("runtimeArgs") or []]  # type: ignore[attr-defined]
            if wanted is not None and Counter(given) != Counter(wanted):
                raise ProviderStartError(
                    f"{self.id} needs Docker's {name} runtime registered with "
                    f"{' '.join(wanted)}, not {' '.join(given) or 'no arguments'}; see the "
                    "Providers README"
                )
        if missing := [device for device in runtime.devices if not Path(device).exists()]:
            raise ProviderStartError(f"{self.id} needs {', '.join(missing)}, which is missing")
        if runtime.build or runtime.drivers or runtime.gpu_memory_flag:
            offered = bool(await self._models()) if not needs else False
            gpus = any(need.resources.gpus for need in needs) or offered
            shares = (
                any(need.resources.gpus and need.resources.gpu_memory_mb for need in needs)
                or offered
            )
            for path in sorted({str(known[name]["path"]) for name in names}):
                await self._check_build(path, runtime, gpus=gpus, shares=shares)
        return self._shows_runtime if runtime.banner else None

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if image.kind is not ImageKind.CONTAINER:
            raise ProviderCapabilityError("DockerProvider requires a container image")
        prepared = await resolve_container_image(image) if isinstance(image, ImageRef) else image
        resolved = await resolve_prepared_container_image(prepared)
        if problems := await self.check_image(resolved.observed_ref):
            raise ProviderCapabilityError(
                f"{prepared.runtime_ref} cannot serve as a sandbox: {'; '.join(problems)}. "
                "See docs/specs/sandbox-image.md"
            )
        return prepared

    async def _check_build(self, path: str, runtime: Runtime, *, gpus: bool, shares: bool) -> None:
        """Check that the runtime Docker runs from ``path`` is Orbit's build of it, that it has
        the flag that limits GPU memory when Sandboxes share GPUs, and, when they need GPUs,
        that it supports the Host's NVIDIA driver."""
        if runtime.build:
            try:
                code, version, stderr = await run(path, "--version", timeout=30)
            except OSError as error:
                raise ProviderStartError(f"{self.id} cannot run {path}: {error}") from None
            reported = (version.strip() or stderr.strip()).splitlines()[:1]
            if code != 0 or not any(
                word.endswith(runtime.build) for word in "".join(reported).split()
            ):
                raise ProviderStartError(
                    f"{self.id} needs Orbit's build {runtime.build} of {path}, not "
                    f"{reported[0] if reported else 'what it reports'}; see the Providers README"
                )
        if shares and runtime.gpu_memory_flag:
            # Go's flag package writes the list to stderr.
            code, out, err = await run(path, "flags", timeout=30)
            if code != 0 or f"-{runtime.gpu_memory_flag}" not in (out + err).split():
                raise ProviderStartError(
                    f"{self.id} cannot hold a Sandbox to its gpu_memory_mb: `{path} flags` "
                    f"lists no -{runtime.gpu_memory_flag}; see the Providers README"
                )
        if not gpus or not runtime.drivers:
            return
        drivers = {device.driver_version for device in (await self._host_gpus()).values()}
        code, listed, stderr = await run(path, *runtime.drivers, timeout=30)
        if code != 0:
            raise ProviderStartError(f"{path} {' '.join(runtime.drivers)} failed: {stderr.strip()}")
        if unsupported := sorted(drivers - set(listed.split())):
            raise ProviderCapabilityError(
                f"{self.id} cannot attach GPUs here: {path} does not support NVIDIA driver "
                f"{', '.join(unsupported)}"
            )

    async def _shows_runtime(self, sandbox: Sandbox) -> None:
        """Check that ``sandbox`` runs on this leaf's runtime, as its kernel log says, and
        that Docker sees what it writes, which its snapshots keep."""
        assert isinstance(sandbox, DockerSandbox)
        runtime = RUNTIMES[self.runtime]
        log = await sandbox.exec([guestd.PYTHON["linux"], "-I", "-c", _KERNEL_LOG], timeout_sec=60)
        if runtime.banner not in log.stdout:
            raise ProviderStartError(
                f"{self.id} started {sandbox.container}, whose kernel log does not say "
                f"{runtime.banner!r}: {(log.stdout or log.stderr).strip()[:200]}"
            )
        # The contents differ from the name, which the archive docker cp writes holds too.
        path = str(GUESTD_DIR.parent / f"preflight-{uuid.uuid4().hex}")
        contents = uuid.uuid4().hex
        await sandbox.write_file(path, contents.encode())
        code, copied, stderr = await _docker("cp", f"{sandbox.container}:{path}", "-", timeout=60)
        if code != 0 or contents not in copied:
            ran = sandbox.allocation.runtime or runtime.registered
            raise ProviderStartError(
                f"Docker does not see what {sandbox.container} writes, so a snapshot would "
                f"lose it: is {ran} registered with "
                f"{' '.join(runtime.arguments.get(ran, ()))}? {stderr.strip()}"
            )

    async def create(self, request: SandboxRequest) -> Sandbox:
        await self.accepts(request)
        runtime = RUNTIMES[self.runtime]
        resources = request.resources
        candidates: tuple[str, ...] = ()
        if resources.gpus:
            candidates = _eligible_gpus(await self._host_gpus(), self.gpus, resources)
        #: What it reserves on each GPU, which it shares; None when it takes them whole.
        shared = resources.gpu_memory_mb if resources.gpus else None
        written = 0
        if request.snapshot is not None:
            resolved_image, written = await self._snapshot_image(request.snapshot, request)
        else:
            resolved_image = await resolve_prepared_container_image(request.prepared_image)
            if problems := await self.check_image(resolved_image.observed_ref):
                raise ProviderCapabilityError(
                    f"{request.prepared_image.runtime_ref} cannot serve as a sandbox: "
                    f"{'; '.join(problems)}. "
                    "See docs/specs/sandbox-image.md"
                )
            resolved_image = await self._framed(resolved_image, request)

        sandbox_id = f"{request.episode_id}-{uuid.uuid4().hex[:6]}"
        container = f"orbit-{sandbox_id}"
        isolated = request.network.mode is not NetworkMode.OPEN
        # Named even for runc: a Host whose default runtime is another would run it there.
        registered, grant = runtime.granted(resources.container_features)
        saved = request.snapshot or request.volumes_from
        stacks = (
            volumes.decode((await _snapshot_record(saved))[0].get(volumes.LAYERS_LABEL))
            if saved is not None
            else None
        )
        if request.volumes_from is not None and stacks is not None:
            # A Sandbox of another image views the agent's volumes, never its engine's.
            stacks = {path: stack for path, stack in stacks.items() if not stack.copied}
        fresh = (
            await volumes.directories(
                resolved_image.observed_ref, guestd.PYTHON["linux"], request.volumes
            )
            if stacks is None
            else ()
        )
        mounts = await volumes.mount(
            sandbox_id, fresh=fresh, engines=grant.volumes, stacks=stacks, helper=NETFILTER_IMAGE
        )
        meter: volumes.Meter | None = None
        # No swap: Docker's default lets a container swap as much again, as far as the Host's
        # swap reaches, so the memory a Task gets would vary from Host to Host.
        memory_limit = f"{runtime.memory_mb(resources.memory_mb)}m"
        argv = [
            "run", "-d", "--name", container,
            "--label", f"{LABEL}={request.episode_id}",
            "--label", f"{MANAGED_LABEL}=true",
            "--label", f"{ROLE_LABEL}={request.role.value}",
            "--label", f"{RETENTION_LABEL}={request.retention}",
            "--label", f"{NETWORK_LABEL}={request.network.mode.value}",
            "--label", f"{WRITTEN_LABEL}={written}",
            "--label", f"{volumes.MOUNTS_LABEL}={volumes.mounts_label(mounts)}",
            "--cpus", str(resources.cpus),
            "--memory", memory_limit,
            "--memory-swap", memory_limit,
            # Kata ignores it: its guest's own /dev/shm already holds half the microVM.
            "--shm-size", f"{memory.shm_mb(resources.memory_mb)}m",
            "--runtime", registered,
            *runtime.flags,
        ]  # fmt: skip
        for capability in grant.capabilities:
            argv += ["--cap-add", capability]
        for mount in mounts:
            argv += ["--mount", mount.argument]
        if (shown := request.shown) is not None:
            # The Task's own folder, read-only from the start: not even the agent's sudo writes
            # to it, and its assets, however large, are never copied.
            argv += ["--mount", _bind_mount(shown.source, shown.target)]
            argv += ["--label", f"{SHOWN_LABEL}=true"]
        for key, value in request.env.items():
            argv += ["-e", f"{key}={value}"]
        argv += ["-e", f"{CONTAINER_VARIABLE}={request.desktop_resolution}"]

        confinement: _Confinement | None = None
        admitted: tuple[GpuDevice, ...] = ()
        sessions: list[_Session] = []
        watcher: memory.GuestEvents | None = None
        try:
            # Its network, if it has one of its own, is created here, so that every failure
            # from now on removes it.
            confinement = await runtime.confinement.start(sandbox_id, request, self._networks)
            argv += confinement.run_args(isolated)
            await self._host_ports.hold(container, _ports_on_host(request))
            if request.gateway_url or request.proxy_url:
                host_ip = confinement.host_ip
                argv += ["--add-host", f"{HOST_ALIAS}:{host_ip}"]
                if request.gateway_url:
                    argv += ["-e", f"ORBIT_GATEWAY_URL={_sandbox_gateway(request.gateway_url)}"]
                if request.proxy_url:
                    request = request.model_copy(
                        update={"proxy_url": _reachable(request.proxy_url, host=host_ip)}
                    )
            # One lock for every GPU this daemon's Sandboxes may take, held from reading what
            # is free until Docker has started the Sandbox, whose labels then record what it
            # took.
            async with (
                gpu_sharing.locked(await self._lock_path())
                if resources.gpus
                else contextlib.nullcontext()
            ):
                if resources.gpus:
                    admitted = await self._admit(resources, candidates)
                    argv += _gpu_run_args(tuple(device.id for device in admitted))
                    if shared is not None and runtime.gpu_memory_flag:
                        # An allocation past it fails as CUDA out of memory; the Sandbox goes on.
                        limit = f"dev.gvisor.flag.{runtime.gpu_memory_flag}={shared << 20}"
                        argv += ["--annotation", limit]
                argv += [
                    "--label", f"{GPU_LABEL}={','.join(device.id for device in admitted)}",
                    "--label", f"{GPU_MEMORY_LABEL}={shared or ''}",
                ]  # fmt: skip
                # The image's own command, always. A container lives exactly as long as its
                # command, and substituting one replaces whatever the image starts for
                # itself — for a desktop image, the entire graphical session. Images that
                # have nothing to do declare a command that waits; that is part of the image
                # contract, so there is nothing left here to decide.
                code, started_id, stderr = await _docker(
                    *argv, resolved_image.observed_ref, timeout=_START_SEC
                )
            if code != 0:
                if resources.gpus:
                    raise ProviderCapabilityError(
                        "Docker could not inject the selected NVIDIA GPUs through its "
                        f"runtime/CDI path: {stderr.strip()}"
                    )
                raise ProviderStartError(f"could not start container: {stderr.strip()}")

            code, started, stderr = await _docker(
                "inspect", "--format", "{{.Image}} {{.HostConfig.Runtime}}", container, timeout=30
            )
            started_content, _, ran_under = started.strip().partition(" ")
            if code != 0 or started_content != resolved_image.observed_identity:
                raise ProviderStartError(
                    "started container image identity does not match the resolved image: "
                    f"{stderr.strip() or started_content}"
                )
            if ran_under != registered:
                raise ProviderStartError(
                    f"Docker started {container} under runtime {ran_under!r}, not {registered!r}"
                )

            runtime_identity = None
            if admitted:
                code, version, stderr = await _docker(
                    "info", "--format", "{{.ServerVersion}}", timeout=30
                )
                if code != 0 or not version.strip():
                    raise ProviderCapabilityError(
                        f"could not identify the Docker GPU runtime: {stderr.strip()}"
                    )
                runtime_identity = f"docker-engine/{version.strip()}"

            await confinement.wire(container, request)
            watcher = await _watcher(container, runtime)
            agent_user = request.user
            session = await self._connect(container, agent_user, resources.memory_mb, watcher)
            sessions.append(session)
            client = session.client
            if isolated:
                await confinement.seal_guest(client)
            agent_home, has_desktop = await self._guest_contract(client)
            for mount in mounts:
                if mount.root is not None:
                    root = mount.root
                    code, _, err, _ = await client.exec(
                        [guestd.PYTHON["linux"], "-I", "-c", _SET_ROOT, mount.path,
                         str(root.uid), str(root.gid), str(root.mode)]
                    )  # fmt: skip
                    if code:
                        raise ProviderStartError(f"could not restore {mount.path}: {err.strip()}")
            if mounts:
                meter = await volumes.Meter.start(sandbox_id, mounts, NETFILTER_IMAGE)
            await set_and_prove_elevation(client, request, agent_user)
            observed_gpu: tuple[GpuDevice, ...] = ()
            if admitted:
                observed_gpu = _verify_docker_gpu(admitted, await self._container_gpus(client))
            desktop: DesktopResolution | None = None
            if has_desktop:
                await self._await_desktop(client)
                desktop = await require_resolution(
                    client, request.desktop_resolution, container=True
                )
            sessions.append(
                await self._connect(container, agent_user, resources.memory_mb, watcher)
            )
        except BaseException:
            self._host_ports.release(container)
            await asyncio.gather(
                *(session.client.close() for session in sessions), return_exceptions=True
            )
            if watcher is not None:
                await watcher.close()
            with contextlib.suppress(BaseException):
                await asyncio.shield(_docker("rm", "-f", "-v", container, timeout=_REMOVE_SEC))
            with contextlib.suppress(BaseException):
                if meter is not None:
                    await asyncio.shield(meter.stop())
            with contextlib.suppress(BaseException):
                await asyncio.shield(volumes.remove(mounts))
            if confinement is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(confinement.remove())
            raise

        selected = tuple(device.id for device in admitted)
        sandbox = DockerSandbox(
            sandbox_id=sandbox_id,
            request=request,
            container=container,
            container_id=started_id.strip(),
            confinement=confinement,
            session=sessions[0],
            file_session=sessions[1],
            resolved_image=resolved_image,
            allocation=ResourceAllocation(
                cpus=resources.cpus,
                memory_mb=resources.memory_mb,
                storage_mb=resources.storage_mb,
                gpu=(
                    GpuAllocation(
                        requested_count=resources.gpus,
                        provider_addresses=selected,
                        lease_keys=selected,
                        observed_devices=observed_gpu,
                        runtime_identity=runtime_identity,
                        hold=GpuHold.WHOLE if shared is None else runtime.gpu_hold,
                        memory_mb=shared,
                    )
                    if admitted
                    else None
                ),
                sudo=request.sudo,
                network_mode=request.network.mode,
                provider=self.id,
                leaf=self.id,
                runtime=ran_under,
                desktop_resolution=desktop,
            ),
            agent_user=agent_user,
            agent_home=agent_home,
            watchdog=self._watchdog,
            host_ports=self._host_ports,
            guest_kernel=runtime.guest_kernel,
            written=written,
            mounts=mounts,
            meter=meter,
        )
        sandbox.shown = request.shown is not None
        sandbox.watch()
        return sandbox

    async def _rootless_bridges(self) -> rootless.Namespaces | None:
        """Where the daemon's bridges are when it is rootless and they are not in this
        process's network namespace; None otherwise."""
        code, options, stderr = await _docker(
            "info", "--format", "{{json .SecurityOptions}}", timeout=30
        )
        if code != 0:
            raise ProviderStartError(f"could not read how Docker runs: {stderr.strip()}")
        if "name=rootless" not in options:
            return None
        code, endpoint, stderr = await _docker(
            "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", timeout=30
        )
        if code != 0:
            raise ProviderStartError(f"could not find the Docker daemon: {stderr.strip()}")
        return rootless.namespaces(endpoint.strip())

    async def _lock_path(self) -> Path:
        """The lock file of this Docker daemon's GPU admission, by the daemon's ID, which
        every Orbit process that admits through the daemon takes, whatever its user or cache."""
        if self._daemon is None:
            code, out, stderr = await _docker("info", "--format", "{{.ID}}", timeout=30)
            if code != 0 or not out.strip():
                raise ProviderStartError(f"could not identify the Docker daemon: {stderr.strip()}")
            self._daemon = out.strip()
        return self.gpu_lock_dir / f"orbit-gpu-{self._daemon}.lock"

    async def _admit(
        self, resources: Resources, candidates: tuple[str, ...]
    ) -> tuple[GpuDevice, ...]:
        """The GPUs among ``candidates`` a Sandbox of ``resources`` takes, as nvidia-smi
        reports them now. Called with the admission lock held."""
        host = await self._host_gpus()
        holders = await _gpu_holders()
        sharing = {holder.container for holder in holders if holder.memory_mb is not None}
        processes = (
            [
                process
                for process in await gpu_sharing.read_processes()
                if process.container in sharing
            ]
            if sharing
            else []
        )
        return gpu_sharing.admit(
            [(index, device) for index, device in sorted(host.items()) if device.id in candidates],
            holders,
            processes,
            count=resources.gpus,
            memory_mb=resources.gpu_memory_mb,
            margin_mb=self.gpu_memory_margin_mb,
        )

    async def _host_gpus(self) -> dict[int, GpuDevice]:
        if shutil.which("nvidia-smi") is None:
            raise ProviderCapabilityError("nvidia-smi is not installed on the Docker host")
        code, stdout, stderr = await run(
            "nvidia-smi",
            f"--query-gpu={_HOST_NVIDIA_QUERY}",
            "--format=csv,noheader,nounits",
            timeout=30,
        )
        if code != 0:
            raise ProviderCapabilityError(f"host nvidia-smi failed: {stderr.strip()}")
        return _parse_host_nvidia_smi(stdout)

    async def _container_gpus(self, client: GuestClient) -> tuple[GpuDevice, ...]:
        code, stdout, stderr, _ = await client.exec(
            [
                "nvidia-smi",
                f"--query-gpu={_NVIDIA_QUERY}",
                "--format=csv,noheader,nounits",
            ],
            timeout_sec=30,
        )
        if code != 0:
            raise ProviderCapabilityError(
                f"Docker started the GPU sandbox but its nvidia-smi path failed: {stderr.strip()}"
            )
        return _parse_nvidia_smi(stdout)

    async def check_image(self, reference: str) -> list[str]:
        """What this image is missing from the sandbox contract, if anything.

        Checked before the sandbox is used rather than discovered through its symptoms:
        an image with no long-lived command dies immediately, and one with no agent
        account fails on the first thing the agent tries to do — neither failure names
        its cause. See `docs/specs/sandbox-image.md`.

        Partial by construction: whether a command *stays* running cannot be known
        without running it, so this catches an image that declares none at all. The rest
        surfaces as a sandbox that dies on start, which at least says so immediately.
        """
        if reference in self._checked:
            return self._checked[reference]

        missing: list[str] = []

        code, out, _ = await _docker(
            "image",
            "inspect",
            "--format",
            "{{json .Config.Cmd}}{{json .Config.Entrypoint}}",
            reference,
        )
        if code != 0:
            return [f"image {reference} could not be inspected; is it pulled?"]
        if out.strip().startswith("null"):
            missing.append(
                "no command: a container lives exactly as long as its command, and the "
                "engine will not supply one"
            )
        self._checked[reference] = missing
        return missing

    async def _framed(self, resolved: ResolvedImage, request: SandboxRequest) -> ResolvedImage:
        """Add the engine's framework layer to an image that was not prepared with it.

        Task preparation adds the layer already; this covers an image handed straight to
        the Provider, so any image can serve as a sandbox.
        """
        layered = await with_framework(resolved.observed_identity, user=request.user)
        if layered == resolved.observed_identity:
            return resolved
        return resolved.model_copy(update={"observed_identity": layered, "observed_ref": layered})

    async def _guest_contract(self, client: GuestClient) -> tuple[str, bool]:
        """The agent's home and whether the image runs a desktop, as the guest reports."""
        health = await client.health()
        home = str(health.get("agent_home") or "")
        if not home:
            raise ProviderCapabilityError("guestd did not report the agent account's home")
        return home, bool(health.get("gui"))

    async def _await_desktop(self, client: GuestClient, timeout_sec: float = 120) -> None:
        """Block until the screen can actually be captured.

        A desktop image starts an X server, a window manager and a session from its own
        command, and none of that is instant. Waiting on a marker file would work for
        one image and be a lie for the next, so this waits on the capability itself:
        a sandbox is ready for GUI work when a screenshot succeeds.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        last: Exception | None = None
        while loop.time() < deadline:
            try:
                await client.screenshot()
                return
            except Exception as exc:
                last = exc
                await asyncio.sleep(1.0)
        raise ProviderStartError(
            f"the desktop did not come up within {timeout_sec:g}s; last attempt: {last}"
        )

    async def _connect(
        self,
        container: str,
        agent_user: str,
        memory_mb: int,
        watcher: memory.GuestEvents | None = None,
    ) -> _Session:
        session = _Session(
            [
                "docker",
                "exec",
                "-i",
                # The guest service runs as root whatever user the image itself declares.
                "--user",
                "0",
                "--env",
                f"ORBIT_AGENT_USER={agent_user}",
                # In a directory the agent cannot write, which root children start in.
                "--workdir",
                "/",
                container,
                # The framework's own Python, whatever the image has, isolated from the
                # image's environment, which an exec inherits: PYTHONUNBUFFERED would leave
                # its output to a pipe's short writes, PYTHONPATH would replace its modules.
                guestd.PYTHON["linux"],
                "-I",
                str(GUESTD_DIR / "main.py"),
                "--stdio",
            ],
            container,
            memory_mb,
            watcher,
        )
        try:
            await session.start()
            await asyncio.wait_for(session.client.health(), timeout=60)
        except BaseException as exc:
            await session.client.close()
            if not isinstance(exc, Exception):
                raise
            raise ProviderStartError(f"guest service did not answer: {exc}") from exc
        return session

    async def list_retained(self) -> list[dict[str, object]]:
        if shutil.which("docker") is None:
            return []  # a machine without Docker keeps none of these
        code, output, stderr = await _docker(
            "ps",
            "-a",
            "-q",
            "--filter",
            f"label={MANAGED_LABEL}=true",
            "--filter",
            f"label={RETENTION_LABEL}=keep",
            timeout=30,
        )
        if code != 0:
            raise ProviderStartError(f"could not list Orbit sandboxes: {stderr.strip()}")
        handles = output.split()
        found = await self._list_snapshots()
        if not handles:
            return found
        code, raw, stderr = await _docker("inspect", *handles, timeout=30)
        if code != 0 and not only_vanished(stderr):
            raise ProviderStartError(f"could not inspect Orbit sandboxes: {stderr.strip()}")
        records = json.loads(raw) if raw.strip() else []
        for record in records:
            labels = record.get("Config", {}).get("Labels", {}) or {}
            gpu = tuple(filter(None, labels.get(GPU_LABEL, "").split(",")))
            container = record.get("Name", "").removeprefix("/")
            handle = f"{HANDLE_PREFIX}{container}"
            found.append(
                {
                    "provider": "docker",
                    "handle": handle,
                    "episode": labels.get(LABEL, ""),
                    "role": labels.get(ROLE_LABEL, ""),
                    "image": record.get("Image", ""),
                    "gpus": gpu,
                    "running": bool(record.get("State", {}).get("Running")),
                    "cleanup_command": f"orbit sandbox destroy {handle}",
                }
            )
        return sorted(found, key=lambda item: str(item["handle"]))

    async def _list_snapshots(self) -> list[dict[str, object]]:
        code, output, stderr = await _docker(
            "images",
            "--filter",
            f"reference={SNAPSHOT_REPOSITORY}:*",
            "--format",
            "{{.Tag}}",
            timeout=30,
        )
        if code != 0:
            raise ProviderStartError(f"could not list Orbit snapshots: {stderr.strip()}")
        found: list[dict[str, object]] = []
        for tag in output.split():
            handle = f"{SNAPSHOT_PREFIX}{tag}"
            try:
                labels, identity = await _snapshot_record(handle)
            except ProviderCapabilityError:
                continue  # tagged by hand, not by Orbit
            except ProviderStartError:
                continue  # destroyed by another run since it was listed
            found.append(
                {
                    "provider": "docker",
                    "handle": handle,
                    "episode": labels.get(LABEL, ""),
                    "role": labels.get(ROLE_LABEL, ""),
                    "image": identity,
                    "gpus": (),
                    "running": False,
                    "cleanup_command": f"orbit sandbox destroy {handle}",
                }
            )
        return found

    async def _snapshot_image(
        self, handle: str, request: SandboxRequest
    ) -> tuple[ResolvedImage, int]:
        """The snapshot a Sandbox boots from, which must come from the Task's prepared image,
        and the bytes it holds over that image."""
        labels, identity = await _snapshot_record(handle)
        if labels.get(PREPARED_LABEL) != request.prepared_image.prepared_identity:
            raise ProviderCapabilityError(
                f"{handle!r} was not taken from the Task's prepared solver image"
            )
        image = ResolvedImage(
            kind=ImageKind.CONTAINER,
            prepared_identity=request.prepared_image.prepared_identity,
            observed_identity=identity,
            observed_ref=identity,
        )
        return image, int(labels.get(WRITTEN_LABEL) or 0)

    async def destroy_retained(self, handle: str) -> None:
        if handle.startswith(SNAPSHOT_PREFIX):
            labels, _ = await _snapshot_record(handle)
            reference = _snapshot_reference(handle)
            # Not forced: a sandbox booted from the snapshot still uses its layers.
            code, _, stderr = await _docker("rmi", reference, timeout=120)
            if code != 0:
                raise ProviderStartError(f"could not destroy {handle}: {stderr.strip()}")
            # Its volume layers go too, but those a later snapshot or a view still stacks.
            stacks = volumes.decode(labels.get(volumes.LAYERS_LABEL)).values()
            await volumes.collect(layer for stack in stacks for layer in stack.layers)
            return
        await self._destroy(handle, retained=True)

    async def discard(self, handle: str) -> None:
        await self._destroy(handle, retained=False)

    async def _destroy(self, handle: str, *, retained: bool) -> None:
        """Remove an Orbit container and its network by its handle. ``retained`` requires a
        kept one; otherwise one that is already gone needs nothing."""
        if handle.startswith(SNAPSHOT_PREFIX) or not handle.startswith(HANDLE_PREFIX):
            raise ProviderCapabilityError(f"{handle!r} is not a Docker sandbox handle")
        container = handle.removeprefix(HANDLE_PREFIX)
        code, raw, stderr = await _docker("inspect", container, timeout=30)
        if code != 0:
            if not only_vanished(stderr):
                raise ProviderStartError(f"could not inspect {handle}: {stderr.strip()}")
            if retained:
                raise ProviderStartError(f"sandbox {handle!r} does not exist")
            return
        record = json.loads(raw)[0]
        labels = record.get("Config", {}).get("Labels", {}) or {}
        kept = labels.get(RETENTION_LABEL) == "keep"
        if labels.get(MANAGED_LABEL) != "true" or (retained and not kept):
            kind = "Orbit-retained" if retained else "Orbit"
            raise ProviderCapabilityError(f"{handle!r} is not an {kind} Docker sandbox")
        networks = tuple(
            name
            for name in (record.get("NetworkSettings", {}).get("Networks", {}) or {})
            if name.startswith("orbit-net-")
        )
        code, _, stderr = await _docker("rm", "-f", "-v", container, timeout=_REMOVE_SEC)
        if code != 0:
            raise ProviderStartError(f"could not destroy {handle}: {stderr.strip()}")
        await volumes.Meter(container.removeprefix("orbit-")).stop()
        await volumes.remove(volumes.from_label(labels.get(volumes.MOUNTS_LABEL)))
        for network in networks:
            await _remove_network(network)

    async def attach_retained(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
    ) -> DockerSandbox:
        """Reconnect to a running retained container without taking ownership of it."""
        return await self._attach(handle, request, allocation, image, retained=True)

    async def reattach(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
    ) -> DockerSandbox:
        return await self._attach(handle, request, allocation, image, retained=False)

    async def _attach(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
        *,
        retained: bool,
    ) -> DockerSandbox:
        """A running Orbit container as a Sandbox again; ``retained`` requires a kept solver."""
        if handle.startswith(SNAPSHOT_PREFIX) or not handle.startswith(HANDLE_PREFIX):
            raise ProviderCapabilityError(f"{handle!r} is not a Docker sandbox handle")
        container = handle.removeprefix(HANDLE_PREFIX)
        code, raw, stderr = await _docker("inspect", container, timeout=30)
        if code != 0:
            raise ProviderStartError(f"sandbox {handle!r} does not exist: {stderr.strip()}")
        record = json.loads(raw)[0]
        labels = record.get("Config", {}).get("Labels", {}) or {}
        role = labels.get(ROLE_LABEL, "")
        if labels.get(MANAGED_LABEL) != "true" or role not in {r.value for r in SandboxRole}:
            raise ProviderCapabilityError(f"{handle!r} is not an Orbit Docker sandbox")
        if retained and (
            labels.get(RETENTION_LABEL) != "keep"
            or role not in {SandboxRole.SOLVER.value, SandboxRole.SHARED.value}
        ):
            raise ProviderCapabilityError(f"{handle!r} is not a retained Orbit solver sandbox")
        if not record.get("State", {}).get("Running"):
            raise ProviderStartError(f"sandbox {handle!r} is not running")

        if record.get("Image") != image.observed_identity:
            raise ProviderCapabilityError(f"{handle!r} does not use the expected image")
        attached = record.get("NetworkSettings", {}).get("Networks", {}) or {}
        networks = tuple(name for name in attached if name.startswith("orbit-net-"))
        if len(networks) > 1:
            raise ProviderCapabilityError(f"{handle!r} has more than one Orbit network")
        # The container, not this run's configuration, says how it was isolated, and whether
        # it is sealed now: a proxied agent's commands need the proxy only then.
        try:
            mode = NetworkMode(labels.get(NETWORK_LABEL, ""))
        except ValueError:
            raise ProviderCapabilityError(f"{handle!r} records no network mode") from None
        request = request.model_copy(
            update={"network": request.network.model_copy(update={"mode": mode})}
        )
        isolated = mode is not NetworkMode.OPEN
        runtime = _runtime_named(str(record.get("HostConfig", {}).get("Runtime")))
        confinement = await runtime.confinement.found(networks, self._networks, labels)
        agent_user = request.user
        watcher = await _watcher(container, runtime)
        memory_mb = allocation.memory_mb
        session: _Session | None = None
        try:
            await self._host_ports.hold(container, _ports_on_host(request))
            await confinement.wire(container, request, reattached=True)
            sealed = isolated and await confinement.sealed(container, attached)
            session = await self._connect(container, agent_user, memory_mb, watcher)
            agent_home, _ = await self._guest_contract(session.client)
            file_session = await self._connect(container, agent_user, memory_mb, watcher)
        except BaseException:
            self._host_ports.release(container)
            if session is not None:
                await session.client.close()
            if watcher is not None:
                await watcher.close()
            raise
        sandbox = DockerSandbox(
            sandbox_id=container.removeprefix("orbit-"),
            request=request.model_copy(update={"role": SandboxRole(role)}),
            container=container,
            container_id=str(record.get("Id", "")),
            confinement=confinement,
            session=session,
            file_session=file_session,
            resolved_image=image,
            allocation=allocation,
            agent_user=agent_user,
            agent_home=agent_home,
            sealed=sealed,
            watchdog=self._watchdog,
            host_ports=self._host_ports,
            guest_kernel=runtime.guest_kernel,
            written=int(labels.get(WRITTEN_LABEL) or 0),
            mounts=(found := volumes.from_label(labels.get(volumes.MOUNTS_LABEL))),
            meter=volumes.Meter(container.removeprefix("orbit-")) if found else None,
        )
        sandbox.shown = labels.get(SHOWN_LABEL) == "true"
        if sealed:
            try:
                # This run's proxy and token, through a guestd of this run's own.
                await sandbox._start_relay()
            except BaseException:
                await sandbox._release()
                raise
        # The allocation its first run recorded says what it reserved.
        sandbox.watch()
        return sandbox


def _snapshot_reference(handle: str) -> str:
    if not handle.startswith(SNAPSHOT_PREFIX):
        raise ProviderCapabilityError(f"{handle!r} is not a Docker snapshot handle")
    return f"{SNAPSHOT_REPOSITORY}:{handle.removeprefix(SNAPSHOT_PREFIX)}"


def only_vanished(stderr: str) -> bool:
    """Whether ``docker inspect`` failed only for objects that no longer exist.

    Another process can destroy a sandbox between finding and inspecting it: a listing keeps
    the sandboxes it still found, which are on stdout, and a discard has nothing left to do.
    """
    errors = [line for line in stderr.splitlines() if line.strip()]
    return bool(errors) and all("no such object" in line.lower() for line in errors)


async def _snapshot_record(handle: str) -> tuple[dict[str, str], str]:
    """A snapshot's labels and image identity, once it is shown to be Orbit's."""
    code, raw, stderr = await _docker("image", "inspect", _snapshot_reference(handle), timeout=30)
    if code != 0:
        raise ProviderStartError(f"snapshot {handle!r} does not exist: {stderr.strip()}")
    record = json.loads(raw)[0]
    labels = record.get("Config", {}).get("Labels", {}) or {}
    if labels.get(MANAGED_LABEL) != "true" or labels.get(RETENTION_LABEL) != "snapshot":
        raise ProviderCapabilityError(f"{handle!r} is not an Orbit snapshot")
    return labels, str(record.get("Id", ""))


def _url_port(url: str | None) -> str:
    port = urlsplit(url).port if url else None
    return str(port) if port else ""


#: The hosts of a URL this Host serves, which a container reaches at the Host's address on
#: its network.
_ON_HOST = frozenset({"0.0.0.0", "127.0.0.1", "localhost", "::"})


def _bind_mount(source: str, target: str) -> str:
    """``docker run --mount`` of the Host's ``source`` at ``target``, read-only, in Docker's
    CSV form, so that a path holding a comma or a quote stays one field."""
    line = io.StringIO()
    csv.writer(line).writerow(["type=bind", f"src={source}", f"dst={target}", "readonly"])
    return line.getvalue().rstrip("\r\n")


def _ports_on_host(request: SandboxRequest) -> list[int]:
    """The ports of the Gateway and egress proxy a Sandbox reaches where this Host serves
    them."""
    return [
        port
        for url in (request.gateway_url, request.proxy_url)
        if url and urlsplit(url).hostname in _ON_HOST and (port := urlsplit(url).port)
    ]


def _sandbox_gateway(url: str | None) -> str | None:
    """The Gateway as every container dials it: the Host's alias at the fixed port.

    A Gateway elsewhere than on this Host is left as it is.
    """
    if not url:
        return url
    parts = urlsplit(url)
    if parts.hostname not in _ON_HOST:
        return url
    return parts._replace(netloc=f"{HOST_ALIAS}:{SANDBOX_GATEWAY_PORT}").geturl()


def _reachable(url: str | None, *, host: str = HOST_ALIAS) -> str | None:
    """Rewrite a host-side gateway URL into one a container can dial."""
    if not url:
        return url
    for bound in ("0.0.0.0", "127.0.0.1", "localhost", "[::]"):
        if f"//{bound}:" in url:
            return url.replace(f"//{bound}:", f"//{host}:", 1)
    return url
