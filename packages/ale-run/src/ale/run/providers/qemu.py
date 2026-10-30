"""Virtual-machine sandboxes.

The second backend exists to prove the sandbox contract is real. If a task, a harness
and a verdict all behave the same against a container and against a VM, then what they
depend on is the contract rather than Docker — and the future OS roadmap (Windows,
macOS guests) becomes "swap the guest image" rather than "write another framework".

Three choices are worth stating, because each has a plausible alternative:

* **The VM is hosted by a container.** Rather than running ``qemu-system-x86_64`` on the
  host, a runner image holds it — the same one the previous framework used, which already
  solves the parts that are tedious and easy to get subtly wrong: device permissions,
  networking, signal handling, and a supervisor that dies with the guest rather than
  outliving it. It also means the host needs nothing installed but Docker and ``/dev/kvm``.
* **Overlays, not copies.** Each episode gets a qcow2 whose backing file is the golden
  image, so a pristine guest costs milliseconds and no disk. It is also the primitive a
  future ``reset()`` would use.
* **The guest service over a forwarded port.** One codebase, two transports: the same
  ``ale-guestd`` that Docker drives over exec-stdio is reached here over TCP.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    Capabilities,
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
from ale.core.taskspec import NetworkMode, OperatingSystem
from ale.run import ale_image, guestd
from ale.run.framework import framed_disk
from ale.run.gpu import GpuLease, normalize_pci_bdf, parse_nvidia_smi
from ale.run.images import (
    resolve_local_vm_fixture,
    resolve_prepared_vm_image,
    resolve_vm_image,
)
from ale.run.providers.privileges import verify_identities
from ale.run.sources import cache_root
from ale.run.transport import GuestClient, TcpTransport

__all__ = [
    "QemuProvider",
    "QemuSandbox",
]

#: Where the guest service listens inside the VM. Forwarded to an ephemeral host port.
GUEST_PORT = guestd.PORT
#: The sandbox's guest-service token, kept beside its overlay for reattachment.
GUEST_TOKEN_FILE = "guestd-token"
VM_STORAGE_OVERHEAD_MB = 1024

#: The image that hosts the virtual machine (built by images/runtimes/qemu-runner). It is
#: inherited from the previous framework, which had already worked out the device
#: permissions, guest bridge and signal handling a QEMU-in-a-container needs; ours is
#: published under our own namespace so a run does not depend on an image someone else
#: can move, and differs only in what its entrypoint checks and says.
RUNNER_IMAGE = os.environ.get("ALE_QEMU_RUNNER", ale_image("ale-qemu-runner"))

#: Where the runner expects the disk to boot, and the address it presents the host at.
#: Runner-side forwarding and the rewritten gateway URL both target the latter.
RUNNER_DISK = "/storage/data.qcow2"

#: Where the guest disk is published, and how it travels. A qcow2 is not a container
#: image, but shipping it as the single layer of one means it moves with the same
#: registry, the same credentials and the same `docker pull` everyone already has —
#: no second distribution channel and no extra tool to install.
GUEST_IMAGE = os.environ.get(
    "ALE_QEMU_GUEST_IMAGE", "ghcr.io/agentslastexam/ale-guest-ubuntu-desktop:24.04"
)
RUNNER_BASE = "/images/base.qcow2"

#: The interface inside the runner that the guest is attached to. Every packet the guest
#: sends arrives on it, which is what makes one rule enough to confine it.
GUEST_BRIDGE = "docker"

#: Fixed address through which the guest reaches Host services exposed by the runner.
HOST_IP = guestd.HOST_ADDRESS

#: A guest boots an operating system, so this is minutes rather than the seconds a
#: container takes.
BOOT_TIMEOUT_SEC = 300
_NVIDIA_QUERY = "uuid,name,pci.bus_id,driver_version"

MANAGED_LABEL = "ale.qemu.managed"
EPISODE_LABEL = "ale.qemu.episode"
ROLE_LABEL = "ale.qemu.role"
RETENTION_LABEL = "ale.qemu.retention"
GPU_LABEL = "ale.qemu.gpus"
STORAGE_LABEL = "ale.qemu.storage"
HANDLE_PREFIX = "qemu:"
SNAPSHOT_PREFIX = f"{HANDLE_PREFIX}snapshot:"
#: A snapshot is a directory holding its standalone disk and this record of where it came from.
SNAPSHOT_DISK = "disk.qcow2"
SNAPSHOT_RECORD = "snapshot.json"

#: The runner's nat chain that sends the fixed Gateway port to the port the Host serves the
#: Gateway on; a later run's Gateway replaces its rule.
GATEWAY_CHAIN = "ALE_GATEWAY"


@dataclass(frozen=True)
class _VfioGpu:
    bdf: str
    group: str


async def _managed_gpu_ids() -> set[str]:
    code, output, _ = await _run(
        "docker",
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


def _index_vfio_gpus(candidates: tuple[_VfioGpu, ...]) -> dict[str, _VfioGpu]:
    by_group: dict[str, _VfioGpu] = {}
    for gpu in candidates:
        if gpu.group in by_group:
            raise ProviderCapabilityError(
                f"configured GPUs {by_group[gpu.group].bdf} and {gpu.bdf} "
                f"share IOMMU group {gpu.group}"
            )
        by_group[gpu.group] = gpu
    return by_group


def _verify_qemu_gpu_count(
    requested: int, observed: tuple[GpuDevice, ...]
) -> tuple[GpuDevice, ...]:
    if len(observed) != requested:
        raise ProviderCapabilityError(
            f"QEMU GPU count mismatch: requested {requested}, observed {len(observed)}"
        )
    return observed


def _driver_name(device: Path) -> str | None:
    driver = device / "driver"
    return driver.resolve().name if driver.exists() or driver.is_symlink() else None


def _inspect_vfio_gpu(
    value: str,
    *,
    sysfs: Path = Path("/sys"),
    dev: Path = Path("/dev"),
) -> _VfioGpu:
    try:
        bdf = normalize_pci_bdf(value)
    except ValueError as exc:
        raise ProviderCapabilityError(str(exc)) from exc
    device = sysfs / "bus/pci/devices" / bdf
    if not device.exists():
        raise ProviderCapabilityError(f"configured PCI device {bdf} does not exist")
    if (device / "vendor").read_text().strip().lower() != "0x10de":
        raise ProviderCapabilityError(f"configured PCI device {bdf} is not NVIDIA")
    if not (device / "class").read_text().strip().lower().startswith("0x03"):
        raise ProviderCapabilityError(f"configured PCI device {bdf} is not a GPU")
    group_link = device / "iommu_group"
    if not group_link.exists():
        raise ProviderCapabilityError(f"configured GPU {bdf} has no IOMMU group")
    group = group_link.resolve().name
    if _driver_name(device) != "vfio-pci":
        raise ProviderCapabilityError(f"configured GPU {bdf} is not bound to vfio-pci")
    members = group_link.resolve() / "devices"
    for member in members.iterdir():
        driver = _driver_name(member.resolve())
        if driver not in {None, "vfio-pci"}:
            raise ProviderCapabilityError(
                f"IOMMU group {group} member {member.name} is bound to unsafe driver {driver}"
            )
    for node in (dev / "vfio/vfio", dev / "vfio" / group):
        if not node.exists() or not os.access(node, os.R_OK | os.W_OK):
            raise ProviderCapabilityError(f"VFIO node {node} is missing or inaccessible")
    return _VfioGpu(bdf=bdf, group=group)


def _proxy_env(request: SandboxRequest) -> dict[str, str]:
    if not request.proxy_url:
        return {}
    port = _port_of(request.proxy_url)
    proxy = f"http://{HOST_IP}:{port}"
    if request.proxy_token:
        proxy = proxy.replace("://", f"://{quote(request.proxy_token, safe='')}:@", 1)
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": f"{HOST_IP},localhost,127.0.0.1",
    }


async def _connect(host_port: int, token: str) -> GuestClient:
    transport = TcpTransport("127.0.0.1", host_port, token=token)
    client = GuestClient(transport)
    try:
        await transport.start(timeout_sec=BOOT_TIMEOUT_SEC)
    except BaseException:
        await client.close()
        raise
    return client


async def _run(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


_WINDOWS_ACL = """
$ErrorActionPreference = 'Stop'
$system = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$admins = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$users = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
$owner = $admins
if ($agent) {
    $owner = [System.Security.Principal.NTAccount]::new($agent).Translate(
        [System.Security.Principal.SecurityIdentifier])
}
$items = @(Get-Item -LiteralPath $target -Force) +
    @(Get-ChildItem -LiteralPath $target -Force -Recurse)
foreach ($item in $items) {
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { continue }
    $inherit = [System.Security.AccessControl.InheritanceFlags]::None
    if ($item.PSIsContainer) {
        $acl = [System.Security.AccessControl.DirectorySecurity]::new()
        $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $acl = [System.Security.AccessControl.FileSecurity]::new()
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($owner)
    foreach ($sid in @($system, $admins, $users)) {
        $rights = if ($sid -eq $users) { 'ReadAndExecute' } else { 'FullControl' }
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid, $rights, $inherit, 'None', 'Allow'))
    }
    if ($agent) {
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            $owner, 'Modify', $inherit, 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $item.FullName -AclObject $acl
}
"""


async def _reset_windows_acl(client: GuestClient, target: str, *, agent: str | None) -> None:
    """Replace inherited ACLs on an uploaded tree; the agent may modify its own uploads.

    guestd writes the tree with Windows' inherited ACLs, which say nothing about who the
    upload was for; this resets them the way Linux uploads get their modes and owner.
    """
    target_literal = target.replace("'", "''")
    agent_literal = (agent or "").replace("'", "''")
    command = f"$target = '{target_literal}'; $agent = '{agent_literal}';\n"
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command + _WINDOWS_ACL]
    code, _, stderr, _ = await client.exec(argv)
    if code != 0:
        raise ProviderStartError(f"could not set upload permissions for {target}: {stderr.strip()}")


class QemuSandbox(Sandbox):
    """One virtual machine, driven through the guest service."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        request: SandboxRequest,
        container: str,
        storage: Path,
        host_port: int,
        client: GuestClient,
        file_client: GuestClient,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        resource_lease: GpuLease | None = None,
        agent_user: str,
        agent_home: str,
        host_ip: str = "",
        snapshot_dir: Path | None = None,
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
        self.storage = storage
        self.host_port = host_port
        self.host_ip = host_ip
        self.snapshot_dir = snapshot_dir
        self._client = client
        self._file_client = file_client
        self._sealed = False
        self.state = SandboxState.READY

    def _as(self, identity: Identity) -> str | None:
        """Which account a call runs under.

        ``None`` means "whatever the guest service already is", which is root — the
        framework's own identity. Only the agent is stepped down, and it is stepped down
        by name because the name is the image's to choose, not ours.
        """
        return self.agent_user if identity is Identity.AGENT else None

    @property
    def gateway_url(self) -> str | None:
        """The gateway as this guest can reach it.

        The guest sits behind the runner container's own network, where the host appears
        at a fixed address — so the host's bind address is meaningless inside. Rewriting it
        here is why the gateway needs to know nothing about providers.
        """
        url = self.request.gateway_url
        if not url:
            return None
        parts = urlsplit(url)
        if parts.port is None:
            return url
        return parts._replace(netloc=f"{HOST_IP}:{SANDBOX_GATEWAY_PORT}").geturl()

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
        self,
        path: str,
        data: bytes,
        *,
        identity: Identity = Identity.FRAMEWORK,
    ) -> None:
        await self._client.write_file(str(path), data, run_as=self._as(identity))

    async def read_file(self, path: str, *, offset: int = 0, length: int | None = None) -> bytes:
        return await self._file_client.read_file(str(path), offset=offset, length=length)

    async def list_files(self, path: str) -> tuple[SandboxFile, ...]:
        return await self._file_client.list_files(path)

    async def file_digest(self, path: str, *, length: int) -> str:
        return await self._file_client.file_digest(path, length=length)

    async def upload_dir(
        self,
        source: str,
        target: str,
        *,
        identity: Identity = Identity.FRAMEWORK,
    ) -> None:
        run_as = self._as(identity)
        await self._client.upload_dir(source, str(target), run_as=run_as)
        if self.request.os is OperatingSystem.WINDOWS:
            await _reset_windows_acl(self._client, str(target), agent=run_as)

    async def screenshot(self) -> bytes:
        return await self._client.screenshot()

    async def inject_input(self, actions: Sequence[object]) -> int:
        payload = [
            action if isinstance(action, dict) else action.model_dump(mode="json")  # type: ignore[union-attr]
            for action in actions
        ]
        return await self._client.inject_input(payload)

    def _seal_rules(self) -> tuple[str, ...]:
        """Guest traffic the sealed runner forwards: replies, and the episode's own ports.

        Only the Gateway and proxy ports are DNATed to the host, but a guest could dial the
        host's address directly; every other forwarded packet is dropped.
        """
        ports = sorted(
            {
                port
                for url in (self.request.gateway_url, self.request.proxy_url)
                if (port := _port_of(url))
            }
        )
        allowed = (
            (f"-i {GUEST_BRIDGE} -d {self.host_ip} -p tcp -m multiport --dports {','.join(ports)}",)
            if ports
            else ()
        )
        return (
            f"-i {GUEST_BRIDGE} -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY",
            *allowed,
        )

    async def open_egress(self) -> None:
        """Lift the runner firewall for the framework's own phases."""
        for rule in (
            *(f"iptables -D FORWARD {match} -j ACCEPT" for match in self._seal_rules()),
            f"iptables -D FORWARD -i {GUEST_BRIDGE} -j DROP",
            f"iptables -D INPUT -i {GUEST_BRIDGE} -j DROP",
        ):
            await _run("docker", "exec", self.container, "sh", "-c", rule, timeout=60)
        self._sealed = False

    async def close_egress(self) -> None:
        """Restore the runner firewall before the agent starts."""
        if self.request.network.mode is NetworkMode.OPEN:
            return
        if not self.host_ip:
            raise ProviderStartError("cannot seal a VM whose host address is unknown")
        for rule in (
            f"iptables -I FORWARD 1 -i {GUEST_BRIDGE} -j DROP",
            *(f"iptables -I FORWARD 1 {match} -j ACCEPT" for match in self._seal_rules()),
            f"iptables -I INPUT 1 -i {GUEST_BRIDGE} -j DROP",
            f"iptables -I INPUT 1 -i {GUEST_BRIDGE} -p udp --dport 67 -j ACCEPT",
        ):
            code, _, stderr = await _run(
                "docker", "exec", self.container, "sh", "-c", rule, timeout=60
            )
            if code != 0:
                # Loud: the alternative is an agent measured with a network it was never
                # meant to have and a lock file that says otherwise.
                raise ProviderStartError(f"could not close egress: {stderr.strip()}")
        self._sealed = True

    async def save_image(self, destination: Path) -> None:
        """Shut down a Windows build VM and export a standalone disk."""
        if self.request.os is not OperatingSystem.WINDOWS:
            raise ProviderCapabilityError("image scripts require a Windows VM")
        await self._power_off()
        await self._export(destination)

    async def snapshot(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        """Shut the guest down and export its disk, base and overlay, as one standalone qcow2.

        Standalone, so the snapshot outlives the image cache it was booted from. Compressed
        with zstd, which made a Windows disk a third smaller and wrote it no slower. Its
        record holds the disk's content identity, taken once here rather than at every boot.
        """
        if self.snapshot_dir is None:
            raise ProviderCapabilityError(f"{self.container} has nowhere to save a snapshot")
        target = self.snapshot_dir / self.sandbox_id
        target.mkdir(parents=True)
        try:
            await self._power_off()
            disk = target / SNAPSHOT_DISK
            await self._export(disk, compressed=True)
            identity = await asyncio.to_thread(_disk_identity, disk)
            record = {
                "managed": True,
                "episode": self.request.episode_id,
                "role": self.request.role.value,
                "os": self.request.os.value,
                "prepared_identity": self.request.prepared_image.prepared_identity,
                "identity": identity,
            }
            (target / SNAPSHOT_RECORD).write_text(json.dumps(record, indent=2) + "\n")
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        handle = f"{SNAPSHOT_PREFIX}{self.sandbox_id}"
        return RetainedSandbox(
            provider="qemu",
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"ale sandbox destroy {handle}",
        )

    async def _power_off(self) -> None:
        """Shut the guest down cleanly and wait for its runner to exit."""
        if self.request.os is OperatingSystem.WINDOWS:
            command = ["shutdown.exe", "/s", "/t", "1"]
        else:
            # A Linux guest powers off without writing back its dirty pages: files written
            # seconds earlier came back empty. Its disk is flushed first.
            result = await self.exec(["sync"], timeout_sec=300)
            if not result.ok:
                raise ProviderStartError(f"could not flush {self.container}: {result.stderr}")
            command = ["systemctl", "poweroff", "--no-block"]
        result = await self.exec(command, timeout_sec=30)
        if not result.ok:
            raise ProviderStartError(f"could not shut down {self.container}: {result.stderr}")
        await asyncio.gather(
            self._client.close(), self._file_client.close(), return_exceptions=True
        )
        code, output, stderr = await _run("docker", "wait", self.container, timeout=300)
        if code != 0 or output.strip() != "0":
            raise ProviderStartError(
                f"{self.container} did not shut down cleanly: {stderr or output}"
            )

    async def _export(self, destination: Path, *, compressed: bool = False) -> None:
        """Write the stopped VM's disk as a standalone qcow2."""
        # The stopped overlay names the runner's mount; point it at the same host disk.
        overlay = self.storage / "data.qcow2"
        code, _, stderr = await _run(
            "qemu-img",
            "rebase",
            "-u",
            "-F",
            "qcow2",
            "-b",
            self.resolved_image.observed_ref,
            str(overlay),
            timeout=60,
        )
        if code != 0:
            raise ProviderStartError(f"could not resolve the VM's backing disk: {stderr}")
        compression = ("-c", "-o", "compression_type=zstd", "-m", "16", "-W") if compressed else ()
        code, _, stderr = await _run(
            "qemu-img",
            "convert",
            "-O",
            "qcow2",
            *compression,
            str(overlay),
            str(destination),
            timeout=1800,
        )
        if code != 0:
            raise ProviderStartError(f"could not export the VM's disk: {stderr}")

    async def destroy(self) -> None:
        """Idempotent: teardown also runs on failure paths, sometimes twice."""
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED

        try:
            with contextlib.suppress(Exception):
                await asyncio.gather(
                    self._client.close(), self._file_client.close(), return_exceptions=True
                )
            with contextlib.suppress(Exception):
                await _run("docker", "rm", "-f", self.container, timeout=60)
            # The overlay is this episode's entire mutable state.
            with contextlib.suppress(OSError):
                shutil.rmtree(self.storage, ignore_errors=True)
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
            provider="qemu",
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"ale sandbox destroy {handle}",
            gpu_devices=gpu_devices,
        )


class QemuProvider(Provider):
    """Supplies virtual-machine sandboxes from a golden qcow2."""

    name = "qemu"

    def __init__(
        self,
        *,
        image: Path | None = None,
        overlay_dir: Path | None = None,
        snapshot_dir: Path | None = None,
        image_cache_dir: Path | None = None,
        gpu_devices: tuple[str, ...] = (),
        gpu_lock_dir: Path | None = None,
        runner_image: str = RUNNER_IMAGE,
    ) -> None:
        self.fixture_image = image
        self.runner_image = runner_image
        # Host-side, and named for what it holds. It was `work_dir`, which is a retired
        # name: the workspace is the agent's home *inside* a sandbox, and reusing the word
        # for a directory on this machine is the collision the lexicon exists to prevent.
        self.overlay_dir = overlay_dir or Path.home() / ".cache/ale/qemu"
        self.snapshot_dir = snapshot_dir or self.overlay_dir.parent / "qemu-snapshots"
        self.image_cache_dir = image_cache_dir
        self.gpu_devices = gpu_devices
        self.gpu_lock_dir = gpu_lock_dir or cache_root() / "gpu-locks" / self.name

    def capabilities(self) -> Capabilities:
        return Capabilities(
            operating_systems=frozenset({OperatingSystem.LINUX, OperatingSystem.WINDOWS}),
            # Whether there is a screen is a property of the disk that was built, not of
            # this backend; a guest with no desktop refuses the request when asked.
            gui=True,
            network_modes=frozenset({NetworkMode.BLOCK, NetworkMode.ALLOWLIST, NetworkMode.OPEN}),
            reset=False,
            snapshot=True,
        )

    async def preflight(self) -> None:
        """Fail early and specifically, before anything is provisioned."""
        problems: list[str] = []

        if shutil.which("docker") is None:
            problems.append("docker is not on PATH; the runner image is what holds qemu")
        if shutil.which("qemu-img") is None:
            problems.append("qemu-img is not on PATH (apt install qemu-utils)")
        if not Path("/dev/kvm").exists():
            problems.append(
                "/dev/kvm is missing: this host has no hardware virtualisation, so the "
                "container backend is the usable one here"
            )
        elif not os.access("/dev/kvm", os.R_OK | os.W_OK):
            problems.append("/dev/kvm is present but not writable (add yourself to the kvm group)")
        if self.fixture_image is not None and not self.fixture_image.is_file():
            problems.append(
                f"no injected guest image at {self.fixture_image}; run ale prepare on a VM Task"
            )

        if problems:
            raise ProviderCapabilityError("; ".join(problems))

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if image.kind is not ImageKind.VM:
            raise ProviderCapabilityError("QemuProvider requires a VM image")
        if isinstance(image, ImageRef):
            if self.fixture_image is not None:
                prepared = await asyncio.to_thread(
                    resolve_local_vm_fixture,
                    image,
                    self.fixture_image,
                )
            else:
                prepared = await resolve_vm_image(image, cache_dir=self.image_cache_dir)
        else:
            prepared = image
        await resolve_prepared_vm_image(prepared)
        return prepared

    async def create(self, request: SandboxRequest) -> QemuSandbox:
        self.accepts(request)
        if request.image_kind is not ImageKind.VM:
            raise ProviderCapabilityError("QemuProvider requires a VM image")
        await self.preflight()
        lease: GpuLease | None = None
        selected: tuple[_VfioGpu, ...] = ()
        by_group: dict[str, _VfioGpu] = {}
        if request.resources.gpus:
            candidates = tuple(_inspect_vfio_gpu(value) for value in self.gpu_devices)
            if not candidates:
                raise ProviderCapabilityError("no QEMU VFIO GPU pool is configured")
            by_group = _index_vfio_gpus(candidates)
            used = await _managed_gpu_ids()
            by_group = {group: gpu for group, gpu in by_group.items() if gpu.bdf not in used}
        if request.snapshot is not None:
            resolved_image = self._snapshot_image(request.snapshot, request)
        else:
            resolved_image = await resolve_prepared_vm_image(request.prepared_image)
            resolved_image = await self._framed(resolved_image, request)
        base_image = Path(resolved_image.observed_ref)

        sandbox_id = f"{request.episode_id}-{uuid.uuid4().hex[:6]}"
        storage = self.overlay_dir / sandbox_id
        storage.mkdir(parents=True, exist_ok=True)
        guest_token = _issue_guest_token(storage)
        await self._make_overlay(
            storage / "data.qcow2",
            base_image,
            request.resources.storage_mb,
        )

        host_port = _free_port()
        if request.resources.gpus:
            lease = GpuLease.acquire(
                tuple(f"vfio-group:{group}" for group in by_group),
                request.resources.gpus,
                self.gpu_lock_dir,
            )
            groups = {key.removeprefix("vfio-group:") for key in lease.device_keys}
            selected = tuple(sorted((by_group[group] for group in groups), key=lambda gpu: gpu.bdf))
        try:
            container = await self._boot(
                request,
                storage,
                base_image,
                host_port,
                selected,
            )
        except BaseException:
            if lease:
                lease.release()
            shutil.rmtree(storage, ignore_errors=True)
            raise

        clients: list[GuestClient] = []
        try:
            host_ip = await self._wire_network(container, request)
            client = await _connect(host_port, guest_token)
            clients.append(client)
            agent_user, agent_home, has_desktop = await self._read_guest_contract(client, request)
            effective_storage_mb = await self._prepare_root_storage(
                client, request.resources.storage_mb, request.os
            )
            observed_gpu = await self._guest_gpus(client) if selected else ()
            if selected:
                _verify_qemu_gpu_count(request.resources.gpus, observed_gpu)
            if has_desktop:
                # The guest decided from its own configuration that it shows a desktop. Its
                # display manager and session start after the guest service is already
                # answering, so "the sandbox replied" is not "the sandbox has a screen".
                await self._await_desktop(client)
            if request.os is OperatingSystem.WINDOWS:
                await self._verify_windows_privileges(client, agent_user, request.sudo)
            else:
                await verify_identities(client, agent_user, sudo=request.sudo)
            file_client = await _connect(host_port, guest_token)
            clients.append(file_client)
        except BaseException:
            await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
            with contextlib.suppress(BaseException):
                await asyncio.shield(_run("docker", "rm", "-f", container, timeout=60))
            shutil.rmtree(storage, ignore_errors=True)
            if lease:
                lease.release()
            raise

        if lease:
            lease.attach()
        sandbox = QemuSandbox(
            sandbox_id=sandbox_id,
            request=request,
            container=container,
            storage=storage,
            host_port=host_port,
            client=client,
            file_client=file_client,
            resolved_image=resolved_image,
            allocation=ResourceAllocation(
                cpus=request.resources.cpus,
                memory_mb=request.resources.memory_mb,
                storage_mb=effective_storage_mb,
                gpu=(
                    GpuAllocation(
                        requested_count=request.resources.gpus,
                        provider_addresses=tuple(gpu.bdf for gpu in selected),
                        lease_keys=lease.device_keys,
                        observed_devices=observed_gpu,
                        runtime_identity=f"qemu-runner/{self.runner_image}",
                    )
                    if lease
                    else None
                ),
                sudo=request.sudo,
                network_mode=request.network.mode,
                provider=self.name,
            ),
            resource_lease=lease,
            agent_user=agent_user,
            agent_home=agent_home,
            host_ip=host_ip,
            snapshot_dir=self.snapshot_dir,
        )
        try:
            if request.network.mode is NetworkMode.OPEN:
                await sandbox.open_egress()
        except BaseException:
            await sandbox.destroy()
            raise
        return sandbox

    async def _make_overlay(
        self,
        overlay: Path,
        base_image: Path,
        requested_storage_mb: int | None = None,
    ) -> None:
        """A copy-on-write clone of the golden image; the golden image is never written.

        The backing path is the one the *runner* will see, not the one on this host, and
        ``-u`` is what lets it be written without being opened here. Recording the host's
        path instead is the mistake that costs a boot: qemu inside the container follows
        it, finds nothing, and refuses the disk.
        """
        code, output, stderr = await _run(
            "qemu-img", "info", "--output=json", str(base_image), timeout=60
        )
        try:
            virtual_size = int(json.loads(output)["virtual-size"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderStartError(
                f"could not inspect the prepared VM size: {stderr.strip() or error}"
            ) from error
        if code != 0 or virtual_size <= 0:
            raise ProviderStartError(
                f"could not inspect the prepared VM size: {stderr.strip() or output.strip()}"
            )

        if requested_storage_mb is not None:
            requested_size = (requested_storage_mb + VM_STORAGE_OVERHEAD_MB) * 1024 * 1024
            virtual_size = max(virtual_size, requested_size)

        code, _, stderr = await _run(
            "qemu-img", "create", "-u",
            "-f", "qcow2",
            "-F", "qcow2",
            "-b", RUNNER_BASE,
            str(overlay),
            str(virtual_size),
            timeout=60,
        )  # fmt: skip
        if code != 0:
            raise ProviderStartError(f"could not create the episode overlay: {stderr.strip()}")

    async def _boot(
        self,
        request: SandboxRequest,
        storage: Path,
        base_image: Path,
        host_port: int,
        gpus: tuple[_VfioGpu, ...] = (),
    ) -> str:
        """Start the runner, which starts the machine.

        The golden image is mounted read-only beside the overlay that backs onto it, so
        one image serves every concurrent episode and none of them can write to it.
        """
        name = f"ale-qemu-{uuid.uuid4().hex[:10]}"
        argv = [
            "docker", "run", "--detach", "--name", name,
            "--label", f"{MANAGED_LABEL}=true",
            "--label", f"{EPISODE_LABEL}={request.episode_id}",
            "--label", f"{ROLE_LABEL}={request.role.value}",
            "--label", f"{RETENTION_LABEL}={request.retention}",
            "--label", f"{STORAGE_LABEL}={storage.resolve()}",
            "--device=/dev/kvm",
            # The runner builds and confines the guest network itself, which needs this
            # capability. Enforcement stays outside the guest on every operating system.
            "--cap-add", "NET_ADMIN",
            "--shm-size", "1g",
            "--mount",
            f"type=bind,src={base_image.resolve()},dst={RUNNER_BASE},readonly",
            "--mount", f"type=bind,src={storage.resolve()},dst=/storage",
            "--publish", f"127.0.0.1:{host_port}:{GUEST_PORT}",
            "--env", f"RAM_SIZE={request.resources.memory_mb}M",
            "--env", f"CPU_CORES={request.resources.cpus}",
            "--env", "CPU_MODEL=host",
            # Match guest forwarding even when the outer container gets another address.
            "--env", "VM_NET_IP=172.30.0.2",
            # Hypervisor enlightenments are for Windows guests; a Linux guest boots
            # faster without them.
            "--env", f"HV={'Y' if request.os is OperatingSystem.WINDOWS else 'N'}",
        ]  # fmt: skip
        if request.os is OperatingSystem.WINDOWS and request.sudo:
            argv += ["--env", "ARGUMENTS=-smbios type=1,serial=ale-sudo"]
        if gpus:
            argv += ["--label", f"{GPU_LABEL}={','.join(gpu.bdf for gpu in gpus)}"]
            argv += ["--device=/dev/vfio/vfio", "--ulimit", "memlock=-1:-1"]
            for group in sorted({gpu.group for gpu in gpus}):
                argv += [f"--device=/dev/vfio/{group}"]
            argv += ["--env", f"ALE_QEMU_VFIO_DEVICES={','.join(gpu.bdf for gpu in gpus)}"]
        argv += [self.runner_image]

        code, stdout, stderr = await _run(*argv, timeout=180)
        if code != 0:
            raise ProviderStartError(f"could not start the qemu runner: {stderr.strip()}")
        container = stdout.strip() and name

        try:
            # A runner that rejects the disk or the device exits at once; saying so now
            # beats waiting out the boot timeout on a machine that never started.
            await asyncio.sleep(1.0)
            alive, running, _ = await _run(
                "docker", "inspect", "-f", "{{.State.Running}}", container, timeout=30
            )
            if alive != 0 or running.strip() != "true":
                _, logs, _ = await _run("docker", "logs", "--tail", "20", container, timeout=30)
                raise ProviderStartError(f"the qemu runner exited immediately: {logs.strip()}")
            return container
        except BaseException:
            with contextlib.suppress(BaseException):
                await asyncio.shield(_run("docker", "rm", "-f", container, timeout=60))
            raise

    async def _framed(self, resolved: ResolvedImage, request: SandboxRequest) -> ResolvedImage:
        """Install the engine's framework into a disk that was not prepared with it.

        Task preparation frames the disk already; this covers a disk handed straight to
        the Provider, so any VM disk can serve as a sandbox, as any image can under Docker.
        """
        framed, _ = await framed_disk(
            request.prepared_image,
            operating_system=request.os,
            user=request.user,
            sudo=request.sudo,
        )
        if framed == request.prepared_image:
            return resolved
        return resolved.model_copy(
            update={
                "observed_identity": framed.prepared_identity,
                "observed_ref": framed.runtime_ref,
            }
        )

    async def _guest_gpus(self, client: GuestClient) -> tuple[GpuDevice, ...]:
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
                f"QEMU guest nvidia-smi failed before setup: {stderr.strip()}"
            )
        return parse_nvidia_smi(stdout)

    async def _root_capacity_mb(
        self, client: GuestClient, operating_system: OperatingSystem
    ) -> int:
        root = "C:\\" if operating_system is OperatingSystem.WINDOWS else "/"
        try:
            total, _, _ = await client.disk_usage(root)
        except Exception as exc:
            raise ProviderStartError(
                f"could not observe VM root filesystem capacity: {exc}"
            ) from exc
        return total // (1024 * 1024)

    async def _prepare_root_storage(
        self,
        client: GuestClient,
        requested_storage_mb: int | None,
        operating_system: OperatingSystem,
    ) -> int:
        capacity = await self._root_capacity_mb(client, operating_system)
        if requested_storage_mb is None or capacity >= requested_storage_mb:
            return capacity

        commands = (
            (
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$s=(Get-PartitionSupportedSize -DriveLetter C); "
                    "Resize-Partition -DriveLetter C -Size $s.SizeMax",
                ],
            )
            if operating_system is OperatingSystem.WINDOWS
            else (
                ["systemd-repart", "--dry-run=no"],
                ["/usr/lib/systemd/systemd-growfs", "/"],
            )
        )
        for argv in commands:
            code, stdout, stderr, _ = await client.exec(argv, timeout_sec=120)
            if code != 0:
                raise ProviderStartError(
                    f"could not expand VM root storage with {argv[0]}: "
                    f"{stderr.strip() or stdout.strip()}"
                )

        capacity = await self._root_capacity_mb(client, operating_system)
        if capacity < requested_storage_mb:
            raise ProviderCapabilityError(
                f"VM root filesystem provides {capacity} MB after expansion, "
                f"task requests {requested_storage_mb} MB"
            )
        return capacity

    async def _wire_network(self, container: str, request: SandboxRequest) -> str:
        """Route the gateway into the guest, and report where the host is.

        Only the route. What *confines* the guest is installed by ``close_egress`` when
        the agent's phase begins, because until then the sandbox is the framework's to
        prepare — a task's setup may fetch what it needs, and the agent's own CLI may have
        to be installed, neither of which is the thing being measured.

        The guest's single permitted address is the runner, and the runner sends the fixed
        Gateway port on to the port the Host serves the Gateway on. Doing it here rather than
        in the guest means the host's address is discovered at run time instead of baked
        into a disk, and a later run that reattaches routes the same address to its own
        Gateway.
        """
        host_ip = await _runner_host_ip(container)
        rules: list[str] = []
        if gateway_port := _port_of(request.gateway_url):
            # The guest dials the fixed port; a later run's Gateway replaces the target.
            rules += [
                f"iptables -t nat -N {GATEWAY_CHAIN} || iptables -t nat -F {GATEWAY_CHAIN}",
                f"iptables -t nat -C PREROUTING -j {GATEWAY_CHAIN} || "
                f"iptables -t nat -A PREROUTING -j {GATEWAY_CHAIN}",
                f"iptables -t nat -A {GATEWAY_CHAIN} -i {GUEST_BRIDGE} -p tcp -d {HOST_IP} "
                f"--dport {SANDBOX_GATEWAY_PORT} -j DNAT --to-destination {host_ip}:{gateway_port}",
                f"iptables -t nat -A POSTROUTING -p tcp -d {host_ip} --dport {gateway_port} "
                "-j MASQUERADE",
            ]
        if request.network.mode is NetworkMode.ALLOWLIST and (port := _port_of(request.proxy_url)):
            rules += [
                f"iptables -t nat -A PREROUTING -i {GUEST_BRIDGE} -p tcp -d {HOST_IP} "
                f"--dport {port} -j DNAT --to-destination {host_ip}:{port}",
                f"iptables -t nat -A POSTROUTING -p tcp -d {host_ip} --dport {port} -j MASQUERADE",
            ]

        for rule in rules:
            code, _, stderr = await _run("docker", "exec", container, "sh", "-c", rule, timeout=60)
            if code != 0:
                raise ProviderStartError(f"could not route the gateway: {stderr.strip()}")
        return host_ip

    async def _await_desktop(self, client: GuestClient, timeout_sec: float = 300) -> None:
        """Block until the screen can be captured *and* something has been drawn on it.

        Capturing alone is not enough here, and that is the difference from the container
        backend. There, one command starts the X server and the session together, so a
        screenshot succeeds only once there is a session to photograph. A virtual machine
        boots them apart: X is listening seconds before the desktop paints, and a
        screenshot taken in between succeeds and returns a black rectangle. The first
        desktop guest reported "ready in 19s" and handed back exactly that.

        A blank screen is an unambiguous signal *at this moment* — nothing task-specific
        has run yet, so the only thing that could be on screen is the desktop's own
        wallpaper. It would not be a safe test later, once a task can legitimately fill
        the screen with one colour, which is why it lives here and not in ``screenshot``.

        Longer deadline than the container backend allows, because a virtual machine is
        booting an operating system rather than starting a process tree.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        last = "no screenshot was taken"
        while loop.time() < deadline:
            try:
                png = await client.screenshot()
            except Exception as exc:
                last = str(exc)
                await asyncio.sleep(2)
                continue
            if not _is_blank(png):
                return
            last = "the screen is up but nothing has been drawn on it"
            await asyncio.sleep(2)
        raise ProviderStartError(
            f"this guest declares a desktop but none was ready within {timeout_sec:g}s ({last})"
        )

    async def _read_guest_contract(
        self, client: GuestClient, request: SandboxRequest
    ) -> tuple[str, str, bool]:
        health = await client.health()
        try:
            observed_os = OperatingSystem(str(health["os"]))
        except (KeyError, ValueError) as exc:
            raise ProviderCapabilityError("guestd health omitted a valid os") from exc
        if observed_os is not request.os:
            raise ProviderCapabilityError(
                f"task requests {request.os} but the VM image runs {observed_os}"
            )
        user = str(health.get("agent_user") or "")
        home = str(health.get("agent_home") or "")
        same_account = (
            user.casefold() == request.user.casefold()
            if observed_os is OperatingSystem.WINDOWS
            else user == request.user
        )
        if not same_account:
            raise ProviderCapabilityError(
                f"the VM disk was prepared for agent account {user!r}, but the Task names "
                f"{request.user!r}"
            )
        if not user or not home or not await client.exists(home):
            raise ProviderCapabilityError(
                f"guestd names {user!r} at {home!r} as the agent account, but that home is absent"
            )
        return user, home, bool(health.get("gui"))

    async def _verify_windows_privileges(
        self, client: GuestClient, agent_user: str, sudo: bool
    ) -> None:
        """Check actual tokens, including the default limited agent, before any Task code."""
        command = [
            "python.exe",
            "-c",
            "import ctypes; print(int(bool(ctypes.windll.shell32.IsUserAnAdmin())))",
        ]
        for user, expected in ((None, True), (agent_user, sudo)):
            code, output, stderr, timed_out = await client.exec(
                command, run_as=user, timeout_sec=30
            )
            if code != 0 or timed_out or output.strip() != str(int(expected)):
                role = "framework" if user is None else "agent"
                required = "Administrator" if expected else "limited"
                raise ProviderCapabilityError(
                    f"Windows {role} must run with a {required} token; rebuild the image "
                    f"with the current ALE framework and UAC enabled: {stderr.strip()}"
                )

    async def list_retained(self) -> list[dict[str, object]]:
        code, output, stderr = await _run(
            "docker",
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
            raise ProviderStartError(f"could not list retained QEMU sandboxes: {stderr.strip()}")
        containers = output.split()
        found = self._list_snapshots()
        if not containers:
            return found
        code, raw, stderr = await _run("docker", "inspect", *containers, timeout=30)
        if code != 0:
            raise ProviderStartError(f"could not inspect retained QEMU sandboxes: {stderr.strip()}")
        for record in json.loads(raw):
            labels = record.get("Config", {}).get("Labels", {}) or {}
            container = record.get("Name", "").removeprefix("/")
            found.append(
                {
                    "provider": "qemu",
                    "handle": f"{HANDLE_PREFIX}{container}",
                    "episode": labels.get(EPISODE_LABEL, ""),
                    "role": labels.get(ROLE_LABEL, ""),
                    "image": record.get("Image", ""),
                    "gpus": tuple(filter(None, labels.get(GPU_LABEL, "").split(","))),
                    "running": bool(record.get("State", {}).get("Running")),
                    "cleanup_command": f"ale sandbox destroy {HANDLE_PREFIX}{container}",
                }
            )
        return sorted(found, key=lambda item: str(item["handle"]))

    def _list_snapshots(self) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if not self.snapshot_dir.is_dir():
            return found
        for directory in sorted(self.snapshot_dir.iterdir()):
            handle = f"{SNAPSHOT_PREFIX}{directory.name}"
            try:
                record = self._snapshot_record(handle)
            except (ProviderCapabilityError, ProviderStartError):
                continue  # not a snapshot ALE saved, or one still being saved
            found.append(
                {
                    "provider": "qemu",
                    "handle": handle,
                    "episode": record.get("episode", ""),
                    "role": record.get("role", ""),
                    "image": record.get("identity", ""),
                    "gpus": (),
                    "running": False,
                    "cleanup_command": f"ale sandbox destroy {handle}",
                }
            )
        return found

    def _snapshot_record(self, handle: str) -> dict[str, str]:
        """A snapshot's record, once its directory is shown to hold one ALE saved."""
        name = handle.removeprefix(SNAPSHOT_PREFIX)
        if not handle.startswith(SNAPSHOT_PREFIX) or not name or "/" in name or name[0] == ".":
            raise ProviderCapabilityError(f"{handle!r} is not a QEMU snapshot handle")
        directory = self.snapshot_dir / name
        try:
            record = json.loads((directory / SNAPSHOT_RECORD).read_text())
        except FileNotFoundError as exc:
            raise ProviderStartError(f"snapshot {handle!r} does not exist") from exc
        except (OSError, ValueError) as exc:
            raise ProviderCapabilityError(f"{handle!r} has no valid snapshot record") from exc
        if (
            not isinstance(record, dict)
            or record.get("managed") is not True
            or not (directory / SNAPSHOT_DISK).is_file()
        ):
            raise ProviderCapabilityError(f"{handle!r} is not an ALE snapshot")
        return record

    def _snapshot_image(self, handle: str, request: SandboxRequest) -> ResolvedImage:
        """The snapshot a VM boots from, which must come from the Task's prepared image."""
        record = self._snapshot_record(handle)
        if record.get("prepared_identity") != request.prepared_image.prepared_identity:
            raise ProviderCapabilityError(
                f"{handle!r} was not taken from the Task's prepared solver image"
            )
        if record.get("os") != request.os.value:
            raise ProviderCapabilityError(f"{handle!r} holds a {record.get('os')} guest")
        return ResolvedImage(
            kind=ImageKind.VM,
            prepared_identity=request.prepared_image.prepared_identity,
            observed_identity=str(record.get("identity")),
            observed_ref=str(
                self.snapshot_dir / handle.removeprefix(SNAPSHOT_PREFIX) / SNAPSHOT_DISK
            ),
        )

    async def _destroy_snapshot(self, handle: str) -> None:
        self._snapshot_record(handle)
        directory = self.snapshot_dir / handle.removeprefix(SNAPSHOT_PREFIX)
        disk = str((directory / SNAPSHOT_DISK).resolve())
        code, output, stderr = await _run(
            "docker", "ps", "-a", "-q", "--filter", f"label={MANAGED_LABEL}=true", timeout=30
        )
        if code != 0:
            raise ProviderStartError(f"could not list QEMU sandboxes: {stderr.strip()}")
        if containers := output.split():
            code, raw, stderr = await _run("docker", "inspect", *containers, timeout=30)
            if code != 0:
                raise ProviderStartError(f"could not inspect QEMU sandboxes: {stderr.strip()}")
            for record in json.loads(raw):
                if any(
                    mount.get("Destination") == RUNNER_BASE and mount.get("Source") == disk
                    for mount in record.get("Mounts", ())
                ):
                    name = record.get("Name", "").removeprefix("/")
                    raise ProviderStartError(
                        f"could not destroy {handle}: {HANDLE_PREFIX}{name} runs on it"
                    )
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise ProviderStartError(f"could not destroy {handle}: {exc}") from exc

    async def destroy_retained(self, handle: str) -> None:
        if handle.startswith(SNAPSHOT_PREFIX):
            await self._destroy_snapshot(handle)
            return
        if not handle.startswith(HANDLE_PREFIX):
            raise ProviderCapabilityError(f"{handle!r} is not a QEMU sandbox handle")
        container = handle.removeprefix(HANDLE_PREFIX)
        code, raw, stderr = await _run("docker", "inspect", container, timeout=30)
        if code != 0:
            raise ProviderStartError(f"sandbox {handle!r} does not exist: {stderr.strip()}")
        record = json.loads(raw)[0]
        labels = record.get("Config", {}).get("Labels", {}) or {}
        if labels.get(MANAGED_LABEL) != "true" or labels.get(RETENTION_LABEL) != "keep":
            raise ProviderCapabilityError(f"{handle!r} is not an ALE-retained QEMU sandbox")
        storage = labels.get(STORAGE_LABEL, "")
        mounted_storage = {
            mount.get("Source", "")
            for mount in record.get("Mounts", ())
            if mount.get("Type") == "bind" and mount.get("Destination") == "/storage"
        }
        if not storage or storage not in mounted_storage:
            raise ProviderCapabilityError(f"{handle!r} has no valid ALE-owned QEMU overlay")
        code, _, stderr = await _run("docker", "rm", "-f", container, timeout=60)
        if code != 0:
            raise ProviderStartError(f"could not destroy {handle}: {stderr.strip()}")
        try:
            shutil.rmtree(storage)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ProviderStartError(
                f"destroyed {handle} but could not remove its overlay {storage}: {exc}"
            ) from exc

    async def attach_retained(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
    ) -> QemuSandbox:
        """Reconnect to a running retained VM without taking ownership of it."""
        if handle.startswith(SNAPSHOT_PREFIX) or not handle.startswith(HANDLE_PREFIX):
            raise ProviderCapabilityError(f"{handle!r} is not a QEMU sandbox handle")
        container = handle.removeprefix(HANDLE_PREFIX)
        code, raw, stderr = await _run("docker", "inspect", container, timeout=30)
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
        storage = labels.get(STORAGE_LABEL, "")
        mounted_storage = {
            mount.get("Source", "")
            for mount in record.get("Mounts", ())
            if mount.get("Type") == "bind" and mount.get("Destination") == "/storage"
        }
        if not storage or storage not in mounted_storage:
            raise ProviderCapabilityError(f"{handle!r} has no valid ALE-owned QEMU overlay")
        base = {
            mount.get("Source", "")
            for mount in record.get("Mounts", ())
            if mount.get("Destination") == RUNNER_BASE
        }
        if base != {str(Path(image.observed_ref).resolve())}:
            raise ProviderCapabilityError(f"{handle!r} does not run the expected solver image")
        ports = record.get("NetworkSettings", {}).get("Ports", {}).get(f"{GUEST_PORT}/tcp") or []
        try:
            host_port = int(ports[0]["HostPort"])
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ProviderCapabilityError(f"{handle!r} has no guest service port") from exc

        token = _read_guest_token(Path(storage))
        client = await _connect(host_port, token)
        attached_request = request.model_copy(update={"role": SandboxRole(role)})
        try:
            agent_user, agent_home, _ = await self._read_guest_contract(client, attached_request)
            # A later run reaches its own Gateway, so its port is routed in as well.
            host_ip = await self._wire_network(container, attached_request)
            file_client = await _connect(host_port, token)
        except BaseException:
            await client.close()
            raise
        return QemuSandbox(
            sandbox_id=container.removeprefix("ale-qemu-"),
            request=attached_request,
            container=container,
            storage=Path(storage),
            host_port=host_port,
            client=client,
            file_client=file_client,
            resolved_image=image,
            allocation=allocation,
            agent_user=agent_user,
            agent_home=agent_home,
            host_ip=host_ip,
            snapshot_dir=self.snapshot_dir,
        )


def _is_blank(png: bytes) -> bool:
    """Whether an image is a single flat colour, judged from the PNG itself.

    Decoded here rather than in the guest, and without an imaging library. Asking the
    guest meant running Python in a process that has no display — the guest service finds
    one when *it* needs one, which does nothing for a command run beside it — so the check
    reported "nothing drawn" forever against a desktop that had painted twenty seconds in.
    The bytes are already on this side; nothing else was needed.

    A flat image compresses to almost nothing: every scanline is one filter byte followed
    by the same pixel repeated, so the decompressed stream holds only a couple of distinct
    values. Anything real holds hundreds.
    """
    data = bytearray()
    offset = 8  # past the signature
    while offset + 8 <= len(png):
        length = int.from_bytes(png[offset : offset + 4], "big")
        kind = png[offset + 4 : offset + 8]
        if kind == b"IDAT":
            data += png[offset + 8 : offset + 8 + length]
        elif kind == b"IEND":
            break
        offset += 12 + length
    if not data:
        return False  # unreadable is not "blank"; let the caller keep what it was given
    try:
        return len(set(zlib.decompress(bytes(data)))) <= 2
    except zlib.error:
        return False


async def _runner_host_ip(container: str) -> str:
    """The host's address as the runner container routes to it."""
    code, route, _ = await _run(
        "docker", "exec", container, "sh", "-c",
        "ip route | awk '/^default/{print $3}'", timeout=60,
    )  # fmt: skip
    host_ip = route.strip()
    if code != 0 or not host_ip:
        raise ProviderStartError("could not find the host's address from inside the runner")
    return host_ip


def _port_of(url: str | None) -> str:
    """The port a gateway URL names, or empty when a run has no gateway at all."""
    if not url:
        return ""
    _, _, rest = url.partition("://")
    host_port, _, _ = rest.partition("/")
    _, _, port = host_port.partition(":")
    return port


def _disk_identity(disk: Path) -> str:
    with disk.open("rb") as handle:
        return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()


def _issue_guest_token(storage: Path) -> str:
    """A fresh token the booted guest's first connection claims; readable only by this account."""
    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(storage / GUEST_TOKEN_FILE, flags, 0o600), "w") as handle:
        handle.write(token)
    return token


def _read_guest_token(storage: Path) -> str:
    try:
        return (storage / GUEST_TOKEN_FILE).read_text().strip()
    except FileNotFoundError as exc:
        raise ProviderCapabilityError(
            f"retained overlay {storage} has no guest service token"
        ) from exc


def _free_port() -> int:
    """Ask the OS for a port, then hand it to the runner.

    There is a race between closing this socket and the runner binding it, which is why
    the port is ephemeral per episode rather than fixed: a collision costs one retry of
    one episode instead of making concurrent runs impossible.
    """
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
