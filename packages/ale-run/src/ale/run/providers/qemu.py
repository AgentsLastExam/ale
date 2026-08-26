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
import json
import os
import shutil
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal
from urllib.parse import quote

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
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
    SandboxRequest,
    SandboxRole,
    SandboxState,
)
from ale.core.taskspec import NetworkMode, OperatingSystem
from ale.run.gpu import GpuLease, normalize_pci_bdf, parse_nvidia_smi
from ale.run.images import (
    resolve_local_vm_fixture,
    resolve_prepared_vm_image,
    resolve_vm_image,
)
from ale.run.sources import cache_root
from ale.run.transport import GuestClient, TcpTransport

__all__ = [
    "QemuProvider",
    "QemuSandbox",
    "attach_retained",
    "destroy_retained",
    "list_retained",
]

#: Where the guest service listens inside the VM. Forwarded to an ephemeral host port.
GUEST_PORT = 7411
VM_STORAGE_OVERHEAD_MB = 1024

#: The image that hosts the virtual machine (built by images/runtimes/qemu-runner). It is
#: inherited from the previous framework, which had already worked out the device
#: permissions, guest bridge and signal handling a QEMU-in-a-container needs; ours is
#: published under our own namespace so a run does not depend on an image someone else
#: can move, and differs only in what its entrypoint checks and says.
RUNNER_IMAGE = os.environ.get("ALE_QEMU_RUNNER", "ghcr.io/agentslastexam/ale-qemu-runner:0.1.0")

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
HOST_IP = "172.30.0.1"

#: Where the guest writes the same facts a container image puts in labels: which account
#: is the agent's, and whether there is a screen. A disk image has nowhere to hang a label,
#: so the contract of docs/specs/sandbox-image.md is carried in a file instead.
DEFAULT_AGENT_USER = "user"

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


async def list_retained() -> list[dict[str, object]]:
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
    if not containers:
        return []
    code, raw, stderr = await _run("docker", "inspect", *containers, timeout=30)
    if code != 0:
        raise ProviderStartError(f"could not inspect retained QEMU sandboxes: {stderr.strip()}")
    found: list[dict[str, object]] = []
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


async def destroy_retained(handle: str) -> None:
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
    handle: str,
    request: SandboxRequest,
    allocation: ResourceAllocation,
) -> QemuSandbox:
    """Reconnect to a running retained VM without taking ownership of it."""
    if not handle.startswith(HANDLE_PREFIX):
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
    if labels.get(EPISODE_LABEL) != request.episode_id:
        raise ProviderCapabilityError(f"{handle!r} belongs to a different episode")
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
    ports = record.get("NetworkSettings", {}).get("Ports", {}).get(f"{GUEST_PORT}/tcp") or []
    try:
        host_port = int(ports[0]["HostPort"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ProviderCapabilityError(f"{handle!r} has no guest service port") from exc

    resolved = await resolve_prepared_vm_image(request.prepared_image)
    transport = TcpTransport("127.0.0.1", host_port)
    await transport.start(timeout_sec=BOOT_TIMEOUT_SEC)
    client = GuestClient(transport)
    provider = QemuProvider()
    attached_request = request.model_copy(update={"role": SandboxRole(role)})
    try:
        agent_user, agent_home, _ = await provider._read_guest_contract(client, attached_request)
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
        resolved_image=resolved,
        allocation=allocation,
        agent_user=agent_user,
        agent_home=agent_home,
    )


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


async def _run(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


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
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        resource_lease: GpuLease | None = None,
        agent_user: str = DEFAULT_AGENT_USER,
        agent_home: str = "/home/user",
        host_ip: str = "",
    ) -> None:
        super().__init__(
            sandbox_id=sandbox_id,
            request=request,
            resolved_image=resolved_image,
            allocation=allocation,
            resource_lease=resource_lease,
        )
        self.container = container
        self.storage = storage
        self.host_port = host_port
        self.agent_user = agent_user
        self.agent_home = agent_home
        self.host_ip = host_ip
        self._client = client
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
        scheme, _, rest = url.partition("://")
        _, _, port = rest.partition(":")
        return f"{scheme}://{HOST_IP}:{port}" if port else url

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

    async def read_file(self, path: str) -> bytes:
        return await self._client.read_file(str(path))

    async def upload_dir(
        self,
        source: str,
        target: str,
        *,
        identity: Identity = Identity.FRAMEWORK,
    ) -> None:
        """Copy a directory in over the guest protocol.

        There is no ``docker cp`` equivalent here, and adding a share or an SSH
        dependency would be a second transport to keep working. The guest service
        already moves files, so the directory is walked and sent through it.
        """
        root = Path(source)
        run_as = self._as(identity)
        path_type = PureWindowsPath if self.request.os is OperatingSystem.WINDOWS else PurePosixPath
        target_path = path_type(str(target))
        await self._client.mkdirs(str(target_path), run_as=run_as)
        for entry in sorted(root.rglob("*")):
            relative = entry.relative_to(root)
            destination = target_path.joinpath(*relative.parts)
            if entry.is_dir():
                await self._client.mkdirs(str(destination), run_as=run_as)
            elif entry.is_file():
                await self._client.mkdirs(str(destination.parent), run_as=run_as)
                await self._client.write_file(str(destination), entry.read_bytes(), run_as=run_as)
        if run_as and self.request.os is OperatingSystem.LINUX:
            # The directories were made by the guest service, which is root; without this
            # the agent owns the files it was given and not the tree holding them.
            await self._client.exec(["chown", "-R", run_as, str(target)])

    async def download_dir(self, source: str, target: str) -> None:
        Path(target).mkdir(parents=True, exist_ok=True)
        path_type = PureWindowsPath if self.request.os is OperatingSystem.WINDOWS else PurePosixPath
        source_path = path_type(str(source))
        for relative_name in await self._client.list_files(str(source_path)):
            relative = PurePosixPath(relative_name)
            remote = source_path.joinpath(*relative.parts)
            local = Path(target).joinpath(*relative.parts)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(await self._client.read_file(str(remote)))

    async def screenshot(self) -> bytes:
        return await self._client.screenshot()

    async def inject_input(self, actions: Sequence[object]) -> int:
        payload = [
            action if isinstance(action, dict) else action.model_dump(mode="json")  # type: ignore[union-attr]
            for action in actions
        ]
        return await self._client.inject_input(payload)

    async def open_egress(self) -> None:
        """Lift the runner firewall for the framework's own phases."""
        for rule in (
            f"iptables -D FORWARD -i {GUEST_BRIDGE} ! -d {self.host_ip} -j DROP",
            f"iptables -D INPUT -i {GUEST_BRIDGE} -j DROP",
        ):
            await _run("docker", "exec", self.container, "sh", "-c", rule, timeout=60)
        self._sealed = False

    async def close_egress(self) -> None:
        """Restore the runner firewall before the agent starts."""
        if self.request.network.mode is NetworkMode.OPEN:
            return
        for rule in (
            f"iptables -I FORWARD -i {GUEST_BRIDGE} ! -d {self.host_ip} -j DROP",
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

    async def destroy(self) -> None:
        """Idempotent: teardown also runs on failure paths, sometimes twice."""
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED

        try:
            with contextlib.suppress(Exception):
                await self._client.close()
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
        await self._client.close()
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
        image_cache_dir: Path | None = None,
        gpu_devices: tuple[str, ...] = (),
        gpu_lock_dir: Path | None = None,
    ) -> None:
        self.fixture_image = image
        # Host-side, and named for what it holds. It was `work_dir`, which is a retired
        # name: the workspace is the agent's home *inside* a sandbox, and reusing the word
        # for a directory on this machine is the collision the lexicon exists to prevent.
        self.overlay_dir = overlay_dir or Path.home() / ".cache/ale/qemu"
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
            snapshot=False,
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
            raise ProviderCapabilityError("QemuProvider requires image.kind=vm")
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

    async def create(self, request: SandboxRequest) -> Sandbox:
        self.accepts(request)
        if request.image_kind is not ImageKind.VM:
            raise ProviderCapabilityError("QemuProvider requires image.kind=vm")
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
        resolved_image = await resolve_prepared_vm_image(request.prepared_image)
        base_image = Path(resolved_image.observed_ref)

        sandbox_id = f"{request.episode_id}-{uuid.uuid4().hex[:6]}"
        storage = self.overlay_dir / sandbox_id
        storage.mkdir(parents=True, exist_ok=True)
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

        try:
            host_ip = await self._wire_network(container, request)
            transport = TcpTransport("127.0.0.1", host_port)
            await transport.start(timeout_sec=BOOT_TIMEOUT_SEC)
            client = GuestClient(transport)
            agent_user, agent_home, has_desktop = await self._read_guest_contract(client, request)
            effective_storage_mb = await self._prepare_root_storage(
                client, request.resources.storage_mb, request.os
            )
            observed_gpu = await self._guest_gpus(client) if selected else ()
            if selected:
                _verify_qemu_gpu_count(request.resources.gpus, observed_gpu)
            if has_desktop:
                # The one thing the manifest's `gui` flag decides. A guest that installs a
                # desktop starts an X server, a display manager and a session after the
                # guest service is already answering, so "the sandbox replied" is not
                # "the sandbox has a screen". No probe can tell the difference on its own:
                # a screenshot that fails at this instant means "no desktop here" and
                # "not yet" equally, and only the image knows which.
                await self._await_desktop(client)
            if request.sudo:
                await self._grant_sudo(client, agent_user, request.os)
        except BaseException:
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
                        runtime_identity=f"qemu-runner/{RUNNER_IMAGE}",
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
        )
        if request.network.mode is NetworkMode.OPEN:
            await sandbox.open_egress()
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
            # Hypervisor enlightenments are for Windows guests; a Linux guest boots
            # faster without them.
            "--env", f"HV={'Y' if request.os is OperatingSystem.WINDOWS else 'N'}",
        ]  # fmt: skip
        if gpus:
            argv += ["--label", f"{GPU_LABEL}={','.join(gpu.bdf for gpu in gpus)}"]
            argv += ["--device=/dev/vfio/vfio", "--ulimit", "memlock=-1:-1"]
            for group in sorted({gpu.group for gpu in gpus}):
                argv += [f"--device=/dev/vfio/{group}"]
            argv += ["--env", f"ALE_QEMU_VFIO_DEVICES={','.join(gpu.bdf for gpu in gpus)}"]
        argv += [RUNNER_IMAGE]

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

        The forwarding is set up once and never moved: the guest's single permitted
        address is the runner, and the runner sends that one port on to the host. Doing it
        here rather than in the guest means the host's address is discovered at run time
        instead of baked into a disk.
        """
        code, route, _ = await _run(
            "docker", "exec", container, "sh", "-c",
            "ip route | awk '/^default/{print $3}'", timeout=60,
        )  # fmt: skip
        host_ip = route.strip()
        if code != 0 or not host_ip:
            raise ProviderStartError("could not find the host's address from inside the runner")

        rules: list[str] = []
        ports = {
            _port_of(request.gateway_url),
            _port_of(request.proxy_url) if request.network.mode is NetworkMode.ALLOWLIST else "",
        }
        for port in sorted(ports - {""}):
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
        if request.os is OperatingSystem.LINUX:
            # Published pre-Windows Linux images predate these health fields but already
            # implement the fixed base-image identity contract.
            user = user or DEFAULT_AGENT_USER
            home = home or f"/home/{user}"
        if not user or not home or not await client.exists(home):
            raise ProviderCapabilityError(
                f"guestd names {user!r} at {home!r} as the agent account, but that home is absent"
            )
        return user, home, bool(health.get("gui"))

    async def _grant_sudo(
        self, client: GuestClient, agent_user: str, operating_system: OperatingSystem
    ) -> None:
        """Elevate the agent, and prove it took.

        Written and then checked, because a rule can land in a guest with no sudo binary
        at all: the write succeeds, the run reports an elevated agent, and nothing can
        actually elevate. Provenance would record an isolation level that never applied.
        """
        if operating_system is OperatingSystem.WINDOWS:
            code, _, stderr, _ = await client.exec(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "if (-not ([Security.Principal.WindowsPrincipal] "
                    "[Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole("
                    "[Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 1 }",
                ],
                run_as=agent_user,
                timeout_sec=30,
            )
            if code != 0:
                raise ProviderCapabilityError(
                    f"elevated privileges were requested but {agent_user} is not an Administrator: "
                    f"{stderr.strip()}"
                )
            return
        await client.write_file(
            f"/etc/sudoers.d/ale-{agent_user}",
            f"{agent_user} ALL=(ALL) NOPASSWD: ALL\n".encode(),
            mode="0440",
        )
        code, _, stderr, _ = await client.exec(
            ["sudo", "-n", "true"], run_as=agent_user, timeout_sec=30
        )
        if code != 0:
            raise ProviderCapabilityError(
                f"elevated privileges were requested but {agent_user} still cannot "
                f"elevate in this guest: {stderr.strip()}"
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


def _port_of(url: str | None) -> str:
    """The port a gateway URL names, or empty when a run has no gateway at all."""
    if not url:
        return ""
    _, _, rest = url.partition("://")
    host_port, _, _ = rest.partition("/")
    _, _, port = host_port.partition(":")
    return port


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
