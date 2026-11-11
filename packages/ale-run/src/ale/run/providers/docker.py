"""Container backend.

Shells out to the docker CLI rather than binding an SDK: the CLI is stable, already
installed wherever docker is, and keeps this provider readable.

Network isolation is topological, not rule-based. Each run gets its own
``--internal`` bridge, which has no route off the host, and the gateway is attached to
that bridge. Deny-all with a single controlled egress therefore falls out of the
network's shape — there are no firewall rules to survive a daemon restart, and nothing
inside the sandbox can lift them.

Under the Kata runtime each container is a microVM, which sees no network attached after
it boots. There the sandbox stays on the default bridge, and tc filters in its network
namespace, outside the VM, seal it instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import uuid
from collections import Counter
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import quote, urlsplit

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    Capabilities,
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
    SandboxFile,
    SandboxRequest,
    SandboxRole,
    SandboxState,
)
from ale.core.taskspec import NetworkMode, OperatingSystem, Resources
from ale.run import ale_image, guestd
from ale.run.framework import with_framework
from ale.run.gpu import GpuLease, accepts_model, parse_nvidia_smi
from ale.run.images import resolve_container_image, resolve_prepared_container_image
from ale.run.providers.desktop import CONTAINER_VARIABLE, require_resolution
from ale.run.providers.privileges import set_elevation, verify_identities
from ale.run.sources import cache_root
from ale.run.transport import GuestClient, StdioTransport

__all__ = [
    "DockerProvider",
    "DockerSandbox",
]

GUESTD_DIR = PurePosixPath("/opt/ale/guestd")
#: Any image with iptables, and tc for a Kata sandbox, can apply a sandbox's host rules;
#: ALE's QEMU runner, which needs both for its own guest, is one.
NETFILTER_IMAGE = ale_image("ale-qemu-runner")

LABEL = "ale.episode"
MANAGED_LABEL = "ale.managed"
ROLE_LABEL = "ale.role"
RETENTION_LABEL = "ale.retention"
GPU_LABEL = "ale.gpus"
PREPARED_LABEL = "ale.prepared"
HANDLE_PREFIX = "docker:"
#: A snapshot is a local image in this repository, tagged with its Sandbox's ID.
SNAPSHOT_REPOSITORY = "ale-snapshot"
SNAPSHOT_PREFIX = f"{HANDLE_PREFIX}snapshot:"

#: The name a sandbox uses for its gateway. It is mapped to the host's address *on the
#: container's own bridge* rather than to docker's usual host-gateway: an ``--internal``
#: network has no default route at all, so the only address that works is one inside the
#: sandbox's own subnet. That is the provider's half of the deny-all bargain — no route
#: off the host, and exactly one destination still reachable.
HOST_ALIAS = "ale-gateway.internal"

#: The nat chain, in a sandbox's own network namespace, that sends the fixed Gateway port
#: to the port the Host serves the Gateway on.
GATEWAY_CHAIN = "ALE_GATEWAY"

#: The containerd shim that starts each container in a Kata Containers microVM. Docker
#: runs it by this name from its PATH, so the daemon needs no setting and no restart.
KATA_RUNTIME = "io.containerd.kata.v2"
#: The VM's tap and the container's veth in the sandbox's network namespace. Kata passes
#: every frame between them with tc filters, past the namespace's netfilter, so the
#: Provider's rules for a Kata sandbox are tc filters placed ahead of Kata's own.
KATA_TAP = "tap0_kata"
KATA_VETH = "eth0"
#: tc preferences: lower runs first, the seal's come next, and Kata's own sit at 49152.
_KATA_GATEWAY_PREF = 10
_KATA_PROXY_PREF = 11
_KATA_HOST_PREF = 12

_NVIDIA_QUERY = "uuid,name,pci.bus_id,driver_version"
_HOST_NVIDIA_QUERY = f"index,{_NVIDIA_QUERY}"


async def _managed_gpu_ids() -> set[str]:
    code, output, _ = await _docker(
        "ps",
        "--filter",
        f"label={MANAGED_LABEL}=true",
        "--format",
        f'{{{{.Label "{GPU_LABEL}"}}}}',
        timeout=30,
    )
    if code != 0:
        return set()
    return {device for line in output.splitlines() for device in line.split(",") if device}


async def _docker(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise ProviderStartError(
            f"docker {' '.join(argv[:2])} timed out after {timeout:g}s"
        ) from None
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def _command(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise ProviderStartError(f"{argv[0]} timed out after {timeout:g}s") from None
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


def _parse_nvidia_smi(output: str) -> tuple[GpuDevice, ...]:
    return parse_nvidia_smi(output)


def _parse_host_nvidia_smi(output: str) -> dict[int, GpuDevice]:
    devices: dict[int, GpuDevice] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            raw_index, device_row = line.split(",", 1)
            index = int(raw_index.strip())
        except ValueError as exc:
            raise ProviderCapabilityError(
                f"could not parse nvidia-smi GPU index: {line!r}"
            ) from exc
        if index < 0 or index in devices:
            raise ProviderCapabilityError(f"invalid or duplicate nvidia-smi GPU index: {index}")
        devices[index] = parse_nvidia_smi(device_row)[0]
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


def _storage_run_args(storage_mb: int) -> list[str]:
    return ["--storage-opt", f"size={storage_mb}M"]


def _kata_memory_mb(memory_mb: int) -> int:
    """The VM size that leaves a Kata sandbox's processes the memory its Task declared.

    Kata makes the VM exactly the container's memory limit, and its guest kernel and agent
    take about 96 MiB plus 2.5 % of it (measured with Kata 4.3.1 from 512 MiB to 8 GiB).
    """
    return memory_mb + 96 + memory_mb // 40


def _kata_filters(
    host_ip: str,
    *,
    gateway_port: str,
    proxy_port: str,
    isolated: bool,
    reattached: bool,
) -> list[str]:
    """The tc commands that give a Kata sandbox what ``_wire_host`` gives a runc one.

    They act on frames between the VM and its veth, in the namespace the helper joins,
    outside the guest: root in the VM cannot lift them. The fixed Gateway port is
    rewritten to the Host's in both directions. An isolated sandbox reaches no other port
    of the Host's address and starts sealed. A reattaching run replaces only the Gateway
    and proxy filters, so the Host stays confined while it does.
    """
    tap, veth = KATA_TAP, KATA_VETH
    commands = [f"test -e /sys/class/net/{tap}", f"test -e /sys/class/net/{veth}"]
    if reattached:
        commands += [
            f"{{ tc filter del dev {device} ingress pref {pref} 2>/dev/null || true; }}"
            for device in (tap, veth)
            for pref in (_KATA_GATEWAY_PREF, _KATA_PROXY_PREF)
        ]
    elif isolated:
        commands += [
            f"tc filter add dev {tap} ingress pref {_KATA_HOST_PREF} protocol ip "
            f"flower dst_ip {host_ip} action drop",
            *_kata_seal(host_ip),
        ]
    if gateway_port:
        rewrite = "action pedit ex munge tcp {field} set {port} pipe action csum tcp pipe"
        commands += [
            f"tc filter add dev {tap} ingress pref {_KATA_GATEWAY_PREF} protocol ip flower "
            f"dst_ip {host_ip} ip_proto tcp dst_port {SANDBOX_GATEWAY_PORT} "
            + rewrite.format(field="dport", port=gateway_port)
            + f" action mirred egress redirect dev {veth}",
            f"tc filter add dev {veth} ingress pref {_KATA_GATEWAY_PREF} protocol ip flower "
            f"src_ip {host_ip} ip_proto tcp src_port {gateway_port} "
            + rewrite.format(field="sport", port=SANDBOX_GATEWAY_PORT)
            + f" action mirred egress redirect dev {tap}",
        ]
    if proxy_port and isolated:
        commands.append(
            f"tc filter add dev {tap} ingress pref {_KATA_PROXY_PREF} protocol ip flower "
            f"dst_ip {host_ip} ip_proto tcp dst_port {proxy_port} "
            f"action mirred egress redirect dev {veth}"
        )
    return commands


def _kata_seal_filters(host_ip: str) -> list[tuple[str, int, str]]:
    """Device, preference and filter that pass a sealed Kata sandbox only the Host's frames.

    The Host's ARP passes too, so the guest keeps resolving its address; nothing else does,
    IPv6 included. The Gateway and proxy filters run first and take their own frames.
    """
    tap, veth = KATA_TAP, KATA_VETH
    to_veth, to_tap = (
        f"action mirred egress redirect dev {veth}",
        f"action mirred egress redirect dev {tap}",
    )
    return [
        (tap, 20, f"protocol arp flower arp_tip {host_ip} {to_veth}"),
        (tap, 21, "protocol all matchall action drop"),
        (veth, 20, f"protocol ip flower src_ip {host_ip} {to_tap}"),
        (veth, 21, f"protocol arp flower arp_sip {host_ip} {to_tap}"),
        (veth, 22, "protocol all matchall action drop"),
    ]


def _kata_seal(host_ip: str) -> list[str]:
    return [
        f"tc filter add dev {device} ingress pref {pref} {spec}"
        for device, pref, spec in _kata_seal_filters(host_ip)
    ]


def _kata_unseal(host_ip: str) -> list[str]:
    """Remove the seal, then check that no drop filter is left: an unsealed sandbox that
    still had one would fail its framework phase for want of a network it was promised."""
    filters = _kata_seal_filters(host_ip)
    return [
        *(
            f"{{ tc filter del dev {device} ingress pref {pref} 2>/dev/null || true; }}"
            for device, pref, _ in filters
        ),
        *(
            f'test -z "$(tc filter show dev {device} ingress pref {pref})"'
            for device, pref, spec in filters
            if spec.endswith("action drop")
        ),
    ]


#: Run as root on the framework's Python in a Kata guest: ``del`` removes its default route,
#: ``add HOST`` restores it through the Host's address. Both are idempotent.
_KATA_ROUTE = """
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


async def _kata_route(client: GuestClient, host_ip: str, *, present: bool) -> None:
    """Remove a sealed Kata guest's default route, or restore it when it is unsealed.

    The tc filters keep a sealed sandbox in; this only makes what they drop fail at once,
    as it does on runc's internal network, which has no route off the Host. Dropped, a DNS
    lookup took 112 s to fail and a connection waited for its timeout.
    """
    action = "add" if present else "del"
    code, _, stderr, _ = await client.exec(
        [guestd.PYTHON["linux"], "-I", "-c", _KATA_ROUTE, action, host_ip], timeout_sec=30
    )
    if code != 0:
        raise ProviderStartError(f"could not {action} the Kata guest's route: {stderr.strip()}")


async def _netfilter_helper(container: str, commands: list[str], purpose: str) -> None:
    """Run commands with NET_ADMIN in the sandbox's network namespace on the Host, where the
    sandbox has none."""
    code, _, stderr = await _docker(
        "run", "--rm", "--network", f"container:{container}", "--cap-add", "NET_ADMIN",
        "--entrypoint", "sh", NETFILTER_IMAGE, "-c", " && ".join(commands),
        timeout=300,
    )  # fmt: skip
    if code != 0:
        raise ProviderStartError(f"could not {purpose}: {stderr.strip()}")


def _proxy_env(request: SandboxRequest) -> dict[str, str]:
    if not request.proxy_url:
        return {}
    proxy = _reachable(request.proxy_url) or ""
    if request.proxy_token:
        proxy = proxy.replace("://", f"://{quote(request.proxy_token, safe='')}:@", 1)
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": f"{HOST_ALIAS},localhost,127.0.0.1",
    }


def _verify_docker_gpu(
    selected: tuple[str, ...], observed: tuple[GpuDevice, ...]
) -> tuple[GpuDevice, ...]:
    actual = tuple(sorted(device.id for device in observed))
    expected = tuple(sorted(selected))
    if actual != expected:
        raise ProviderCapabilityError(
            f"Docker GPU identity mismatch: selected {expected}, observed {actual}"
        )
    return observed


class DockerSandbox(Sandbox):
    """One container, driven through the guest service."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        request: SandboxRequest,
        container: str,
        network: str | None,
        client: GuestClient,
        file_client: GuestClient,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        resource_lease: GpuLease | None = None,
        agent_user: str,
        agent_home: str,
        kata_host_ip: str | None = None,
    ) -> None:
        super().__init__(
            sandbox_id=sandbox_id,
            request=request,
            resolved_image=resolved_image,
            allocation=allocation,
            agent_user=agent_user,
            agent_home=agent_home,
            resource_lease=resource_lease,
        )
        self.container = container
        self.network = network
        #: The Host's address on the bridge of a sandbox in a Kata microVM, which its tc
        #: seal admits; None for a runc sandbox, which is sealed by detaching its bridge.
        self.kata_host_ip = kata_host_ip
        self._client = client
        self._file_client = file_client
        self._sealed = False
        self.state = SandboxState.READY

    def _as(self, identity: Identity) -> str | None:
        """The unix user for a role, or ``None`` to keep the container's default.

        Framework work stays as the image's own user, which for a sandbox image is root:
        installing the guest service and collecting artifacts have to work whatever the
        agent did to its own files.
        """
        return self.agent_user if identity is Identity.AGENT else None

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
        if (
            identity is Identity.AGENT
            and self._sealed
            and self.request.network.mode is NetworkMode.ALLOWLIST
        ):
            env = _proxy_env(self.request) | (env or {})
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
        """Attach the default bridge, which is where a route off the host comes from.

        The isolated network stays attached throughout, so the gateway is reachable in
        both states and its address never moves — an agent handed one URL at the start of
        an episode must not find it stale halfway through.

        A Kata VM sees no network attached after it boots, so it lives on the bridge and
        is unsealed by removing the tc filters that confine it instead.
        """
        if self.request.network.mode is NetworkMode.OPEN:
            return  # the task declared `open`; it is already on the bridge
        if self.kata_host_ip is not None:
            await _netfilter_helper(self.container, _kata_unseal(self.kata_host_ip), "open egress")
            await _kata_route(self._client, self.kata_host_ip, present=True)
            self._sealed = False
            return
        code, _, stderr = await _docker("network", "connect", "bridge", self.container)
        if code != 0 and "already exists" not in stderr:
            raise ProviderStartError(f"could not open egress: {stderr.strip()}")
        self._sealed = False

    async def close_egress(self) -> None:
        """Detach it again, leaving only the isolated network and the gateway on it."""
        if self.request.network.mode is NetworkMode.OPEN:
            return
        if self.kata_host_ip is not None:
            # Replaced rather than added, so sealing twice leaves one set of filters.
            commands = _kata_unseal(self.kata_host_ip) + _kata_seal(self.kata_host_ip)
            await _netfilter_helper(self.container, commands, "close egress")
            await _kata_route(self._client, self.kata_host_ip, present=False)
            self._sealed = True
            return
        code, _, stderr = await _docker("network", "disconnect", "bridge", self.container)
        if code != 0 and "is not connected" not in stderr:
            # Loud, because the alternative is an agent measured with a network it was
            # never meant to have and a lock file that says otherwise.
            raise ProviderStartError(f"could not close egress: {stderr.strip()}")
        self._sealed = True

    async def destroy(self) -> None:
        """Idempotent: teardown also runs on failure paths, sometimes twice."""
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED
        try:
            await asyncio.gather(
                self._client.close(), self._file_client.close(), return_exceptions=True
            )
            await _docker("rm", "-f", "-v", self.container, timeout=60)
            if self.network:
                await _docker("network", "rm", self.network, timeout=60)
        finally:
            self.release_resources()

    async def retain(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        await asyncio.gather(
            self._client.close(), self._file_client.close(), return_exceptions=True
        )
        gpu_devices = (
            self.allocation.gpu.provider_addresses if self.allocation.gpu is not None else ()
        )
        handle = f"{HANDLE_PREFIX}{self.container}"
        return RetainedSandbox(
            provider="docker",
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"ale sandbox destroy {handle}",
            gpu_devices=gpu_devices,
        )

    async def snapshot(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        """Stop the container and commit its filesystem as a labelled local image.

        A sandbox mounts no volumes, so the commit holds all of its files. The image keeps
        the container's labels and command; a snapshot holds no GPU.
        """
        await asyncio.gather(
            self._client.close(), self._file_client.close(), return_exceptions=True
        )
        code, _, stderr = await _docker("stop", "--time", "30", self.container, timeout=120)
        if code != 0:
            raise ProviderStartError(f"could not stop {self.container}: {stderr.strip()}")
        code, _, stderr = await _docker(
            "commit",
            "--change", f"LABEL {RETENTION_LABEL}=snapshot",
            "--change", f'LABEL {GPU_LABEL}=""',
            "--change", f"LABEL {PREPARED_LABEL}={self.request.prepared_image.prepared_identity}",
            self.container,
            f"{SNAPSHOT_REPOSITORY}:{self.sandbox_id}",
            timeout=1800,
        )  # fmt: skip
        if code != 0:
            raise ProviderStartError(f"could not commit {self.container}: {stderr.strip()}")
        handle = f"{SNAPSHOT_PREFIX}{self.sandbox_id}"
        return RetainedSandbox(
            provider="docker",
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"ale sandbox destroy {handle}",
        )


class DockerProvider(Provider):
    """Supplies container sandboxes."""

    name = "docker"

    def __init__(
        self,
        *,
        gpus: tuple[int, ...] = (),
        gpu_lock_dir: Path | None = None,
        runtime: Literal["runc", "kata"] = "runc",
    ) -> None:
        self.gpus = gpus
        self.gpu_lock_dir = gpu_lock_dir or cache_root() / "gpu-locks" / self.name
        #: ``runc`` leaves the runtime to the daemon's default; ``kata`` starts each
        #: sandbox in a microVM of its own.
        self.runtime = runtime
        # An image's conformance cannot change while it is pinned to a digest, and the
        # check costs a container start — so a sweep of ten thousand episodes pays for it
        # once rather than ten thousand times.
        self._checked: dict[str, list[str]] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            operating_systems=frozenset({OperatingSystem.LINUX}),
            gui=True,  # depends on the image; the GUI base image provides a desktop
            network_modes=frozenset({NetworkMode.BLOCK, NetworkMode.ALLOWLIST, NetworkMode.OPEN}),
            snapshot=True,
        )

    async def preflight(self) -> None:
        if shutil.which("docker") is None:
            raise ProviderStartError("docker is not installed — see `just doctor`")
        code, _, stderr = await _docker("info", "--format", "{{.ServerVersion}}", timeout=30)
        if code != 0:
            raise ProviderStartError(f"docker daemon unreachable: {stderr.strip()}")

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

    async def create(self, request: SandboxRequest) -> Sandbox:
        self.accepts(request)
        if request.image_kind is not ImageKind.CONTAINER:
            raise ProviderCapabilityError("DockerProvider requires a container image")
        kata = self.runtime == "kata"
        if kata and request.resources.gpus:
            raise ProviderCapabilityError(
                "Docker's GPU injection does not reach a Kata microVM; run GPU Tasks on runc"
            )
        lease: GpuLease | None = None
        selected: tuple[str, ...] = ()
        candidates: tuple[str, ...] = ()
        if request.resources.gpus:
            candidates = _eligible_gpus(await self._host_gpus(), self.gpus, request.resources)
        if request.snapshot is not None:
            resolved_image = await self._snapshot_image(request.snapshot, request)
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
        container = f"ale-{sandbox_id}"
        # A Kata VM takes its networks at boot and sees none attached later, so it stays
        # on the bridge and is confined by tc filters instead of an internal network.
        network = None if kata else await self._ensure_network(sandbox_id, request)
        memory_mb = request.resources.memory_mb
        argv = [
            "run", "-d", "--name", container,
            "--label", f"{LABEL}={request.episode_id}",
            "--label", f"{MANAGED_LABEL}=true",
            "--label", f"{ROLE_LABEL}={request.role.value}",
            "--label", f"{RETENTION_LABEL}={request.retention}",
            "--cpus", str(request.resources.cpus),
            "--memory", f"{_kata_memory_mb(memory_mb) if kata else memory_mb}m",
        ]  # fmt: skip
        isolated = request.network.mode is not NetworkMode.OPEN
        if kata:
            argv += ["--runtime", KATA_RUNTIME]
            if isolated:
                # Only to drop the guest's default route while it is sealed: what seals it
                # is outside the VM, where the capability does not reach.
                argv += ["--cap-add", "NET_ADMIN"]
        if network:
            argv += ["--network", network]
        for key, value in request.env.items():
            argv += ["-e", f"{key}={value}"]
        argv += ["-e", f"{CONTAINER_VARIABLE}={request.desktop_resolution}"]
        if request.gateway_url or request.proxy_url:
            host_ip = await self._bridge_host_ip(network)
            argv += ["--add-host", f"{HOST_ALIAS}:{host_ip}"]
            if request.gateway_url:
                argv += ["-e", f"ALE_GATEWAY_URL={_sandbox_gateway(request.gateway_url)}"]
            if request.proxy_url:
                request = request.model_copy(
                    update={"proxy_url": _reachable(request.proxy_url, host=host_ip)}
                )
        if request.resources.storage_mb is not None:
            argv += _storage_run_args(request.resources.storage_mb)
        if request.resources.gpus:
            used = await _managed_gpu_ids()
            available = tuple(key for key in candidates if key not in used)
            lease = GpuLease.acquire(available, request.resources.gpus, self.gpu_lock_dir)
            selected = lease.device_keys
            used_after_lock = await _managed_gpu_ids()
            if set(selected) & used_after_lock:
                lease.release()
                lease = None
                available = tuple(key for key in candidates if key not in used_after_lock)
                lease = GpuLease.acquire(available, request.resources.gpus, self.gpu_lock_dir)
                selected = lease.device_keys
            argv += _gpu_run_args(selected)
            argv += ["--label", f"{GPU_LABEL}={','.join(selected)}"]
        # The image's own command, always. A container lives exactly as long as its
        # command, and substituting one replaces whatever the image starts for itself —
        # for a desktop image, the entire graphical session. Images that have nothing to
        # do declare a command that waits; that is part of the image contract, so there
        # is nothing left here to decide.
        argv += [resolved_image.observed_ref]

        clients: list[GuestClient] = []
        try:
            code, _, stderr = await _docker(*argv, timeout=300)
            if code != 0:
                if request.resources.storage_mb is not None:
                    raise ProviderCapabilityError(
                        "Docker could not enforce the requested writable-layer quota "
                        f"({request.resources.storage_mb} MB): {stderr.strip()}"
                    )
                if request.resources.gpus:
                    raise ProviderCapabilityError(
                        "Docker could not inject the selected NVIDIA GPUs through its "
                        f"runtime/CDI path: {stderr.strip()}"
                    )
                raise ProviderStartError(f"could not start container: {stderr.strip()}")

            code, started, stderr = await _docker(
                "inspect", "--format", "{{.Image}} {{.HostConfig.Runtime}}", container, timeout=30
            )
            started_content, _, runtime = started.strip().partition(" ")
            if code != 0 or started_content != resolved_image.observed_identity:
                raise ProviderStartError(
                    "started container image identity does not match the resolved image: "
                    f"{stderr.strip() or started_content}"
                )

            runtime_identity = None
            if lease:
                code, version, stderr = await _docker(
                    "info", "--format", "{{.ServerVersion}}", timeout=30
                )
                if code != 0 or not version.strip():
                    raise ProviderCapabilityError(
                        f"could not identify the Docker GPU runtime: {stderr.strip()}"
                    )
                runtime_identity = f"docker-engine/{version.strip()}"

            kata_host_ip = None
            if kata:
                kata_host_ip = await self._wire_kata(container, request)
            else:
                await self._wire_host(container, network, request)
            agent_user = request.user
            client = await self._connect(container, agent_user)
            clients.append(client)
            if kata_host_ip and isolated:
                await _kata_route(client, kata_host_ip, present=False)
            agent_home, has_desktop = await self._guest_contract(client)
            await set_elevation(
                client, agent_user, sudo=request.sudo, operating_system=OperatingSystem.LINUX
            )
            await verify_identities(client, agent_user, sudo=request.sudo)
            observed_gpu = await self._container_gpus(client) if selected else ()
            if selected:
                _verify_docker_gpu(selected, observed_gpu)
            desktop: DesktopResolution | None = None
            if has_desktop:
                await self._await_desktop(client)
                desktop = await require_resolution(
                    client, request.desktop_resolution, container=True
                )
            file_client = await self._connect(container, agent_user)
            clients.append(file_client)
        except BaseException:
            await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
            with contextlib.suppress(BaseException):
                await asyncio.shield(_docker("rm", "-f", "-v", container))
            if network:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(_docker("network", "rm", network))
            if lease:
                lease.release()
            raise

        if lease:
            lease.attach()
        return DockerSandbox(
            sandbox_id=sandbox_id,
            request=request,
            container=container,
            network=network,
            client=client,
            file_client=file_client,
            resolved_image=resolved_image,
            allocation=ResourceAllocation(
                cpus=request.resources.cpus,
                memory_mb=request.resources.memory_mb,
                storage_mb=request.resources.storage_mb,
                gpu=(
                    GpuAllocation(
                        requested_count=request.resources.gpus,
                        provider_addresses=selected,
                        lease_keys=lease.device_keys,
                        observed_devices=observed_gpu,
                        runtime_identity=runtime_identity,
                    )
                    if lease
                    else None
                ),
                sudo=request.sudo,
                network_mode=request.network.mode,
                provider=self.name,
                runtime=runtime or None,
                desktop_resolution=desktop,
            ),
            resource_lease=lease,
            agent_user=agent_user,
            agent_home=agent_home,
            kata_host_ip=kata_host_ip,
        )

    async def _host_gpus(self) -> dict[int, GpuDevice]:
        if shutil.which("nvidia-smi") is None:
            raise ProviderCapabilityError("nvidia-smi is not installed on the Docker host")
        code, stdout, stderr = await _command(
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

    async def _ensure_network(self, sandbox_id: str, request: SandboxRequest) -> str | None:
        """Create the per-episode network.

        ``block`` and ``allowlist`` both use ``--internal``: the bridge has no route off
        the host, so the only reachable thing is the gateway. They differ in what the
        gateway will then forward, not in what the sandbox can dial — which is why
        allowlisting needs no privileged host rules and no tooling inside an image we do
        not control. ``open`` uses the default bridge.
        """
        if request.network.mode is NetworkMode.OPEN:
            return None
        network = f"ale-net-{sandbox_id}"
        code, _, stderr = await _docker("network", "create", "--internal", network, timeout=60)
        if code != 0:
            raise ProviderStartError(f"could not create isolated network: {stderr.strip()}")
        return network

    async def _wire_host(
        self,
        container: str,
        network: str | None,
        request: SandboxRequest,
        *,
        reattached: bool = False,
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
        if not routed and not network:
            return
        host_ip = await self._bridge_host_ip(network)
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
        if network:
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

    async def _wire_kata(
        self, container: str, request: SandboxRequest, *, reattached: bool = False
    ) -> str:
        """What ``_wire_host`` does, for a sandbox in a Kata microVM; returns the Host's
        address on its bridge.

        The same helper applies tc filters rather than netfilter rules: Kata moves frames
        between the VM and its veth with tc, so netfilter never sees them. A sandbox that
        is not ``open`` starts sealed, as an internal network leaves a runc one.
        """
        host_ip = await self._bridge_host_ip(None)
        routed = _sandbox_gateway(request.gateway_url) != request.gateway_url
        commands = _kata_filters(
            host_ip,
            gateway_port=_url_port(request.gateway_url) if routed else "",
            proxy_port=_url_port(request.proxy_url),
            isolated=request.network.mode is not NetworkMode.OPEN,
            reattached=reattached,
        )
        await _netfilter_helper(container, commands, "confine the Kata sandbox's network")
        return host_ip

    async def _bridge_host_ip(self, network: str | None) -> str:
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

    async def _connect(self, container: str, agent_user: str) -> GuestClient:
        transport = StdioTransport(
            [
                "docker",
                "exec",
                "-i",
                # The guest service runs as root whatever user the image itself declares.
                "--user",
                "0",
                "--env",
                f"ALE_AGENT_USER={agent_user}",
                container,
                # The framework's own Python, whatever the image has.
                guestd.PYTHON["linux"],
                str(GUESTD_DIR / "main.py"),
                "--stdio",
            ]
        )
        client = GuestClient(transport)
        try:
            await transport.start()
            await asyncio.wait_for(client.health(), timeout=60)
        except BaseException as exc:
            await client.close()
            if not isinstance(exc, Exception):
                raise
            raise ProviderStartError(f"guest service did not answer: {exc}") from exc
        return client

    async def list_retained(self) -> list[dict[str, object]]:
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
            raise ProviderStartError(f"could not list ALE sandboxes: {stderr.strip()}")
        handles = output.split()
        found = await self._list_snapshots()
        if not handles:
            return found
        code, raw, stderr = await _docker("inspect", *handles, timeout=30)
        if code != 0 and not _only_vanished(stderr):
            raise ProviderStartError(f"could not inspect ALE sandboxes: {stderr.strip()}")
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
                    "cleanup_command": f"ale sandbox destroy {handle}",
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
            raise ProviderStartError(f"could not list ALE snapshots: {stderr.strip()}")
        found: list[dict[str, object]] = []
        for tag in output.split():
            handle = f"{SNAPSHOT_PREFIX}{tag}"
            try:
                labels, identity = await _snapshot_record(handle)
            except ProviderCapabilityError:
                continue  # tagged by hand, not by ALE
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
                    "cleanup_command": f"ale sandbox destroy {handle}",
                }
            )
        return found

    async def _snapshot_image(self, handle: str, request: SandboxRequest) -> ResolvedImage:
        """The snapshot a Sandbox boots from, which must come from the Task's prepared image."""
        labels, identity = await _snapshot_record(handle)
        if labels.get(PREPARED_LABEL) != request.prepared_image.prepared_identity:
            raise ProviderCapabilityError(
                f"{handle!r} was not taken from the Task's prepared solver image"
            )
        return ResolvedImage(
            kind=ImageKind.CONTAINER,
            prepared_identity=request.prepared_image.prepared_identity,
            observed_identity=identity,
            observed_ref=identity,
        )

    async def destroy_retained(self, handle: str) -> None:
        if handle.startswith(SNAPSHOT_PREFIX):
            await _snapshot_record(handle)
            reference = _snapshot_reference(handle)
            # Not forced: a sandbox booted from the snapshot still uses its layers.
            code, _, stderr = await _docker("rmi", reference, timeout=120)
            if code != 0:
                raise ProviderStartError(f"could not destroy {handle}: {stderr.strip()}")
            return
        if not handle.startswith(HANDLE_PREFIX):
            raise ProviderCapabilityError(f"{handle!r} is not a Docker sandbox handle")
        container = handle.removeprefix(HANDLE_PREFIX)
        code, raw, stderr = await _docker("inspect", container, timeout=30)
        if code != 0:
            raise ProviderStartError(f"sandbox {handle!r} does not exist: {stderr.strip()}")
        record = json.loads(raw)[0]
        labels = record.get("Config", {}).get("Labels", {}) or {}
        if labels.get(MANAGED_LABEL) != "true" or labels.get(RETENTION_LABEL) != "keep":
            raise ProviderCapabilityError(f"{handle!r} is not an ALE-retained Docker sandbox")
        networks = tuple(
            name
            for name in (record.get("NetworkSettings", {}).get("Networks", {}) or {})
            if name.startswith("ale-net-")
        )
        code, _, stderr = await _docker("rm", "-f", "-v", container, timeout=60)
        if code != 0:
            raise ProviderStartError(f"could not destroy {handle}: {stderr.strip()}")
        for network in networks:
            await _docker("network", "rm", network, timeout=30)

    async def attach_retained(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
    ) -> DockerSandbox:
        """Reconnect to a running retained container without taking ownership of it."""
        if handle.startswith(SNAPSHOT_PREFIX) or not handle.startswith(HANDLE_PREFIX):
            raise ProviderCapabilityError(f"{handle!r} is not a Docker sandbox handle")
        container = handle.removeprefix(HANDLE_PREFIX)
        code, raw, stderr = await _docker("inspect", container, timeout=30)
        if code != 0:
            raise ProviderStartError(f"sandbox {handle!r} does not exist: {stderr.strip()}")
        record = json.loads(raw)[0]
        labels = record.get("Config", {}).get("Labels", {}) or {}
        role = labels.get(ROLE_LABEL, "")
        if (
            labels.get(MANAGED_LABEL) != "true"
            or labels.get(RETENTION_LABEL) != "keep"
            or role not in {SandboxRole.SOLVER.value, SandboxRole.SHARED.value}
        ):
            raise ProviderCapabilityError(f"{handle!r} is not a retained ALE solver sandbox")
        if not record.get("State", {}).get("Running"):
            raise ProviderStartError(f"retained sandbox {handle!r} is not running")

        if record.get("Image") != image.observed_identity:
            raise ProviderCapabilityError(f"{handle!r} does not use the expected solver image")
        networks = tuple(
            name
            for name in (record.get("NetworkSettings", {}).get("Networks", {}) or {})
            if name.startswith("ale-net-")
        )
        if len(networks) > 1:
            raise ProviderCapabilityError(f"{handle!r} has more than one ALE network")
        # The container, not this run's configuration, says how it was isolated.
        kata_host_ip = None
        if record.get("HostConfig", {}).get("Runtime") == KATA_RUNTIME:
            kata_host_ip = await self._wire_kata(container, request, reattached=True)
        else:
            await self._wire_host(
                container, networks[0] if networks else None, request, reattached=True
            )
        agent_user = request.user
        client = await self._connect(container, agent_user)
        try:
            agent_home, _ = await self._guest_contract(client)
            file_client = await self._connect(container, agent_user)
        except BaseException:
            await client.close()
            raise
        return DockerSandbox(
            sandbox_id=container.removeprefix("ale-"),
            request=request.model_copy(update={"role": SandboxRole(role)}),
            container=container,
            network=networks[0] if networks else None,
            client=client,
            file_client=file_client,
            resolved_image=image,
            allocation=allocation,
            agent_user=agent_user,
            agent_home=agent_home,
            kata_host_ip=kata_host_ip,
        )


def _snapshot_reference(handle: str) -> str:
    if not handle.startswith(SNAPSHOT_PREFIX):
        raise ProviderCapabilityError(f"{handle!r} is not a Docker snapshot handle")
    return f"{SNAPSHOT_REPOSITORY}:{handle.removeprefix(SNAPSHOT_PREFIX)}"


def _only_vanished(stderr: str) -> bool:
    """Whether ``docker inspect`` failed only for objects that no longer exist.

    Another run can destroy a retained sandbox between listing and inspecting it; the
    sandboxes it still found are on stdout.
    """
    errors = [line for line in stderr.splitlines() if line.strip()]
    return bool(errors) and all("no such object" in line.lower() for line in errors)


async def _snapshot_record(handle: str) -> tuple[dict[str, str], str]:
    """A snapshot's labels and image identity, once it is shown to be ALE's."""
    code, raw, stderr = await _docker("image", "inspect", _snapshot_reference(handle), timeout=30)
    if code != 0:
        raise ProviderStartError(f"snapshot {handle!r} does not exist: {stderr.strip()}")
    record = json.loads(raw)[0]
    labels = record.get("Config", {}).get("Labels", {}) or {}
    if labels.get(MANAGED_LABEL) != "true" or labels.get(RETENTION_LABEL) != "snapshot":
        raise ProviderCapabilityError(f"{handle!r} is not an ALE snapshot")
    return labels, str(record.get("Id", ""))


def _url_port(url: str | None) -> str:
    port = urlsplit(url).port if url else None
    return str(port) if port else ""


def _sandbox_gateway(url: str | None) -> str | None:
    """The Gateway as every container dials it: the Host's alias at the fixed port.

    A Gateway elsewhere than on this Host is left as it is.
    """
    if not url:
        return url
    parts = urlsplit(url)
    if parts.hostname not in {"0.0.0.0", "127.0.0.1", "localhost", "::"}:
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
