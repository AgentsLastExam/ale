"""Virtual-machine sandboxes.

The second backend exists to prove the sandbox contract is real. If a task, a harness
and a verdict all behave the same against a container and against a VM, then what they
depend on is the contract rather than Docker — and the future OS roadmap (Windows,
macOS guests) becomes "swap the guest image" rather than "write another framework".

Three choices are worth stating, because each has a plausible alternative:

* **Overlays, not copies.** Each episode gets a qcow2 whose backing file is the golden
  image, so a pristine guest costs milliseconds and no disk. It is also the primitive a
  future ``reset()`` would use.
* **User-mode networking.** Slirp needs no root, no tap devices and no bridge
  management. The guest has no route to anything except what is forwarded, so the
  default-deny posture is the network's shape rather than a rule that could be missing.
  In-guest nftables then allows only the gateway, and is baked into the image.
* **The guest service over a forwarded port.** One codebase, two transports: the same
  ``ale-guestd`` that Docker drives over exec-stdio is reached here over TCP.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    Capabilities,
    ExecResult,
    Provider,
    Sandbox,
    SandboxRequest,
    SandboxState,
)
from ale.core.taskspec import NetworkMode
from ale.run.transport import GuestClient, TcpTransport

__all__ = ["QemuProvider", "QemuSandbox"]

#: Where the guest service listens inside the VM. Forwarded to an ephemeral host port.
GUEST_PORT = 7411

#: Slirp always presents the host at this address, which is what the in-guest firewall
#: rule and the rewritten gateway URL both target.
SLIRP_HOST = "10.0.2.2"

BOOT_TIMEOUT_SEC = 180


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
        process: asyncio.subprocess.Process,
        overlay: Path,
        host_port: int,
        client: GuestClient,
    ) -> None:
        super().__init__(sandbox_id=sandbox_id, request=request)
        self.process = process
        self.overlay = overlay
        self.host_port = host_port
        self._client = client
        self.state = SandboxState.READY

    @property
    def gateway_url(self) -> str | None:
        """The gateway as this guest can reach it.

        Under slirp the host is always 10.0.2.2, so the host's own bind address is
        meaningless inside — the URL is rewritten here, which is why the gateway needs
        to know nothing about providers.
        """
        url = self.request.gateway_url
        if not url:
            return None
        scheme, _, rest = url.partition("://")
        _, _, port = rest.partition(":")
        return f"{scheme}://{SLIRP_HOST}:{port}" if port else url

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> ExecResult:
        loop = asyncio.get_running_loop()
        started = loop.time()
        exit_code, stdout, stderr = await self._client.exec(
            argv, cwd=cwd, env=env, timeout_sec=timeout_sec
        )
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((loop.time() - started) * 1000),
        )

    async def write_file(self, path: PurePosixPath | str, data: bytes) -> None:
        await self._client.write_file(str(path), data)

    async def read_file(self, path: PurePosixPath | str) -> bytes:
        return await self._client.read_file(str(path))

    async def upload_dir(self, source: str, target: PurePosixPath | str) -> None:
        """Copy a directory in over the guest protocol.

        There is no ``docker cp`` equivalent here, and adding a share or an SSH
        dependency would be a second transport to keep working. The guest service
        already moves files, so the directory is walked and sent through it.
        """
        root = Path(source)
        await self._client.mkdirs(str(target))
        for entry in sorted(root.rglob("*")):
            relative = entry.relative_to(root)
            destination = PurePosixPath(str(target)) / relative
            if entry.is_dir():
                await self._client.mkdirs(str(destination))
            elif entry.is_file():
                await self._client.mkdirs(str(destination.parent))
                await self._client.write_file(str(destination), entry.read_bytes())

    async def download_dir(self, source: PurePosixPath | str, target: str) -> None:
        Path(target).mkdir(parents=True, exist_ok=True)
        listing = await self.exec(["find", str(source), "-type", "f"])
        for line in listing.stdout.splitlines():
            remote = line.strip()
            if not remote:
                continue
            relative = PurePosixPath(remote).relative_to(PurePosixPath(str(source)))
            local = Path(target) / relative
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(await self._client.read_file(remote))

    async def screenshot(self) -> bytes:
        return await self._client.screenshot()

    async def inject_input(self, actions: Sequence[object]) -> int:
        payload = [
            action if isinstance(action, dict) else action.model_dump(mode="json")  # type: ignore[union-attr]
            for action in actions
        ]
        return await self._client.inject_input(payload)

    async def destroy(self) -> None:
        """Idempotent: teardown also runs on failure paths, sometimes twice."""
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED

        with contextlib.suppress(Exception):
            await self._client.close()

        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=15)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

        # The overlay is this episode's entire mutable state, so removing it is the
        # whole cleanup — the golden image was never written to.
        with contextlib.suppress(OSError):
            self.overlay.unlink()


class QemuProvider(Provider):
    """Supplies virtual-machine sandboxes from a golden qcow2."""

    name = "qemu"

    def __init__(self, *, image: Path | None = None, work_dir: Path | None = None) -> None:
        self.image = image or Path(
            os.environ.get("ALE_QEMU_IMAGE", Path.home() / ".cache/ale/images/ale-ubuntu22.qcow2")
        )
        self.work_dir = work_dir or Path.home() / ".cache/ale/qemu"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            os="linux",
            gui=True,  # the guest image carries a desktop, as the container one does
            gpus=0,
            # `allowlist` needs the egress proxy reachable from inside the guest, which
            # slirp gives, but the in-guest rules are not written yet — so it is refused
            # rather than silently degraded.
            network_modes=frozenset({NetworkMode.BLOCK, NetworkMode.OPEN}),
            reset=False,
            snapshot=False,
        )

    async def preflight(self) -> None:
        """Fail early and specifically, before anything is provisioned."""
        problems: list[str] = []

        if shutil.which("qemu-system-x86_64") is None:
            problems.append("qemu-system-x86_64 is not on PATH (apt install qemu-system-x86)")
        if shutil.which("qemu-img") is None:
            problems.append("qemu-img is not on PATH (apt install qemu-utils)")
        if not Path("/dev/kvm").exists():
            problems.append(
                "/dev/kvm is missing: this host has no hardware virtualisation, so the "
                "container backend is the usable one here"
            )
        elif not os.access("/dev/kvm", os.R_OK | os.W_OK):
            problems.append("/dev/kvm is present but not writable (add yourself to the kvm group)")
        if not self.image.is_file():
            problems.append(
                f"no guest image at {self.image}; build one with images/base/qemu/build.sh "
                "or set ALE_QEMU_IMAGE"
            )

        if problems:
            raise ProviderCapabilityError("; ".join(problems))

    async def create(self, request: SandboxRequest) -> Sandbox:
        self.accepts(request)
        await self.preflight()

        sandbox_id = f"{request.episode_id}-{uuid.uuid4().hex[:6]}"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        overlay = self.work_dir / f"{sandbox_id}.qcow2"
        await self._make_overlay(overlay)

        host_port = _free_port()
        process = await self._boot(request, overlay, host_port)

        try:
            transport = TcpTransport("127.0.0.1", host_port)
            await transport.start(timeout_sec=BOOT_TIMEOUT_SEC)
            client = GuestClient(transport)
        except Exception:
            process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=10)
            with contextlib.suppress(OSError):
                overlay.unlink()
            raise

        return QemuSandbox(
            sandbox_id=sandbox_id,
            request=request,
            process=process,
            overlay=overlay,
            host_port=host_port,
            client=client,
        )

    async def _make_overlay(self, overlay: Path) -> None:
        """A copy-on-write clone of the golden image; the golden image is never written."""
        code, _, stderr = await _run(
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(self.image.resolve()),
            str(overlay),
            timeout=60,
        )
        if code != 0:
            raise ProviderStartError(f"could not create the episode overlay: {stderr.strip()}")

    async def _boot(
        self, request: SandboxRequest, overlay: Path, host_port: int
    ) -> asyncio.subprocess.Process:
        forwards = f"hostfwd=tcp:127.0.0.1:{host_port}-:{GUEST_PORT}"
        argv = [
            "qemu-system-x86_64",
            "-enable-kvm",
            "-machine", "q35,accel=kvm",
            "-cpu", "host",
            "-smp", str(request.resources.cpus),
            "-m", str(request.resources.memory_mb),
            "-drive", f"file={overlay},if=virtio,format=qcow2",
            "-netdev", f"user,id=net0,{forwards}",
            "-device", "virtio-net-pci,netdev=net0",
            "-display", "none",
            "-serial", "null",
            "-monitor", "none",
        ]  # fmt: skip

        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )

        # A VM that dies on boot should say why now, not time out in the transport.
        await asyncio.sleep(0.5)
        if process.returncode is not None:
            raw = await process.stderr.read() if process.stderr else b""
            raise ProviderStartError(
                f"qemu exited immediately: {raw.decode('utf-8', 'replace').strip()}"
            )
        return process


def _free_port() -> int:
    """Ask the OS for a port, then hand it to qemu.

    There is a race between closing this socket and qemu binding it, which is why the
    port is ephemeral per episode rather than fixed: a collision costs one retry of one
    episode instead of making concurrent runs impossible.
    """
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
