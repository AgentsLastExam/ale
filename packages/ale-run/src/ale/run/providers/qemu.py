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
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    Capabilities,
    ExecResult,
    Identity,
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

#: The image that hosts the virtual machine (built by images/base/qemu-runner). It is
#: inherited from the previous framework, which had already worked out the device
#: permissions, guest bridge and signal handling a QEMU-in-a-container needs; ours is
#: published under our own namespace so a run does not depend on an image someone else
#: can move, and differs only in what its entrypoint checks and says.
RUNNER_IMAGE = os.environ.get("ALE_QEMU_RUNNER", "ghcr.io/agentslastexam/ale-qemu-runner:0.1.0")

#: Where the runner expects the disk to boot, and the address it presents the host at.
#: The in-guest firewall rule and the rewritten gateway URL both target the latter.
RUNNER_DISK = "/storage/data.qcow2"
RUNNER_BASE = "/images/base.qcow2"

#: The interface inside the runner that the guest is attached to. Every packet the guest
#: sends arrives on it, which is what makes one rule enough to confine it.
GUEST_BRIDGE = "docker"

#: The deny-all ruleset baked into the guest, reloaded when the agent's phase begins.
GUEST_RULES = "/etc/nftables.conf"
HOST_IP = "172.30.0.1"

#: Where the guest writes the same facts a container image puts in labels: which account
#: is the agent's, and whether there is a screen. A disk image has nowhere to hang a label,
#: so the contract of docs/specs/sandbox-image.md is carried in a file instead.
MANIFEST_PATH = "/etc/ale/image.json"
DEFAULT_AGENT_USER = "user"

#: A guest boots an operating system, so this is minutes rather than the seconds a
#: container takes.
BOOT_TIMEOUT_SEC = 300


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
        agent_user: str = DEFAULT_AGENT_USER,
        host_ip: str = "",
    ) -> None:
        super().__init__(sandbox_id=sandbox_id, request=request)
        self.container = container
        self.storage = storage
        self.host_port = host_port
        self.agent_user = agent_user
        self.host_ip = host_ip
        self._client = client
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
    ) -> ExecResult:
        loop = asyncio.get_running_loop()
        started = loop.time()
        exit_code, stdout, stderr = await self._client.exec(
            argv, cwd=cwd, env=env, timeout_sec=timeout_sec, run_as=self._as(identity)
        )
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((loop.time() - started) * 1000),
        )

    async def write_file(
        self,
        path: PurePosixPath | str,
        data: bytes,
        *,
        identity: Identity = Identity.FRAMEWORK,
    ) -> None:
        await self._client.write_file(str(path), data, run_as=self._as(identity))

    async def read_file(self, path: PurePosixPath | str) -> bytes:
        return await self._client.read_file(str(path))

    async def upload_dir(
        self,
        source: str,
        target: PurePosixPath | str,
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
        await self._client.mkdirs(str(target))
        for entry in sorted(root.rglob("*")):
            relative = entry.relative_to(root)
            destination = PurePosixPath(str(target)) / relative
            if entry.is_dir():
                await self._client.mkdirs(str(destination))
            elif entry.is_file():
                await self._client.mkdirs(str(destination.parent))
                await self._client.write_file(str(destination), entry.read_bytes(), run_as=run_as)
        if run_as:
            # The directories were made by the guest service, which is root; without this
            # the agent owns the files it was given and not the tree holding them.
            await self._client.exec(["chown", "-R", run_as, str(target)])

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

    async def open_egress(self) -> None:
        """Lift both halves of the confinement, for the framework's own phases.

        Both, because either alone would leave the guest sealed: the runner drops what the
        guest forwards, and the guest's own firewall permits only the runner. They are
        applied and lifted together for the same reason they exist together.
        """
        if self.request.network.mode is NetworkMode.OPEN:
            return
        for rule in (
            f"iptables -D FORWARD -i {GUEST_BRIDGE} ! -d {self.host_ip} -j DROP",
            f"iptables -D INPUT -i {GUEST_BRIDGE} -j DROP",
        ):
            await _run("docker", "exec", self.container, "sh", "-c", rule, timeout=60)
        await self._client.exec(["nft", "flush", "ruleset"], timeout_sec=60)

    async def close_egress(self) -> None:
        """Put both halves back, before the agent starts."""
        if self.request.network.mode is NetworkMode.OPEN:
            return
        result = await self._client.exec(["nft", "-f", GUEST_RULES], timeout_sec=60)
        if result[0] != 0:
            raise ProviderStartError(f"could not restore the guest firewall: {result[2].strip()}")
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

    async def destroy(self) -> None:
        """Idempotent: teardown also runs on failure paths, sometimes twice."""
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED

        with contextlib.suppress(Exception):
            await self._client.close()

        with contextlib.suppress(Exception):
            await _run("docker", "rm", "-f", self.container, timeout=60)

        # The overlay is this episode's entire mutable state, so removing it is the whole
        # cleanup — the golden image was never written to.
        with contextlib.suppress(OSError):
            shutil.rmtree(self.storage, ignore_errors=True)


class QemuProvider(Provider):
    """Supplies virtual-machine sandboxes from a golden qcow2."""

    name = "qemu"

    def __init__(
        self,
        *,
        image: Path | None = None,
        work_dir: Path | None = None,
        disk_size: str = "40G",
    ) -> None:
        self.image = image or Path(
            os.environ.get("ALE_QEMU_IMAGE", Path.home() / ".cache/ale/images/ale-ubuntu22.qcow2")
        )
        self.work_dir = work_dir or Path.home() / ".cache/ale/qemu"
        self.disk_size = disk_size

    def capabilities(self) -> Capabilities:
        return Capabilities(
            os="linux",
            # Whether there is a screen is a property of the disk that was built, not of
            # this backend; a guest with no desktop refuses the request when asked.
            gui=True,
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
        storage = self.work_dir / sandbox_id
        storage.mkdir(parents=True, exist_ok=True)
        await self._make_overlay(storage / "data.qcow2")

        host_port = _free_port()
        container = await self._boot(request, storage, host_port)

        try:
            host_ip = await self._wire_network(container, request)
            transport = TcpTransport("127.0.0.1", host_port)
            await transport.start(timeout_sec=BOOT_TIMEOUT_SEC)
            client = GuestClient(transport)
            agent_user = await self._read_manifest(client)
            if request.sudo:
                await self._grant_sudo(client, agent_user)
        except Exception:
            with contextlib.suppress(Exception):
                await _run("docker", "rm", "-f", container, timeout=60)
            shutil.rmtree(storage, ignore_errors=True)
            raise

        return QemuSandbox(
            sandbox_id=sandbox_id,
            request=request,
            container=container,
            storage=storage,
            host_port=host_port,
            client=client,
            agent_user=agent_user,
            host_ip=host_ip,
        )

    async def _make_overlay(self, overlay: Path) -> None:
        """A copy-on-write clone of the golden image; the golden image is never written.

        The backing path is the one the *runner* will see, not the one on this host, and
        ``-u`` is what lets it be written without being opened here. Recording the host's
        path instead is the mistake that costs a boot: qemu inside the container follows
        it, finds nothing, and refuses the disk.
        """
        code, _, stderr = await _run(
            "qemu-img", "create", "-u",
            "-f", "qcow2",
            "-F", "qcow2",
            "-b", RUNNER_BASE,
            str(overlay),
            self.disk_size,
            timeout=60,
        )  # fmt: skip
        if code != 0:
            raise ProviderStartError(f"could not create the episode overlay: {stderr.strip()}")

    async def _boot(self, request: SandboxRequest, storage: Path, host_port: int) -> str:
        """Start the runner, which starts the machine.

        The golden image is mounted read-only beside the overlay that backs onto it, so
        one image serves every concurrent episode and none of them can write to it.
        """
        name = f"ale-qemu-{uuid.uuid4().hex[:10]}"
        argv = [
            "docker", "run", "--detach", "--name", name,
            "--device=/dev/kvm",
            # The runner builds the guest's network itself, which needs the capability;
            # the guest is still confined by the container's own network and by the
            # firewall baked into the image.
            "--cap-add", "NET_ADMIN",
            "--shm-size", "1g",
            "--mount",
            f"type=bind,src={self.image.resolve()},dst={RUNNER_BASE},readonly",
            "--mount", f"type=bind,src={storage.resolve()},dst=/storage",
            "--publish", f"127.0.0.1:{host_port}:{GUEST_PORT}",
            "--env", f"RAM_SIZE={request.resources.memory_mb}M",
            "--env", f"CPU_CORES={request.resources.cpus}",
            "--env", "CPU_MODEL=host",
            # Hypervisor enlightenments are for Windows guests; a Linux guest boots
            # faster without them.
            "--env", "HV=N",
            "--env", f"DISK_SIZE={self.disk_size}",
            RUNNER_IMAGE,
        ]  # fmt: skip

        code, stdout, stderr = await _run(*argv, timeout=180)
        if code != 0:
            raise ProviderStartError(f"could not start the qemu runner: {stderr.strip()}")
        container = stdout.strip() and name

        # A runner that rejects the disk or the device exits at once; saying so now beats
        # waiting out the boot timeout on a machine that never started.
        await asyncio.sleep(1.0)
        alive, running, _ = await _run(
            "docker", "inspect", "-f", "{{.State.Running}}", container, timeout=30
        )
        if alive != 0 or running.strip() != "true":
            _, logs, _ = await _run("docker", "logs", "--tail", "20", container, timeout=30)
            with contextlib.suppress(Exception):
                await _run("docker", "rm", "-f", container, timeout=60)
            raise ProviderStartError(f"the qemu runner exited immediately: {logs.strip()}")
        return container

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
        port = _port_of(request.gateway_url)
        if port:
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

    async def _read_manifest(self, client: GuestClient) -> str:
        """Read which account this image calls the agent's.

        A container image answers with a label. A disk image has nowhere to put one, so it
        is a file in the guest — read through the guest service, which is the only channel
        into a machine that has no exec.
        """
        result = await client.exec(["cat", MANIFEST_PATH], timeout_sec=30)
        if result[0] != 0:
            raise ProviderCapabilityError(
                f"this guest image has no {MANIFEST_PATH}, so there is no way to know which "
                "account the agent runs as; rebuild it with images/base/qemu/build.sh"
            )
        try:
            manifest = json.loads(result[1])
        except json.JSONDecodeError as exc:
            raise ProviderCapabilityError(f"{MANIFEST_PATH} is not valid JSON: {exc}") from exc

        user = str(manifest.get("user") or DEFAULT_AGENT_USER)
        code, _, _ = await client.exec(["id", "-u", user], timeout_sec=30)
        if code != 0:
            raise ProviderCapabilityError(
                f"{MANIFEST_PATH} names {user!r} as the agent account, but no such user "
                "exists in this guest"
            )
        return user

    async def _grant_sudo(self, client: GuestClient, agent_user: str) -> None:
        """Elevate the agent, and prove it took.

        Written and then checked, because a rule can land in a guest with no sudo binary
        at all: the write succeeds, the run reports an elevated agent, and nothing can
        actually elevate. Provenance would record an isolation level that never applied.
        """
        await client.write_file(
            f"/etc/sudoers.d/ale-{agent_user}",
            f"{agent_user} ALL=(ALL) NOPASSWD: ALL\n".encode(),
            mode="0440",
        )
        code, _, stderr = await client.exec(
            ["sudo", "-n", "true"], run_as=agent_user, timeout_sec=30
        )
        if code != 0:
            raise ProviderCapabilityError(
                f"elevated privileges were requested but {agent_user} still cannot "
                f"elevate in this guest: {stderr.strip()}"
            )


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
