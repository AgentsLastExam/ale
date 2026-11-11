"""Linux sandboxes as Firecracker microVMs.

A Firecracker microVM is a virtual machine with only a few virtio devices (disks, a
network card, an entropy source) and no firmware, display or PCI bus. It boots a Linux
kernel in well under a second and costs a few megabytes beyond its guest's memory, so a
Linux Task gets its own kernel at close to a container's price. Five choices shape it:

* **The Task's container image, not a VM disk.** The Provider takes the same prepared
  OCI image Docker runs and adds the same framework layer, then converts the result once
  into an ext4 disk. A Task needs nothing new to run here, and one prepared image serves
  both backends.
* **Docker's job, done by a small init.** An OCI image has no kernel and usually no init.
  ``firecracker_init.py`` becomes PID 1: it mounts what a process expects, starts guestd
  as root and the image's own command as its ``USER``, in its ``WORKDIR`` and with its
  ``ENV``, and stops the machine when that command exits, as a container would.
* **One read-only disk per image, one writable disk per sandbox.** Firecracker reads raw
  disks only, so copy-on-write happens in the guest: overlayfs lays a sparse ext4 the
  runner makes for each sandbox over the shared read-only root, as Docker's own overlay
  lays a container's layer over its image.
* **A runner container per microVM**, as the QEMU Provider has: the guest's tap device,
  its NAT and the egress rules live in that container's network namespace, so the Host
  needs Docker, ``/dev/kvm`` and ``/dev/net/tun`` and nothing of its own netfilter changes.
* **guestd over a forwarded port**, with the token the framework's first connection
  claims, exactly as on QEMU.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import io
import json
import math
import os
import secrets
import shutil
import socket
import tarfile
import uuid
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    Capabilities,
    DesktopResolution,
    ExecOutputSink,
    ExecResult,
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
from ale.run.framework import with_framework
from ale.run.images import resolve_container_image, resolve_prepared_container_image
from ale.run.providers.desktop import CONTAINER_VARIABLE, require_resolution
from ale.run.providers.privileges import set_elevation, verify_identities
from ale.run.sources import cache_root
from ale.run.transport import GuestClient, TcpTransport

__all__ = ["FirecrackerProvider", "FirecrackerSandbox"]

#: The image that hosts the microVM and converts disks for it (images/runtimes/firecracker-runner).
RUNNER_IMAGE = os.environ.get("ALE_FIRECRACKER_RUNNER", ale_image("ale-firecracker-runner"))
#: The guest kernel inside the runner image, shared by every microVM.
KERNEL = "/opt/ale/vmlinux"

GUEST_PORT = guestd.PORT
#: The runner's address on the guest's network: the only one a sealed guest may reach.
HOST_IP = guestd.HOST_ADDRESS
GUEST_IP = "172.30.0.2"
GUEST_MAC = "06:00:ac:1e:00:02"
#: The interface inside the runner that the guest is attached to.
GUEST_TAP = "tap0"

#: Where the runner finds each disk; the Provider bind-mounts them there.
RUNNER_ROOTFS = "/images/rootfs.ext4"
RUNNER_LAYERS = "/layers"
RUNNER_STORAGE = "/storage"
#: The init the kernel starts, run by the framework's Python, and the files it reads.
INIT_SOURCE = Path(__file__).with_name("firecracker_init.py")
GUEST_DIR = "/opt/ale/firecracker"

#: Firecracker boots in a fraction of a second; this covers the image's own start.
BOOT_TIMEOUT_SEC = 120
#: Firecracker's limit on vCPUs.
MAX_CPUS = 32
#: The writable disk when a Task names no storage. It is sparse, so its size costs nothing
#: until the guest writes.
DEFAULT_STORAGE_MB = 32768
#: ext4's own metadata on the writable disk, so the guest sees at least what was requested.
STORAGE_OVERHEAD_MB = 512
#: Read-only layers a sandbox stacks: a snapshot of a sandbox booted from a snapshot adds
#: one. Firecracker's virtio-mmio devices are few, so the chain is capped.
MAX_LAYERS = 8

MANAGED_LABEL = "ale.firecracker.managed"
EPISODE_LABEL = "ale.firecracker.episode"
ROLE_LABEL = "ale.firecracker.role"
RETENTION_LABEL = "ale.firecracker.retention"
STORAGE_LABEL = "ale.firecracker.storage"
HANDLE_PREFIX = "firecracker:"
SNAPSHOT_PREFIX = f"{HANDLE_PREFIX}snapshot:"
SNAPSHOT_RECORD = "snapshot.json"
SNAPSHOT_ROOTFS = "rootfs.ext4"
DISK_RECORD = "disk.json"
GUEST_TOKEN_FILE = "guestd-token"

#: The runner's nat chain that sends the fixed Gateway port to the port the Host serves the
#: Gateway on; a later run's Gateway replaces its rule.
GATEWAY_CHAIN = "ALE_GATEWAY"


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


def _port_of(url: str | None) -> str:
    """The port a URL names, or empty when a run has no such service."""
    if not url:
        return ""
    port = urlsplit(url).port
    return str(port) if port else ""


def _proxy_env(request: SandboxRequest) -> dict[str, str]:
    if not request.proxy_url:
        return {}
    proxy = f"http://{HOST_IP}:{_port_of(request.proxy_url)}"
    if request.proxy_token:
        proxy = proxy.replace("://", f"://{quote(request.proxy_token, safe='')}:@", 1)
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": f"{HOST_IP},localhost,127.0.0.1",
    }


def _sandbox_gateway(url: str | None) -> str | None:
    """The Gateway as the guest reaches it: the runner's address at the fixed port."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.port is None:
        return url
    return parts._replace(netloc=f"{HOST_IP}:{SANDBOX_GATEWAY_PORT}").geturl()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _sparse_digest(path: Path) -> str:
    """SHA-256 over a disk's size and each data extent's offset, length and bytes.

    The disks are sparse, a writable one mostly holes, so hashing only what is allocated
    identifies them without reading gigabytes of zeros. Zeros written where another copy has a hole
    hash differently, which can make two equal disks look different, never the reverse.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        descriptor = handle.fileno()
        size = os.fstat(descriptor).st_size
        digest.update(size.to_bytes(8, "big"))
        offset = 0
        while offset < size:
            try:
                start = os.lseek(descriptor, offset, os.SEEK_DATA)
            except OSError:
                break  # only a hole remains
            end = os.lseek(descriptor, start, os.SEEK_HOLE)
            handle.seek(start)
            digest.update(start.to_bytes(8, "big") + (end - start).to_bytes(8, "big"))
            remaining = end - start
            while remaining:
                chunk = handle.read(min(remaining, 8 << 20))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            offset = end
    return f"sha256:{digest.hexdigest()}"


def _is_blank(png: bytes) -> bool:
    """Whether a PNG is one flat colour: a desktop that is up but has drawn nothing yet."""
    data = bytearray()
    offset = 8
    while offset + 8 <= len(png):
        length = int.from_bytes(png[offset : offset + 4], "big")
        kind = png[offset + 4 : offset + 8]
        if kind == b"IDAT":
            data += png[offset + 8 : offset + 8 + length]
        elif kind == b"IEND":
            break
        offset += 12 + length
    if not data:
        return False
    try:
        return len(set(zlib.decompress(bytes(data)))) <= 2
    except zlib.error:
        return False


def _issue_guest_token(storage: Path) -> str:
    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(storage / GUEST_TOKEN_FILE, flags, 0o600), "w") as handle:
        handle.write(token)
    return token


def _read_guest_token(storage: Path) -> str:
    try:
        return (storage / GUEST_TOKEN_FILE).read_text().strip()
    except FileNotFoundError as exc:
        raise ProviderCapabilityError(f"retained sandbox {storage} has no guest token") from exc


async def _connect(host_port: int, token: str) -> GuestClient:
    transport = TcpTransport("127.0.0.1", host_port, token=token)
    client = GuestClient(transport)
    try:
        await transport.start(timeout_sec=BOOT_TIMEOUT_SEC, interval_sec=0.1)
    except BaseException:
        await client.close()
        raise
    return client


def _vm_config(*, cpus: int, memory_mb: int, hostname: str, layers: int) -> dict[str, object]:
    """Firecracker's configuration file: kernel, disks, network card and machine size.

    The kernel configures the guest's address itself (``ip=``) and starts the framework's
    Python on the init, which needs nothing else from the image. Probing the PS/2 keyboard
    took half of the kernel's boot, and nothing uses it, so the ``i8042`` options skip it;
    ``reboot=k`` still resets through the controller, which ends Firecracker.
    """
    boot_args = " ".join(
        (
            "console=ttyS0 reboot=k panic=1 pci=off quiet",
            "i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd",
            "root=/dev/vda ro rootfstype=ext4",
            f"ip={GUEST_IP}::{HOST_IP}:255.255.255.0:{hostname}:eth0:off",
            f"init={guestd.PYTHON['linux']} -- -I {GUEST_DIR}/init.py",
        )
    )
    drives: list[dict[str, object]] = [
        {"drive_id": "rootfs", "path_on_host": RUNNER_ROOTFS, "is_root_device": True,
         "is_read_only": True},
        {"drive_id": "rw", "path_on_host": f"{RUNNER_STORAGE}/rw.ext4",
         "is_root_device": False, "is_read_only": False},
    ]  # fmt: skip
    drives += [
        {"drive_id": f"layer{index}", "path_on_host": f"{RUNNER_LAYERS}/{index}.ext4",
         "is_root_device": False, "is_read_only": True}
        for index in range(layers)
    ]  # fmt: skip
    return {
        "boot-source": {"kernel_image_path": KERNEL, "boot_args": boot_args},
        "drives": drives,
        "network-interfaces": [
            {"iface_id": "eth0", "guest_mac": GUEST_MAC, "host_dev_name": GUEST_TAP}
        ],
        "machine-config": {"vcpu_count": cpus, "mem_size_mib": memory_mb, "smt": False},
        "entropy": {},
    }


def _layer_devices(count: int) -> list[str]:
    """The guest's block devices for ``count`` read-only layers, newest first.

    ``vda`` is the root and ``vdb`` the writable disk; layers follow in the order the
    snapshot chain records them, oldest first, and overlayfs wants the newest leftmost.
    """
    return [f"/dev/vd{chr(ord('c') + index)}" for index in reversed(range(count))]


def _inject(tar_path: Path, image: dict[str, object]) -> tuple[int, int]:
    """Append the init and the image's run configuration to an exported rootfs tar.

    Returns the blocks and entries the filesystem must hold. ``/opt/ale`` is root-only
    already, as the framework layer leaves it.
    """
    blocks = entries = 0
    with tarfile.open(tar_path, "a") as tar:
        for member in tar.getmembers():
            blocks += math.ceil(member.size / 4096) + 1
            entries += 1
        for name in ("", "/rw", "/old", "/run"):
            info = tarfile.TarInfo(f"{GUEST_DIR.lstrip('/')}{name}")
            info.type = tarfile.DIRTYPE
            info.mode = 0o700
            tar.addfile(info)
        for name, data in (
            ("init.py", INIT_SOURCE.read_bytes()),
            ("image.json", json.dumps(image).encode()),
        ):
            info = tarfile.TarInfo(f"{GUEST_DIR.lstrip('/')}/{name}")
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
            blocks += math.ceil(len(data) / 4096) + 1
            entries += 1
    return blocks, entries + 4


async def _image_config(image: str) -> dict:
    code, raw, stderr = await _run(
        "docker", "image", "inspect", "--format", "{{json .Config}}", image, timeout=30
    )
    if code != 0:
        raise ProviderStartError(f"could not inspect {image}: {stderr.strip()}")
    return json.loads(raw)


def _run_config(config: dict) -> dict[str, object]:
    """What the init needs from an image's configuration; an image must name a command."""
    run_config = {
        key: config.get(key) for key in ("Env", "Entrypoint", "Cmd", "User", "WorkingDir")
    }
    if not (run_config["Entrypoint"] or run_config["Cmd"]):
        raise ProviderCapabilityError(
            "the image has no command: a sandbox lives exactly as long as its command, and "
            "the engine will not supply one. See docs/specs/sandbox-image.md"
        )
    return run_config


class FirecrackerSandbox(Sandbox):
    """One microVM, driven through the guest service."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        request: SandboxRequest,
        container: str,
        storage: Path,
        layers: tuple[Path, ...],
        client: GuestClient,
        file_client: GuestClient,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        agent_user: str,
        agent_home: str,
        host_ip: str,
        snapshot_dir: Path,
        runner_image: str,
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
        self.storage = storage
        self.layers = layers
        self.host_ip = host_ip
        self.snapshot_dir = snapshot_dir
        self.runner_image = runner_image
        self._client = client
        self._file_client = file_client
        self._sealed = False
        self.state = SandboxState.READY

    def _as(self, identity: Identity) -> str | None:
        return self.agent_user if identity is Identity.AGENT else None

    @property
    def gateway_url(self) -> str | None:
        """The Gateway at the runner's fixed address, whatever port the Host serves it on."""
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
        self, path: str, data: bytes, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        await self._client.write_file(str(path), data, run_as=self._as(identity))

    async def read_file(self, path: str, *, offset: int = 0, length: int | None = None) -> bytes:
        return await self._file_client.read_file(str(path), offset=offset, length=length)

    async def list_files(self, path: str) -> tuple[SandboxFile, ...]:
        return await self._file_client.list_files(path)

    async def file_digest(self, path: str, *, length: int) -> str:
        return await self._file_client.file_digest(path, length=length)

    async def upload_dir(
        self, source: str, target: str, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        await self._client.upload_dir(source, str(target), run_as=self._as(identity))

    async def screenshot(self) -> bytes:
        return await self._client.screenshot()

    async def inject_input(self, actions: Sequence[object]) -> int:
        payload = [
            action if isinstance(action, dict) else action.model_dump(mode="json")  # type: ignore[union-attr]
            for action in actions
        ]
        return await self._client.inject_input(payload)

    def _seal_rules(self) -> tuple[str, ...]:
        """Guest traffic a sealed runner forwards: replies, and the episode's own ports."""
        ports = sorted(
            {
                port
                for url in (self.request.gateway_url, self.request.proxy_url)
                if (port := _port_of(url))
            }
        )
        allowed = (
            (f"-i {GUEST_TAP} -d {self.host_ip} -p tcp -m multiport --dports {','.join(ports)}",)
            if ports
            else ()
        )
        return (
            f"-i {GUEST_TAP} -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY",
            *allowed,
        )

    async def open_egress(self) -> None:
        """Lift the runner firewall for the framework's own phases."""
        for rule in (
            *(f"iptables -w -D FORWARD {match} -j ACCEPT" for match in self._seal_rules()),
            f"iptables -w -D FORWARD -i {GUEST_TAP} -j DROP",
            f"iptables -w -D INPUT -i {GUEST_TAP} -j DROP",
        ):
            await _run("docker", "exec", self.container, "sh", "-c", rule, timeout=60)
        self._sealed = False

    async def close_egress(self) -> None:
        """Confine the guest before the agent starts: only its Gateway and proxy remain."""
        if self.request.network.mode is NetworkMode.OPEN:
            return
        for rule in (
            f"iptables -w -I FORWARD 1 -i {GUEST_TAP} -j DROP",
            *(f"iptables -w -I FORWARD 1 {match} -j ACCEPT" for match in self._seal_rules()),
            f"iptables -w -I INPUT 1 -i {GUEST_TAP} -j DROP",
        ):
            code, _, stderr = await _run(
                "docker", "exec", self.container, "sh", "-c", rule, timeout=60
            )
            if code != 0:
                raise ProviderStartError(f"could not close egress: {stderr.strip()}")
        self._sealed = True

    async def snapshot(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        """Stop the guest cleanly and keep its writable disk as one more read-only layer.

        The snapshot holds the root disk and every layer under it, hard-linked where the
        filesystem allows, so it outlives the image cache at no copying cost; the new layer
        is a sparse copy of only what this sandbox wrote.
        """
        if len(self.layers) >= MAX_LAYERS:
            raise ProviderCapabilityError(
                f"{self.container} already stacks {len(self.layers)} snapshot layers, the most "
                "a Firecracker sandbox can boot"
            )
        target = self.snapshot_dir / self.sandbox_id
        target.mkdir(parents=True)
        try:
            await self._power_off()
            names = [SNAPSHOT_ROOTFS, *(f"layer-{i}.ext4" for i in range(len(self.layers) + 1))]
            sources = [Path(self.resolved_image.observed_ref), *self.layers]
            for name, source in zip(names, sources, strict=False):
                await _link_or_copy(source, target / name)
            await _sparse_copy(self.storage / "rw.ext4", target / names[-1])
            await _check_filesystem(target / names[-1], self.runner_image)
            layer = await asyncio.to_thread(_sparse_digest, target / names[-1])
            record = {
                "managed": True,
                "episode": self.request.episode_id,
                "role": self.request.role.value,
                "os": self.request.os.value,
                "prepared_identity": self.request.prepared_image.prepared_identity,
                "disks": names,
                # What it booted from, which covers the disks under the new layer, then that layer.
                "identity": "sha256:"
                + hashlib.sha256(
                    f"{self.resolved_image.observed_identity}\n{layer}".encode()
                ).hexdigest(),
            }
            (target / SNAPSHOT_RECORD).write_text(json.dumps(record, indent=2) + "\n")
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        handle = f"{SNAPSHOT_PREFIX}{self.sandbox_id}"
        return RetainedSandbox(
            provider="firecracker",
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"ale sandbox destroy {handle}",
        )

    async def _power_off(self) -> None:
        """Ask the init for a clean shutdown and wait for the runner to exit.

        The init stops every process, writes the disks back and unmounts the writable one,
        so a later microVM can mount it read-only without replaying a journal. It stops
        guestd too, so the request's reply may never come.
        """
        with contextlib.suppress(Exception):
            await self._client.exec(
                [guestd.PYTHON["linux"], "-c", "import os, signal; os.kill(1, signal.SIGINT)"],
                timeout_sec=30,
            )
        await asyncio.gather(
            self._client.close(), self._file_client.close(), return_exceptions=True
        )
        code, output, stderr = await _run("docker", "wait", self.container, timeout=120)
        if code != 0 or output.strip() != "0":
            raise ProviderStartError(
                f"{self.container} did not shut down cleanly: {stderr.strip() or output.strip()}"
            )

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
            shutil.rmtree(self.storage, ignore_errors=True)
        finally:
            self.release_resources()

    async def retain(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        await asyncio.gather(
            self._client.close(), self._file_client.close(), return_exceptions=True
        )
        handle = f"{HANDLE_PREFIX}{self.container}"
        return RetainedSandbox(
            provider="firecracker",
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"ale sandbox destroy {handle}",
        )


async def _link_or_copy(source: Path, target: Path) -> None:
    """A hard link where both share a filesystem; otherwise a sparse copy."""
    try:
        os.link(source, target)
    except OSError:
        await _sparse_copy(source, target)


async def _sparse_copy(source: Path, target: Path) -> None:
    code, _, stderr = await _run("cp", "--sparse=always", str(source), str(target), timeout=1800)
    if code != 0:
        raise ProviderStartError(f"could not copy {source}: {stderr.strip()}")


async def _check_filesystem(disk: Path, runner_image: str) -> None:
    """Check a layer before it is kept; a later microVM mounts it read-only, as it is."""
    code, stdout, stderr = await _run(
        "docker", "run", "--rm", "--network", "none",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,src={disk.parent.resolve()},dst=/disk",
        "--entrypoint", "e2fsck", runner_image, "-fp", f"/disk/{disk.name}",
        timeout=600,
    )  # fmt: skip
    if code not in (0, 1):  # 1: errors corrected
        raise ProviderStartError(f"the saved disk {disk.name} is damaged: {stderr or stdout}")


class FirecrackerProvider(Provider):
    """Supplies Linux sandboxes as Firecracker microVMs from prepared container images."""

    name = "firecracker"

    def __init__(
        self,
        *,
        overlay_dir: Path | None = None,
        snapshot_dir: Path | None = None,
        disk_dir: Path | None = None,
        runner_image: str = RUNNER_IMAGE,
    ) -> None:
        self.overlay_dir = overlay_dir or cache_root() / "firecracker"
        self.snapshot_dir = snapshot_dir or self.overlay_dir.parent / "firecracker-snapshots"
        self.disk_dir = disk_dir or self.overlay_dir.parent / "firecracker-disks"
        self.runner_image = runner_image

    def capabilities(self) -> Capabilities:
        return Capabilities(
            operating_systems=frozenset({OperatingSystem.LINUX}),
            # A desktop is the image's own X server, as in a container: the microVM has
            # no display device of its own.
            gui=True,
            network_modes=frozenset({NetworkMode.BLOCK, NetworkMode.ALLOWLIST, NetworkMode.OPEN}),
            max_cpus=MAX_CPUS,
            reset=False,
            snapshot=True,
        )

    def accepts(self, request: SandboxRequest) -> None:
        super().accepts(request)
        if request.resources.gpus:
            raise ProviderCapabilityError("a Firecracker microVM has no GPU")
        if request.image_kind is not ImageKind.CONTAINER:
            raise ProviderCapabilityError("FirecrackerProvider runs container images")

    async def preflight(self) -> None:
        problems: list[str] = []
        if shutil.which("docker") is None:
            problems.append("docker is not on PATH; the runner image is what holds Firecracker")
        if not Path("/dev/kvm").exists():
            problems.append("/dev/kvm is missing: this host has no hardware virtualisation")
        elif not os.access("/dev/kvm", os.R_OK | os.W_OK):
            problems.append("/dev/kvm is present but not writable (add yourself to the kvm group)")
        if not Path("/dev/net/tun").exists():
            problems.append("/dev/net/tun is missing; the microVM's network needs a tap device")
        if problems:
            raise ProviderCapabilityError("; ".join(problems))

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if image.kind is not ImageKind.CONTAINER:
            raise ProviderCapabilityError("FirecrackerProvider runs container images")
        prepared = await resolve_container_image(image) if isinstance(image, ImageRef) else image
        resolved = await resolve_prepared_container_image(prepared)
        _run_config(await _image_config(resolved.observed_ref))
        return prepared

    async def create(self, request: SandboxRequest) -> FirecrackerSandbox:
        self.accepts(request)
        await self.preflight()
        if request.snapshot is not None:
            resolved_image, layers = self._snapshot_image(request.snapshot, request)
        else:
            resolved_image = await self._disk(request)
            layers = ()

        sandbox_id = f"{request.episode_id}-{uuid.uuid4().hex[:6]}"
        container = f"ale-fc-{uuid.uuid4().hex[:10]}"
        storage = self.overlay_dir / sandbox_id
        storage.mkdir(parents=True, exist_ok=True)
        try:
            token = _issue_guest_token(storage)
            self._write_config(storage, request, container.removeprefix("ale-fc-"), len(layers))
            host_port = _free_port()
            await self._boot(
                container, request, storage, Path(resolved_image.observed_ref), layers, host_port
            )
        except BaseException:
            # A failed or cancelled `docker run` may still have created the container.
            with contextlib.suppress(BaseException):
                await asyncio.shield(_run("docker", "rm", "-f", container, timeout=60))
            shutil.rmtree(storage, ignore_errors=True)
            raise

        clients: list[GuestClient] = []
        try:
            host_ip = await self._wire_network(container, request)
            client = await self._first_connection(container, host_port, token)
            clients.append(client)
            agent_user, agent_home, has_desktop = await self._read_guest_contract(client, request)
            storage_mb = await self._root_capacity_mb(client, request.resources.storage_mb)
            desktop: DesktopResolution | None = None
            if has_desktop:
                await self._await_desktop(client)
                desktop = await require_resolution(
                    client, request.desktop_resolution, container=True
                )
            await set_elevation(
                client, agent_user, sudo=request.sudo, operating_system=OperatingSystem.LINUX
            )
            await verify_identities(client, agent_user, sudo=request.sudo)
            file_client = await _connect(host_port, token)
            clients.append(file_client)
        except BaseException:
            await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
            with contextlib.suppress(BaseException):
                await asyncio.shield(_run("docker", "rm", "-f", container, timeout=60))
            shutil.rmtree(storage, ignore_errors=True)
            raise

        sandbox = FirecrackerSandbox(
            sandbox_id=sandbox_id,
            request=request,
            container=container,
            storage=storage,
            layers=layers,
            client=client,
            file_client=file_client,
            resolved_image=resolved_image,
            allocation=ResourceAllocation(
                cpus=request.resources.cpus,
                memory_mb=request.resources.memory_mb,
                storage_mb=storage_mb,
                sudo=request.sudo,
                network_mode=request.network.mode,
                provider=self.name,
                desktop_resolution=desktop,
            ),
            agent_user=agent_user,
            agent_home=agent_home,
            host_ip=host_ip,
            snapshot_dir=self.snapshot_dir,
            runner_image=self.runner_image,
        )
        try:
            if request.network.mode is NetworkMode.OPEN:
                await sandbox.open_egress()
        except BaseException:
            await sandbox.destroy()
            raise
        return sandbox

    # --- the disk ------------------------------------------------------------------

    async def _disk(self, request: SandboxRequest) -> ResolvedImage:
        """The microVM disk of the request's image, converted once per image and framework.

        The framework layer is the one Docker adds, so both backends run the same files.
        """
        resolved = await resolve_prepared_container_image(request.prepared_image)
        framed = await with_framework(resolved.observed_identity, user=request.user)
        code, runner, stderr = await _run(
            "docker", "image", "inspect", "--format", "{{.Id}}", self.runner_image, timeout=30
        )
        if code != 0:
            raise ProviderCapabilityError(
                f"the Firecracker runner {self.runner_image} is unavailable; build it with "
                f"images/build.sh ale-firecracker-runner: {stderr.strip()}"
            )
        key = hashlib.sha256(
            "\n".join(
                (framed, runner.strip(), hashlib.sha256(INIT_SOURCE.read_bytes()).hexdigest())
            ).encode()
        ).hexdigest()[:32]
        directory = self.disk_dir / key
        self.disk_dir.mkdir(parents=True, exist_ok=True)
        with (self.disk_dir / f"{key}.lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.5)
            try:
                if not (directory / DISK_RECORD).is_file():
                    await self._convert(framed, directory)
                record = json.loads((directory / DISK_RECORD).read_text())
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        return resolved.model_copy(
            update={
                "observed_identity": record["identity"],
                "observed_ref": str(directory / SNAPSHOT_ROOTFS),
            }
        )

    async def _convert(self, image: str, directory: Path) -> None:
        """Export the framed image's filesystem and make a read-only ext4 of it.

        ``mke2fs -d`` reads the tar directly, keeping owners, modes and links, and runs as
        this user in the runner, so nothing is extracted on the Host. The image's
        ``ENV``, ``USER``, ``WORKDIR``, ``ENTRYPOINT`` and ``CMD`` travel with it for the
        init. Metadata checksums are off: they halved conversion speed, and the disk is
        never written.
        """
        run_config = _run_config(await _image_config(image))
        partial = directory.with_name(f".{directory.name}-{uuid.uuid4().hex[:8]}")
        partial.mkdir(parents=True)
        try:
            code, created, stderr = await _run(
                "docker", "create", "--entrypoint", "/ale-none", image, timeout=120
            )
            if code != 0:
                raise ProviderStartError(f"could not export {image}: {stderr.strip()}")
            try:
                code, _, stderr = await _run(
                    "docker",
                    "export",
                    "--output",
                    str(partial / "rootfs.tar"),
                    created.strip(),
                    timeout=1800,
                )
            finally:
                await _run("docker", "rm", "-f", created.strip(), timeout=60)
            if code != 0:
                raise ProviderStartError(f"could not export {image}: {stderr.strip()}")
            blocks, entries = await asyncio.to_thread(_inject, partial / "rootfs.tar", run_config)
            size_mb = math.ceil(blocks * 4096 * 1.1 / 2**20) + 256
            code, stdout, stderr = await _run(
                "docker", "run", "--rm", "--network", "none",
                "--user", f"{os.getuid()}:{os.getgid()}", "--env", "LC_ALL=C.UTF-8",
                "--mount", f"type=bind,src={partial.resolve()},dst=/disk",
                "--entrypoint", "sh", self.runner_image, "-c",
                "set -e; "
                "mke2fs -q -F -t ext4 -O ^has_journal,^metadata_csum -E root_owner=0:0 "
                f"-N {entries + 4096} -d /disk/rootfs.tar /disk/{SNAPSHOT_ROOTFS} {size_mb}M; "
                # A container marker Docker leaves in every export; this is not a container.
                f"debugfs -w -R 'rm /.dockerenv' /disk/{SNAPSHOT_ROOTFS} >/dev/null 2>&1 || true",
                timeout=3600,
            )  # fmt: skip
            if code != 0:
                raise ProviderStartError(
                    f"could not convert {image} to a microVM disk: {stderr.strip() or stdout}"
                )
            (partial / "rootfs.tar").unlink()
            identity = await asyncio.to_thread(_sparse_digest, partial / SNAPSHOT_ROOTFS)
            record = {"image": image, "identity": identity, "config": run_config}
            (partial / DISK_RECORD).write_text(json.dumps(record, indent=2) + "\n")
            shutil.rmtree(directory, ignore_errors=True)
            partial.rename(directory)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise

    # --- the machine ---------------------------------------------------------------

    def _write_config(
        self, storage: Path, request: SandboxRequest, hostname: str, layers: int
    ) -> None:
        env = dict(request.env)
        env[CONTAINER_VARIABLE] = str(request.desktop_resolution)
        if gateway := _sandbox_gateway(request.gateway_url):
            env["ALE_GATEWAY_URL"] = gateway
        sandbox = {
            "hostname": hostname,
            "address": GUEST_IP,
            "agent_user": request.user,
            "guest_port": GUEST_PORT,
            "env": env,
            "layers": _layer_devices(layers),
        }
        vm = _vm_config(
            cpus=request.resources.cpus,
            memory_mb=request.resources.memory_mb,
            hostname=hostname,
            layers=layers,
        )
        (storage / "sandbox.json").write_text(json.dumps(sandbox, indent=2) + "\n")
        (storage / "vm.json").write_text(json.dumps(vm, indent=2) + "\n")

    async def _boot(
        self,
        name: str,
        request: SandboxRequest,
        storage: Path,
        rootfs: Path,
        layers: tuple[Path, ...],
        host_port: int,
    ) -> None:
        """Start the runner, which makes the writable disk and starts the microVM."""
        storage_mb = request.resources.storage_mb or DEFAULT_STORAGE_MB
        argv = [
            "docker", "run", "--detach", "--name", name,
            "--label", f"{MANAGED_LABEL}=true",
            "--label", f"{EPISODE_LABEL}={request.episode_id}",
            "--label", f"{ROLE_LABEL}={request.role.value}",
            "--label", f"{RETENTION_LABEL}={request.retention}",
            "--label", f"{STORAGE_LABEL}={storage.resolve()}",
            "--device", "/dev/kvm", "--device", "/dev/net/tun",
            # The runner builds and confines the guest's network itself.
            "--cap-add", "NET_ADMIN", "--sysctl", "net.ipv4.ip_forward=1",
            "--mount", f"type=bind,src={rootfs.resolve()},dst={RUNNER_ROOTFS},readonly",
        ]  # fmt: skip
        for index, layer in enumerate(layers):
            argv += [
                "--mount",
                f"type=bind,src={layer.resolve()},dst={RUNNER_LAYERS}/{index}.ext4,readonly",
            ]
        argv += [
            "--mount", f"type=bind,src={storage.resolve()},dst={RUNNER_STORAGE}",
            "--publish", f"127.0.0.1:{host_port}:{GUEST_PORT}",
            "--env", f"RW_DISK_MB={storage_mb + STORAGE_OVERHEAD_MB}",
            self.runner_image,
        ]  # fmt: skip
        code, _, stderr = await _run(*argv, timeout=180)
        if code != 0:
            raise ProviderStartError(f"could not start the Firecracker runner: {stderr.strip()}")

    async def _first_connection(self, container: str, host_port: int, token: str) -> GuestClient:
        """The guest service's first connection, or the console's last lines if the VM stopped.

        A microVM whose init fails resets at once, and its runner exits; waiting out the
        boot deadline would hide why.
        """
        connecting = asyncio.ensure_future(_connect(host_port, token))
        stopped = asyncio.ensure_future(
            _run("docker", "wait", container, timeout=BOOT_TIMEOUT_SEC + 30)
        )
        try:
            await asyncio.wait({connecting, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopped.cancel()
        if connecting.done():
            return connecting.result()
        connecting.cancel()
        with contextlib.suppress(BaseException):
            await connecting
        _, stdout, stderr = await _run("docker", "logs", "--tail", "20", container, timeout=30)
        raise ProviderStartError(
            f"the microVM stopped before its guest service answered: {(stdout + stderr).strip()}"
        )

    async def _wire_network(self, container: str, request: SandboxRequest) -> str:
        """Route the Gateway (and an allowlist's proxy) into the guest; return the Host's address.

        The guest dials the runner's fixed address; the runner sends it to the Host at the
        port this run serves. Confinement waits for ``close_egress``.
        """
        code, route, stderr = await _run(
            "docker", "exec", container, "sh", "-c", "ip route | awk '/^default/{print $3}'",
            timeout=60,
        )  # fmt: skip
        host_ip = route.strip()
        if code != 0 or not host_ip:
            raise ProviderStartError(f"could not find the Host from the runner: {stderr.strip()}")
        rules: list[str] = []
        if gateway_port := _port_of(request.gateway_url):
            rules += [
                f"iptables -w -t nat -N {GATEWAY_CHAIN} || iptables -w -t nat -F {GATEWAY_CHAIN}",
                f"iptables -w -t nat -C PREROUTING -j {GATEWAY_CHAIN} || "
                f"iptables -w -t nat -A PREROUTING -j {GATEWAY_CHAIN}",
                f"iptables -w -t nat -A {GATEWAY_CHAIN} -i {GUEST_TAP} -p tcp -d {HOST_IP} "
                f"--dport {SANDBOX_GATEWAY_PORT} -j DNAT --to-destination {host_ip}:{gateway_port}",
                f"iptables -w -t nat -C POSTROUTING -p tcp -d {host_ip} --dport {gateway_port} "
                f"-j MASQUERADE || iptables -w -t nat -A POSTROUTING -p tcp -d {host_ip} "
                f"--dport {gateway_port} -j MASQUERADE",
            ]
        if request.network.mode is NetworkMode.ALLOWLIST and (port := _port_of(request.proxy_url)):
            rules += [
                f"iptables -w -t nat -A PREROUTING -i {GUEST_TAP} -p tcp -d {HOST_IP} "
                f"--dport {port} -j DNAT --to-destination {host_ip}:{port}",
                f"iptables -w -t nat -A POSTROUTING -p tcp -d {host_ip} --dport {port} "
                "-j MASQUERADE",
            ]
        for rule in rules:
            code, _, stderr = await _run("docker", "exec", container, "sh", "-c", rule, timeout=60)
            if code != 0:
                raise ProviderStartError(f"could not route the Gateway: {stderr.strip()}")
        return host_ip

    async def _read_guest_contract(
        self, client: GuestClient, request: SandboxRequest
    ) -> tuple[str, str, bool]:
        health = await client.health()
        if health.get("os") != OperatingSystem.LINUX.value:
            raise ProviderCapabilityError(f"guestd reports {health.get('os')!r}, not linux")
        user = str(health.get("agent_user") or "")
        home = str(health.get("agent_home") or "")
        if user != request.user or not home or not await client.exists(home):
            raise ProviderCapabilityError(
                f"guestd names {user!r} at {home!r} as the agent account, but the Task names "
                f"{request.user!r} or that home is absent"
            )
        return user, home, bool(health.get("gui"))

    async def _root_capacity_mb(self, client: GuestClient, requested: int | None) -> int:
        """The writable root's capacity as the guest sees it; at least what was requested."""
        try:
            total, _, _ = await client.disk_usage("/")
        except Exception as exc:
            raise ProviderStartError(f"could not observe the root filesystem: {exc}") from exc
        capacity = total // (1024 * 1024)
        if requested is not None and capacity < requested:
            raise ProviderCapabilityError(
                f"the microVM's root holds {capacity} MB, the Task requests {requested} MB"
            )
        return capacity

    async def _await_desktop(self, client: GuestClient, timeout_sec: float = 180) -> None:
        """Wait until a screenshot shows something drawn.

        The image's X server answers before its session paints: in a microVM of the ALE
        container base the first capture, three seconds in, was black.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        last = "no screenshot was taken"
        while loop.time() < deadline:
            try:
                if not _is_blank(await client.screenshot()):
                    return
                last = "the screen is up but nothing has been drawn on it"
            except Exception as exc:
                last = str(exc)
            await asyncio.sleep(0.5)
        raise ProviderStartError(
            f"this image declares a desktop but none was ready within {timeout_sec:g}s ({last})"
        )

    # --- retention -----------------------------------------------------------------

    async def _inspect(self, container: str) -> dict:
        code, raw, stderr = await _run("docker", "inspect", container, timeout=30)
        if code != 0:
            raise ProviderStartError(f"sandbox {container!r} does not exist: {stderr.strip()}")
        return json.loads(raw)[0]

    def _owned_storage(self, handle: str, record: dict) -> Path:
        labels = record.get("Config", {}).get("Labels", {}) or {}
        if labels.get(MANAGED_LABEL) != "true" or labels.get(RETENTION_LABEL) != "keep":
            raise ProviderCapabilityError(f"{handle!r} is not an ALE-retained Firecracker sandbox")
        storage = labels.get(STORAGE_LABEL, "")
        mounted = {
            mount.get("Source", "")
            for mount in record.get("Mounts", ())
            if mount.get("Type") == "bind" and mount.get("Destination") == RUNNER_STORAGE
        }
        if not storage or storage not in mounted:
            raise ProviderCapabilityError(f"{handle!r} has no ALE-owned Firecracker storage")
        return Path(storage)

    async def list_retained(self) -> list[dict[str, object]]:
        code, output, stderr = await _run(
            "docker", "ps", "-a", "-q",
            "--filter", f"label={MANAGED_LABEL}=true",
            "--filter", f"label={RETENTION_LABEL}=keep",
            timeout=30,
        )  # fmt: skip
        if code != 0:
            raise ProviderStartError(f"could not list retained microVMs: {stderr.strip()}")
        found = self._list_snapshots()
        if containers := output.split():
            code, raw, stderr = await _run("docker", "inspect", *containers, timeout=30)
            if code != 0:
                raise ProviderStartError(f"could not inspect retained microVMs: {stderr.strip()}")
            for record in json.loads(raw):
                labels = record.get("Config", {}).get("Labels", {}) or {}
                container = record.get("Name", "").removeprefix("/")
                found.append(
                    {
                        "provider": "firecracker",
                        "handle": f"{HANDLE_PREFIX}{container}",
                        "episode": labels.get(EPISODE_LABEL, ""),
                        "role": labels.get(ROLE_LABEL, ""),
                        "image": next(
                            (
                                mount.get("Source", "")
                                for mount in record.get("Mounts", ())
                                if mount.get("Destination") == RUNNER_ROOTFS
                            ),
                            "",
                        ),
                        "gpus": (),
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
                continue  # not one ALE saved, or one still being saved
            found.append(
                {
                    "provider": "firecracker",
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

    def _snapshot_record(self, handle: str) -> dict:
        name = handle.removeprefix(SNAPSHOT_PREFIX)
        if not handle.startswith(SNAPSHOT_PREFIX) or not name or "/" in name or name[0] == ".":
            raise ProviderCapabilityError(f"{handle!r} is not a Firecracker snapshot handle")
        directory = self.snapshot_dir / name
        try:
            record = json.loads((directory / SNAPSHOT_RECORD).read_text())
        except FileNotFoundError as exc:
            raise ProviderStartError(f"snapshot {handle!r} does not exist") from exc
        except (OSError, ValueError) as exc:
            raise ProviderCapabilityError(f"{handle!r} has no valid snapshot record") from exc
        disks = record.get("disks") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or record.get("managed") is not True
            or not isinstance(disks, list)
            or not disks
            or disks[0] != SNAPSHOT_ROOTFS
            or any(not isinstance(disk, str) or "/" in disk for disk in disks)
            or not all((directory / disk).is_file() for disk in disks)
        ):
            raise ProviderCapabilityError(f"{handle!r} is not an ALE snapshot")
        return record

    def _snapshot_image(
        self, handle: str, request: SandboxRequest
    ) -> tuple[ResolvedImage, tuple[Path, ...]]:
        """The root disk and layers a microVM boots from a snapshot of the Task's own image."""
        record = self._snapshot_record(handle)
        if record.get("prepared_identity") != request.prepared_image.prepared_identity:
            raise ProviderCapabilityError(
                f"{handle!r} was not taken from the Task's prepared solver image"
            )
        directory = self.snapshot_dir / handle.removeprefix(SNAPSHOT_PREFIX)
        disks = [directory / disk for disk in record["disks"]]
        return (
            ResolvedImage(
                kind=ImageKind.CONTAINER,
                prepared_identity=request.prepared_image.prepared_identity,
                observed_identity=str(record["identity"]),
                observed_ref=str(disks[0]),
            ),
            tuple(disks[1:]),
        )

    async def _destroy_snapshot(self, handle: str) -> None:
        self._snapshot_record(handle)
        directory = (self.snapshot_dir / handle.removeprefix(SNAPSHOT_PREFIX)).resolve()
        code, output, stderr = await _run(
            "docker", "ps", "-a", "-q", "--filter", f"label={MANAGED_LABEL}=true", timeout=30
        )
        if code != 0:
            raise ProviderStartError(f"could not list microVMs: {stderr.strip()}")
        if containers := output.split():
            code, raw, stderr = await _run("docker", "inspect", *containers, timeout=30)
            if code != 0:
                raise ProviderStartError(f"could not inspect microVMs: {stderr.strip()}")
            for record in json.loads(raw):
                if any(
                    Path(mount.get("Source", "")).parent == directory
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
            raise ProviderCapabilityError(f"{handle!r} is not a Firecracker sandbox handle")
        container = handle.removeprefix(HANDLE_PREFIX)
        storage = self._owned_storage(handle, await self._inspect(container))
        code, _, stderr = await _run("docker", "rm", "-f", container, timeout=60)
        if code != 0:
            raise ProviderStartError(f"could not destroy {handle}: {stderr.strip()}")
        try:
            shutil.rmtree(storage)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ProviderStartError(
                f"destroyed {handle} but could not remove its storage {storage}: {exc}"
            ) from exc

    async def attach_retained(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
    ) -> FirecrackerSandbox:
        """Reconnect to a running retained microVM without taking ownership of it."""
        if handle.startswith(SNAPSHOT_PREFIX) or not handle.startswith(HANDLE_PREFIX):
            raise ProviderCapabilityError(f"{handle!r} is not a Firecracker sandbox handle")
        container = handle.removeprefix(HANDLE_PREFIX)
        record = await self._inspect(container)
        storage = self._owned_storage(handle, record)
        labels = record.get("Config", {}).get("Labels", {}) or {}
        role = labels.get(ROLE_LABEL, "")
        if role not in {SandboxRole.SOLVER.value, SandboxRole.SHARED.value}:
            raise ProviderCapabilityError(f"{handle!r} is not a retained ALE solver sandbox")
        if not record.get("State", {}).get("Running"):
            raise ProviderStartError(f"retained sandbox {handle!r} is not running")
        mounts = {mount.get("Destination"): mount for mount in record.get("Mounts", ())}
        rootfs = mounts.get(RUNNER_ROOTFS, {}).get("Source", "")
        if rootfs != str(Path(image.observed_ref).resolve()):
            raise ProviderCapabilityError(f"{handle!r} does not run the expected solver image")
        layers = tuple(
            Path(mounts[f"{RUNNER_LAYERS}/{index}.ext4"]["Source"])
            for index in range(MAX_LAYERS)
            if f"{RUNNER_LAYERS}/{index}.ext4" in mounts
        )
        ports = record.get("NetworkSettings", {}).get("Ports", {}).get(f"{GUEST_PORT}/tcp") or []
        try:
            host_port = int(ports[0]["HostPort"])
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ProviderCapabilityError(f"{handle!r} has no guest service port") from exc
        token = _read_guest_token(storage)
        attached_request = request.model_copy(update={"role": SandboxRole(role)})
        client = await _connect(host_port, token)
        try:
            agent_user, agent_home, _ = await self._read_guest_contract(client, attached_request)
            # A later run reaches its own Gateway at the same address.
            host_ip = await self._wire_network(container, attached_request)
            file_client = await _connect(host_port, token)
        except BaseException:
            await client.close()
            raise
        return FirecrackerSandbox(
            sandbox_id=container.removeprefix("ale-fc-"),
            request=attached_request,
            container=container,
            storage=storage,
            layers=layers,
            client=client,
            file_client=file_client,
            resolved_image=image,
            allocation=allocation,
            agent_user=agent_user,
            agent_home=agent_home,
            host_ip=host_ip,
            snapshot_dir=self.snapshot_dir,
            runner_image=self.runner_image,
        )
