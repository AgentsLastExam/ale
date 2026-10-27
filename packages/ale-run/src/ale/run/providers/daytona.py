"""Daytona container Provider: Linux sandboxes on Daytona's cloud.

Daytona lets the Host reach into a sandbox and refuses everything the other way, and it
runs its own daemon, which any process inside can drive, as the image's user. This backend
works within that:

* **Image.** The prepared image is adapted (``daytona_image``), pushed to Daytona's registry
  and built from there by digest, with the requested CPU, memory and disk.
* **Channel.** guestd runs over an SSH exec session, the way it runs over ``docker exec``.
  A second SSH session carries a stream tunnel (``guestd/tunnel.py``) so that the agent can
  reach the Host's Gateway on the sandbox's own loopback.
* **Network.** Daytona's ``networkBlockAll`` and domain allowlist apply outside the
  sandbox, where the agent cannot lift them; they replace the internal bridge and the
  Host's egress proxy that Docker uses, so allowed traffic never passes through the Host.
* **Daemon.** Daytona's daemon listens, without authentication, on a few local ports and
  runs what it is asked as root. The Provider rejects connections to them from every
  non-root user, then proves it by connecting as the agent. A sandbox that cannot be sealed
  is destroyed rather than handed out.
* **Image command.** Daytona starts its own daemon in place of the image's entrypoint and
  command, so the Provider starts those itself, as the image's user and with its
  environment and working directory, and waits for the desktop when the image has one.
* **GPUs.** A request for GPUs names the configured models; Daytona places the sandbox in
  its GPU region. The RunLock records the GPUs as the agent sees them, and a sandbox whose
  GPUs differ from the request is destroyed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from ale.core.config import DaytonaProviderConfig
from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    Capabilities,
    ExecOutputSink,
    ExecResult,
    GpuAllocation,
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
from ale.run.framework import MARKER, framework_identity, with_framework
from ale.run.gpu import parse_nvidia_smi
from ale.run.guestd.tunnel import Mux
from ale.run.images import resolve_container_image, resolve_prepared_container_image
from ale.run.providers.daytona_api import DaytonaApi, DaytonaError
from ale.run.providers.daytona_image import (
    ImageConfig,
    ImagePublisher,
    PublishedImage,
    image_config,
)
from ale.run.providers.docker import DockerProvider
from ale.run.providers.privileges import verify_identities
from ale.run.sources import cache_root
from ale.run.transport import GuestClient, StdioTransport

__all__ = ["HANDLE_PREFIX", "DaytonaProvider", "DaytonaSandbox"]

HANDLE_PREFIX = "daytona:"
#: Daytona organizations are shared, and other tools there use bare ``ale.*`` labels, so
#: ALE's own live under ``ale.run.``. Listing and destroying act only on these.
MANAGED_LABEL = "ale.run.managed"
EPISODE_LABEL = "ale.run.episode"
ROLE_LABEL = "ale.run.role"
RETENTION_LABEL = "ale.run.retention"

GUESTD_DIR = PurePosixPath("/opt/ale/guestd")
#: Local ports of Daytona's daemon: toolbox API, SSH server, web terminal, and one more.
DAEMON_PORTS = (2280, 22220, 22222, 33333)
#: How long a fresh SSH access token lives. Sessions already open outlive it.
_SSH_MINUTES = 60
_START_TIMEOUT_SEC = 900
#: How long a cancelled start waits for Daytona's reply to the create it already sent.
_CREATE_REPLY_SEC = 30
#: Public TLS endpoints that tell an open network from a closed one, as (address, name). The
#: first whose name the Task did not allowlist is used.
_EGRESS_PROBES = (("1.1.1.1:443", "one.one.one.one"), ("8.8.8.8:443", "dns.google"))
#: Daytona takes at most this many domains in an allowlist.
_DOMAIN_LIMIT = 100
_DNS_NAME = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
#: What the RunLock records of each GPU, read as the Docker Provider reads it.
_NVIDIA_QUERY = "uuid,name,pci.bus_id,driver_version"

#: Run as root in the sandbox. Rejects every non-root user's connections to the ports on
#: which Daytona's daemon (PID 1) listens, plus the known ones, over IPv4 and IPv6.
_SEAL = r"""
import json, os, re, subprocess, sys
known = {int(port) for port in sys.argv[1].split(",")}
inodes = set()
for entry in os.listdir("/proc/1/fd"):
    try:
        target = os.readlink(f"/proc/1/fd/{entry}")
    except OSError:
        continue
    if match := re.fullmatch(r"socket:\[(\d+)\]", target):
        inodes.add(match.group(1))
ports = set(known)
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        rows = open(table).read().splitlines()[1:]
    except OSError:
        continue
    for row in rows:
        fields = row.split()
        if fields[3] == "0A" and fields[9] in inodes:
            ports.add(int(fields[1].rsplit(":", 1)[1], 16))
ordered = sorted(ports)
for tool in ("iptables", "ip6tables"):
    for start in range(0, len(ordered), 15):  # multiport takes at most 15 ports a rule
        rule = ["-p", "tcp", "-m", "multiport", "--dports",
                ",".join(map(str, ordered[start:start + 15])),
                "-m", "owner", "!", "--uid-owner", "0",
                "-j", "REJECT", "--reject-with", "tcp-reset"]
        if subprocess.run([tool, "-w", "-C", "OUTPUT", *rule], capture_output=True).returncode:
            done = subprocess.run([tool, "-w", "-I", "OUTPUT", "1", *rule],
                                  capture_output=True, text=True)
            if done.returncode:
                print(f"{tool}: {done.stderr.strip()}", file=sys.stderr)
                sys.exit(1)
print(json.dumps(ordered))
"""

#: Run as the agent. Lists every daemon port it can still connect to, on every local address.
_PROBE = r"""
import json, socket, sys
ports = [int(port) for port in sys.argv[1].split(",")]
hosts = ["127.0.0.1", "::1"]
try:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect(("192.0.2.1", 9))
    hosts.append(probe.getsockname()[0])
    probe.close()
except OSError:
    pass
reached = []
for host in hosts:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for port in ports:
        try:
            with socket.socket(family) as connection:
                connection.settimeout(3)
                connection.connect((host, port))
            reached.append(f"{host}:{port}")
        except OSError:
            pass
print(json.dumps(reached))
"""

#: Run as root with a JSON spec. Starts the image's command detached, as its user.
_LAUNCH = r"""
import json, os, pwd, sys
spec = json.loads(sys.argv[1])
if os.fork():
    sys.exit(0)
os.setsid()
if os.fork():
    os._exit(0)  # re-parented to the daemon, as a container's own command would be
log = os.open(spec["log"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
os.dup2(log, 1)
os.dup2(log, 2)
os.closerange(3, 1024)
user, _, group = spec["user"].partition(":")
if user and user not in ("root", "0"):
    account = pwd.getpwuid(int(user)) if user.isdigit() else pwd.getpwnam(user)
    gid = int(group) if group.isdigit() else account.pw_gid
    if group and not group.isdigit():
        import grp
        gid = grp.getgrnam(group).gr_gid
    os.initgroups(account.pw_name, gid)
    os.setgid(gid)
    os.setuid(account.pw_uid)
if spec["workdir"]:
    os.chdir(spec["workdir"])
environment = dict(os.environ)
environment.update(spec["env"])
os.execvpe(spec["argv"][0], spec["argv"], environment)
"""

#: Whether a TLS handshake with ``host:port``, naming ``name``, completes from the sandbox.
#: A bare TCP connect cannot tell: under a domain allowlist Daytona's own proxy accepts every
#: connection to port 443 and drops the ones whose name is not listed.
_HANDSHAKE = r"""
import socket, ssl, sys
host, _, port = sys.argv[1].rpartition(":")
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
try:
    with socket.create_connection((host, int(port)), timeout=float(sys.argv[3])) as raw:
        with context.wrap_socket(raw, server_hostname=sys.argv[2]):
            print("open")
except OSError:
    print("closed")
"""


class _SshHost:
    """Opens SSH sessions into one sandbox, each with a fresh access token.

    The token is the SSH user name, so it goes in a private configuration file rather
    than on a command line that every local user can read with ``ps``.
    """

    def __init__(self, api: DaytonaApi, daytona_id: str) -> None:
        self._api = api
        self._id = daytona_id
        self._directory = Path(tempfile.mkdtemp(prefix="ale-daytona-ssh-"))
        self._count = 0

    async def argv(self, remote_command: str) -> list[str]:
        user, host, port = _parse_ssh(await self._api.ssh_command(self._id, minutes=_SSH_MINUTES))
        known_hosts = cache_root() / "daytona" / "known_hosts"
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        self._count += 1
        config = self._directory / f"ssh-{self._count}.conf"
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(
                "Host sandbox\n"
                f"    HostName {host}\n"
                f"    Port {port}\n"
                f"    User {user}\n"
                f"    UserKnownHostsFile {known_hosts}\n"
                "    StrictHostKeyChecking accept-new\n"
                "    BatchMode yes\n"
                "    LogLevel ERROR\n"
                "    ServerAliveInterval 15\n"
                "    ServerAliveCountMax 4\n"
                "    ControlMaster no\n"
                "    ClearAllForwardings yes\n"
                "    ForwardAgent no\n"
                "    ForwardX11 no\n"
            )
        return ["ssh", "-F", str(config), "-T", "sandbox", remote_command]

    def close(self) -> None:
        shutil.rmtree(self._directory, ignore_errors=True)


class _Tunnel:
    """The Host end of the stream tunnel; reconnects if its SSH session drops."""

    def __init__(
        self,
        ssh: _SshHost,
        *,
        listeners: list[tuple[int, int]],
        targets: list[tuple[str, int]],
    ) -> None:
        self._ssh = ssh
        self._targets = targets
        self._command = "exec python3 {} {}".format(
            GUESTD_DIR / "tunnel.py",
            " ".join(f"--listen {port}:{target}" for port, target in listeners),
        )
        self._closing = False
        self._first: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self, timeout_sec: float = 120) -> None:
        self._task = asyncio.ensure_future(self._supervise())
        try:
            await asyncio.wait_for(asyncio.shield(self._first), timeout_sec)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self._closing = True
        if self._first.done() and not self._first.cancelled():
            self._first.exception()  # mark retrieved
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _dial(self, index: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        host, port = self._targets[index]
        return await asyncio.open_connection(host, port)

    async def _supervise(self) -> None:
        failures = 0
        detail = ""
        while not self._closing:
            ready = False
            try:
                process = await asyncio.create_subprocess_exec(
                    *await self._ssh.argv(self._command),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._process = process
                assert process.stdin is not None and process.stdout is not None
                mux = Mux(process.stdout, process.stdin, dial=self._dial)
                running = asyncio.ensure_future(mux.run())
                try:
                    waiting = asyncio.ensure_future(mux.ready.wait())
                    await asyncio.wait(
                        {waiting, running}, timeout=60, return_when=asyncio.FIRST_COMPLETED
                    )
                    waiting.cancel()
                    ready = mux.ready.is_set()
                    if ready:
                        failures = 0
                        if not self._first.done():
                            self._first.set_result(None)
                        await running
                finally:
                    running.cancel()
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                    assert process.stderr is not None
                    detail = (await process.stderr.read()).decode("utf-8", "replace").strip()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
            if self._closing:
                return
            if not ready:
                failures += 1
                if failures >= 3:
                    if not self._first.done():
                        self._first.set_exception(
                            ProviderStartError(f"the sandbox tunnel did not start: {detail[-500:]}")
                        )
                    return
            await asyncio.sleep(min(2**failures, 15))


class DaytonaSandbox(Sandbox):
    """One Daytona sandbox, driven through guestd over SSH."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        daytona_id: str,
        request: SandboxRequest,
        api: DaytonaApi,
        ssh: _SshHost,
        client: GuestClient,
        file_client: GuestClient,
        tunnel: _Tunnel | None,
        local_gateway: str | None,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
        agent_user: str,
        agent_home: str,
    ) -> None:
        super().__init__(
            sandbox_id=sandbox_id,
            request=request,
            resolved_image=resolved_image,
            allocation=allocation,
            agent_user=agent_user,
            agent_home=agent_home,
        )
        self.daytona_id = daytona_id
        self._api = api
        self._ssh = ssh
        self._client = client
        self._file_client = file_client
        self._tunnel = tunnel
        self._local_gateway = local_gateway
        self.state = SandboxState.READY

    def _as(self, identity: Identity) -> str | None:
        return self.agent_user if identity is Identity.AGENT else None

    @property
    def gateway_url(self) -> str | None:
        """The Gateway on the sandbox's own loopback, where the tunnel delivers it."""
        return self._local_gateway

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

    # File reads have their own guestd session, so they proceed while a command runs.

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
        """Let the sandbox reach the network, for the framework's own phases."""
        if self.request.network.mode is NetworkMode.OPEN:
            return
        await self._set_egress({"networkBlockAll": False})

    async def close_egress(self) -> None:
        """Apply the Task's policy for the agent; Daytona enforces it outside the sandbox.

        ``block`` blocks all outbound traffic. ``allowlist`` is Daytona's own domain
        allowlist, so allowed traffic goes straight from the sandbox to its host and never
        through the Host; the Gateway alone comes through the tunnel.
        """
        policy = self.request.network
        if policy.mode is NetworkMode.OPEN:
            return
        if policy.mode is NetworkMode.ALLOWLIST:
            domains = _domain_allowlist(policy.allowed_hosts)
            await self._set_egress({"networkBlockAll": False, "domainAllowList": domains})
        else:
            await self._set_egress({"networkBlockAll": True})

    async def _set_egress(self, settings: dict[str, Any]) -> None:
        """Change the settings, then wait until the network behaves as they say.

        Daytona applies them asynchronously. Acting on the API's answer alone would start
        the agent, or a setup script, before the change reached the sandbox. The probe is a
        TLS handshake with a public host the Task did not allowlist, which completes only
        when the network is open.
        """
        data = await self._api.update_network(self.daytona_id, settings)
        stored = {key: data.get(key) for key in settings}
        if stored != settings:
            raise ProviderStartError(
                f"Daytona stored {stored}, not {settings}, on sandbox {self.daytona_id}"
            )
        want_open = settings == {"networkBlockAll": False}
        address, name = _egress_probe(self.request.network.allowed_hosts)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while await _handshakes(self._client, address, name) is not want_open:
            if loop.time() >= deadline:
                wanted = "open" if want_open else "closed"
                raise ProviderStartError(
                    f"the sandbox network did not become {wanted} within 60s of the change"
                )
            await asyncio.sleep(1.5)

    async def destroy(self) -> None:
        """Idempotent: teardown also runs on failure paths, sometimes twice."""
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED
        try:
            await self._disconnect()
            await self._api.delete_sandbox(self.daytona_id)
        finally:
            self._ssh.close()
            self.release_resources()

    async def _disconnect(self) -> None:
        for client in (self._client, self._file_client):
            with contextlib.suppress(Exception):
                await client.close()
        if self._tunnel is not None:
            with contextlib.suppress(Exception):
                await self._tunnel.close()

    async def retain(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        await self._disconnect()
        self._ssh.close()
        handle = f"{HANDLE_PREFIX}{self.daytona_id}"
        return RetainedSandbox(
            provider="daytona",
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"ale sandbox destroy {handle}",
            gpu_devices=(
                self.allocation.gpu.provider_addresses if self.allocation.gpu is not None else ()
            ),
        )


class DaytonaProvider(Provider):
    """Supplies Linux container sandboxes on Daytona."""

    name = "daytona"

    def __init__(self, config: DaytonaProviderConfig | None = None) -> None:
        self.config = config or DaytonaProviderConfig()
        self._api_client: DaytonaApi | None = None
        self._images_client: ImagePublisher | None = None
        # Local image inspection (python3, command) is the same check Docker makes.
        self._local = DockerProvider()

    @property
    def _api(self) -> DaytonaApi:
        if self._api_client is None:
            self._api_client = DaytonaApi.from_environment(self.config.api_key_env)
        return self._api_client

    @property
    def _images(self) -> ImagePublisher:
        if self._images_client is None:
            self._images_client = ImagePublisher(self._api)
        return self._images_client

    def capabilities(self) -> Capabilities:
        return Capabilities(
            operating_systems=frozenset({OperatingSystem.LINUX}),
            gui=True,  # depends on the image, as for Docker
            network_modes=frozenset({NetworkMode.BLOCK, NetworkMode.ALLOWLIST, NetworkMode.OPEN}),
        )

    def accepts(self, request: SandboxRequest) -> None:
        super().accepts(request)
        problems: list[str] = []
        if request.resources.memory_mb % 1024:
            problems.append(
                f"memory_mb={request.resources.memory_mb} is not a whole number of GiB, "
                "which is the unit Daytona allocates"
            )
        storage = request.resources.storage_mb
        if storage is not None and storage % 1024:
            problems.append(f"storage_mb={storage} is not a whole number of GiB")
        if request.network.mode is NetworkMode.ALLOWLIST:
            try:
                _domain_allowlist(request.network.allowed_hosts)
                _egress_probe(request.network.allowed_hosts)
            except ProviderCapabilityError as exc:
                problems.append(str(exc))
        if problems:
            raise ProviderCapabilityError("; ".join(problems))

    async def preflight(self) -> None:
        for tool, why in (("docker", "builds and pushes the image"), ("ssh", "reaches guestd")):
            if shutil.which(tool) is None:
                raise ProviderStartError(f"{tool} is not installed; Daytona needs it, it {why}")
        try:
            await self._api.request("GET", "/sandbox", params={"limit": 1})
        except DaytonaError as exc:
            if exc.status in {401, 403}:
                raise ProviderStartError(
                    f"Daytona rejected the API key in {self.config.api_key_env}: {exc.reason}"
                ) from exc
            raise

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if image.kind is not ImageKind.CONTAINER:
            raise ProviderCapabilityError("DaytonaProvider requires image.kind=container")
        prepared = await resolve_container_image(image) if isinstance(image, ImageRef) else image
        resolved = await resolve_prepared_container_image(prepared)
        await self._require_usable(prepared.runtime_ref, resolved.observed_ref)
        return prepared

    async def _require_usable(self, name: str, reference: str) -> None:
        if problems := await self._local.check_image(reference):
            raise ProviderCapabilityError(
                f"{name} cannot serve as a Daytona sandbox: {'; '.join(problems)}. "
                "See docs/specs/sandbox-image.md"
            )

    # --- create ----------------------------------------------------------------

    async def create(self, request: SandboxRequest) -> Sandbox:
        self.accepts(request)
        if request.image_kind is not ImageKind.CONTAINER:
            raise ProviderCapabilityError("DaytonaProvider requires image.kind=container")
        local = await resolve_prepared_container_image(request.prepared_image)
        framed = await with_framework(local.observed_identity, user=request.user, sudo=request.sudo)
        await self._require_usable(request.prepared_image.runtime_ref, framed)
        config = await image_config(framed)

        local_gateway, listeners, targets = _tunnel_plan(request)
        sandbox_id = f"{request.episode_id}-{uuid.uuid4().hex[:6]}"
        daytona_id: str | None = None
        ssh: _SshHost | None = None
        client: GuestClient | None = None
        file_client: GuestClient | None = None
        tunnel: _Tunnel | None = None
        try:
            published, daytona_id, data = await self._start(
                request, framed, sandbox_id, local_gateway
            )
            resolved = ResolvedImage(
                kind=ImageKind.CONTAINER,
                prepared_identity=request.prepared_image.prepared_identity,
                observed_identity=published.digest,
                observed_ref=published.ref,
            )
            allocation = _observed_allocation(request, data)
            ssh = _SshHost(self._api, daytona_id)
            environment = _guest_environment(config, request, local_gateway)
            client = await _connect(ssh, request.user, environment)
            agent_home, has_desktop = await _guest_contract(client)
            await _require_framework(client, request)
            await verify_identities(client, request.user, sudo=request.sudo)
            await _seal_daemon(client, request.user)
            if request.resources.gpus:
                gpus = await _observed_gpus(client, request, data, self.config.gpu_types)
                allocation = allocation.model_copy(update={"gpu": gpus})
            if listeners:
                tunnel = _Tunnel(ssh, listeners=listeners, targets=targets)
                await tunnel.start()
            await _start_image_command(client, config)
            if has_desktop:
                await _await_desktop(client)
            file_client = await _connect(ssh, request.user, environment)
        except BaseException:
            await asyncio.shield(
                _discard(
                    self._api, daytona_id, ssh=ssh, clients=(client, file_client), tunnel=tunnel
                )
            )
            raise
        return DaytonaSandbox(
            sandbox_id=sandbox_id,
            daytona_id=daytona_id,
            request=request,
            api=self._api,
            ssh=ssh,
            client=client,
            file_client=file_client,
            tunnel=tunnel,
            local_gateway=local_gateway,
            resolved_image=resolved,
            allocation=allocation,
            agent_user=request.user,
            agent_home=agent_home,
        )

    async def _start(
        self, request: SandboxRequest, framed: str, sandbox_id: str, local_gateway: str | None
    ) -> tuple[PublishedImage, str, dict[str, Any]]:
        """Publish the image and bring a sandbox up from it.

        Daytona can lose a pushed image, or its builder can hold stale registry
        credentials; both look like a failed pull. That is repaired once, by publishing
        again with new credentials.

        The caller learns the sandbox only from the return value, so every other way out,
        cancellation included, deletes what Daytona created.
        """
        for attempt in range(2):
            published = await self._images.publish(framed, force=attempt > 0)
            daytona_id = await _created(
                self._api, self._body(request, published, sandbox_id, local_gateway)
            )
            try:
                data = await self._api.wait_started(daytona_id, timeout_sec=_START_TIMEOUT_SEC)
                return published, daytona_id, data
            except BaseException as exc:
                await asyncio.shield(_discard(self._api, daytona_id))
                retry = isinstance(exc, ProviderStartError) and _image_unavailable(str(exc))
                if attempt == 0 and retry:
                    await self._images.refresh_access()
                    continue
                raise
        raise AssertionError("unreachable")

    def _body(
        self,
        request: SandboxRequest,
        published: PublishedImage,
        sandbox_id: str,
        local_gateway: str | None,
    ) -> dict[str, Any]:
        resources = request.resources
        retained = request.retention == "keep"
        hours = self.config.retained_lifetime_hours if retained else self.config.lifetime_hours
        body: dict[str, Any] = {
            "name": _sandbox_name(sandbox_id),
            "buildInfo": {"dockerfileContent": f"FROM {published.ref}\n"},
            "cpu": resources.cpus,
            "memory": resources.memory_mb // 1024,
            "target": self.config.target,
            "public": False,
            # Never stopped for idleness: an agent can think for a long time without a
            # call. The lifetime below is what bounds a sandbox nobody destroyed.
            "autoStopInterval": 0,
            "autoDeleteInterval": 0,
            "ttlMinutes": hours * 60,
            # Closed from the start, as a Docker sandbox on an internal network is.
            "networkBlockAll": request.network.mode is not NetworkMode.OPEN,
            "env": {**request.env, **({"ALE_GATEWAY_URL": local_gateway} if local_gateway else {})},
            "labels": {
                MANAGED_LABEL: "true",
                EPISODE_LABEL: request.episode_id,
                ROLE_LABEL: request.role.value,
                RETENTION_LABEL: request.retention,
            },
        }
        if resources.storage_mb is not None:
            body["disk"] = resources.storage_mb // 1024
        if resources.gpus:
            # Daytona places a GPU sandbox in its GPU region itself. Never spot: a spot
            # sandbox can be terminated without notice in the middle of an episode.
            body["gpu"] = resources.gpus
            body["spot"] = False
            if self.config.gpu_types:
                body["gpuType"] = list(self.config.gpu_types)
        return body

    # --- retained sandboxes ----------------------------------------------------

    async def list_retained(self) -> list[dict[str, object]]:
        if not os.environ.get(self.config.api_key_env):
            return []  # no Daytona credentials here, so nothing of this checkout's to list
        found = await self._api.list_sandboxes({MANAGED_LABEL: "true", RETENTION_LABEL: "keep"})
        records: list[dict[str, object]] = []
        for data in found:
            if data.get("state") in {"destroying", "destroyed"}:
                continue  # Daytona lists a sandbox for a while after it is deleted
            labels = data.get("labels") or {}
            handle = f"{HANDLE_PREFIX}{data['id']}"
            records.append(
                {
                    "provider": "daytona",
                    "handle": handle,
                    "episode": labels.get(EPISODE_LABEL, ""),
                    "role": labels.get(ROLE_LABEL, ""),
                    "image": _from_reference(data),
                    "gpus": (),
                    "running": data.get("state") == "started",
                    "cleanup_command": f"ale sandbox destroy {handle}",
                }
            )
        return sorted(records, key=lambda item: str(item["handle"]))

    async def destroy_retained(self, handle: str) -> None:
        if not handle.startswith(HANDLE_PREFIX):
            raise ProviderCapabilityError(f"{handle!r} is not a Daytona sandbox handle")
        daytona_id = handle.removeprefix(HANDLE_PREFIX)
        data = await self._api.get_sandbox(daytona_id)
        if data is None:
            raise ProviderStartError(f"sandbox {handle!r} does not exist")
        labels = data.get("labels") or {}
        if labels.get(MANAGED_LABEL) != "true" or labels.get(RETENTION_LABEL) != "keep":
            raise ProviderCapabilityError(f"{handle!r} is not an ALE-retained Daytona sandbox")
        await self._api.delete_sandbox(daytona_id)

    async def attach_retained(
        self, handle: str, request: SandboxRequest, allocation: ResourceAllocation
    ) -> Sandbox:
        """Reconnect to a running retained sandbox without taking ownership of it."""
        if not handle.startswith(HANDLE_PREFIX):
            raise ProviderCapabilityError(f"{handle!r} is not a Daytona sandbox handle")
        daytona_id = handle.removeprefix(HANDLE_PREFIX)
        data = await self._api.get_sandbox(daytona_id)
        if data is None:
            raise ProviderStartError(f"sandbox {handle!r} does not exist")
        labels = data.get("labels") or {}
        role = labels.get(ROLE_LABEL, "")
        if (
            labels.get(MANAGED_LABEL) != "true"
            or labels.get(RETENTION_LABEL) != "keep"
            or role not in {SandboxRole.SOLVER.value, SandboxRole.SHARED.value}
        ):
            raise ProviderCapabilityError(f"{handle!r} is not a retained ALE solver sandbox")
        if data.get("state") != "started":
            raise ProviderStartError(f"retained sandbox {handle!r} is not running")

        local = await resolve_prepared_container_image(request.prepared_image)
        framed = await with_framework(local.observed_identity, user=request.user, sudo=request.sudo)
        published = await self._images.lookup(framed)
        if published is None or _from_reference(data) != published.ref:
            raise ProviderCapabilityError(f"{handle!r} does not use the expected solver image")
        resolved = ResolvedImage(
            kind=ImageKind.CONTAINER,
            prepared_identity=request.prepared_image.prepared_identity,
            observed_identity=published.digest,
            observed_ref=published.ref,
        )
        attached = request.model_copy(update={"role": SandboxRole(role)})
        config = await image_config(framed)
        local_gateway, listeners, targets = _tunnel_plan(attached)
        ssh = _SshHost(self._api, daytona_id)
        client: GuestClient | None = None
        file_client: GuestClient | None = None
        tunnel: _Tunnel | None = None
        try:
            environment = _guest_environment(config, attached, local_gateway)
            client = await _connect(ssh, request.user, environment)
            agent_home, _ = await _guest_contract(client)
            await _require_framework(client, attached)
            await _seal_daemon(client, request.user)  # idempotent: the rules persist
            if listeners:
                tunnel = _Tunnel(ssh, listeners=listeners, targets=targets)
                await tunnel.start()
            file_client = await _connect(ssh, request.user, environment)
        except BaseException:
            # Not ours to delete: only the connections are undone.
            await asyncio.shield(
                _discard(self._api, None, ssh=ssh, clients=(client, file_client), tunnel=tunnel)
            )
            raise
        return DaytonaSandbox(
            sandbox_id=daytona_id,
            daytona_id=daytona_id,
            request=attached,
            api=self._api,
            ssh=ssh,
            client=client,
            file_client=file_client,
            tunnel=tunnel,
            local_gateway=local_gateway,
            resolved_image=resolved,
            allocation=allocation,
            agent_user=request.user,
            agent_home=agent_home,
        )


# --- steps shared by create and attach -------------------------------------------


async def _connect(
    ssh: _SshHost, agent_user: str, environment: dict[str, str], *, timeout_sec: float = 120
) -> GuestClient:
    """guestd over SSH, retrying while the sandbox's daemon is still starting.

    It runs with the environment ``docker exec`` would give it: the image's own, and the
    Host's. That is also how it learns whether the image has a desktop.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    assignments = " ".join(
        shlex.quote(f"{name}={value}")
        for name, value in {**environment, "ALE_AGENT_USER": agent_user}.items()
    )
    command = f"exec env {assignments} python3 {GUESTD_DIR / 'main.py'} --stdio"
    last: Exception | None = None
    while True:
        transport = StdioTransport(await ssh.argv(command))
        await transport.start()
        client = GuestClient(transport)
        try:
            await asyncio.wait_for(client.health(), timeout=30)
            return client
        except Exception as exc:
            last = exc
            await client.close()
        if loop.time() >= deadline:
            raise ProviderStartError(f"guest service did not answer over SSH: {last}")
        await asyncio.sleep(2)


async def _guest_contract(client: GuestClient) -> tuple[str, bool]:
    """The agent's home and whether the image runs a desktop, as the guest reports."""
    health = await client.health()
    home = str(health.get("agent_home") or "")
    if not home:
        raise ProviderCapabilityError("guestd did not report the agent account's home")
    return home, bool(health.get("gui"))


def _guest_environment(
    config: ImageConfig, request: SandboxRequest, local_gateway: str | None
) -> dict[str, str]:
    return {
        **config.env,
        **request.env,
        **({"ALE_GATEWAY_URL": local_gateway} if local_gateway else {}),
    }


async def _start_image_command(client: GuestClient, config: ImageConfig) -> None:
    """Run the image's entrypoint and command, which Daytona replaces with its daemon.

    Detached, as a container's main process is: it outlives this call, and the Provider
    does not wait for it or look at what it prints.
    """
    if not config.argv:
        return
    spec = {
        "argv": list(config.argv),
        "user": config.user,
        "workdir": config.workdir,
        "env": config.env,
        "log": f"{GUESTD_DIR.parent}/image-command.log",
    }
    code, _, stderr, _ = await client.exec(
        ["python3", "-c", _LAUNCH, json.dumps(spec)], timeout_sec=60
    )
    if code != 0:
        raise ProviderStartError(f"could not start the image's command: {stderr.strip()}")


async def _await_desktop(client: GuestClient, timeout_sec: float = 120) -> None:
    """Block until the screen can actually be captured, as the Docker Provider does."""
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


async def _require_framework(client: GuestClient, request: SandboxRequest) -> None:
    """The sandbox carries the framework this engine expects, not an older push."""
    expected = framework_identity(user=request.user, sudo=request.sudo, vm=False)
    code, stdout, stderr, _ = await client.exec(["cat", MARKER], timeout_sec=30)
    if code != 0 or stdout.strip() != expected:
        raise ProviderStartError(
            "the sandbox image does not carry this engine's framework layer "
            f"(found {stdout.strip() or stderr.strip() or 'none'}, expected {expected})"
        )


async def _observed_gpus(
    client: GuestClient,
    request: SandboxRequest,
    data: dict[str, Any],
    gpu_types: tuple[str, ...],
) -> GpuAllocation:
    """The GPUs as the agent sees them, which is what the RunLock records.

    Read as the agent, so a GPU the agent cannot open stops the sandbox here rather than in
    the middle of its task. Daytona's GPU types are NVIDIA's except MI355X, which
    ``nvidia-smi`` cannot see and so cannot be recorded.
    """
    code, stdout, stderr, _ = await client.exec(
        ["nvidia-smi", f"--query-gpu={_NVIDIA_QUERY}", "--format=csv,noheader,nounits"],
        run_as=request.user,
        timeout_sec=60,
    )
    if code != 0:
        raise ProviderCapabilityError(
            f"the agent cannot use the sandbox's GPUs: {(stderr or stdout).strip()[-500:]}"
        )
    devices = parse_nvidia_smi(stdout)
    wanted = request.resources.gpus
    if len(devices) != wanted:
        raise ProviderStartError(
            f"Daytona attached {len(devices)} GPUs, not the {wanted} requested"
        )
    foreign = [
        device.model
        for device in devices
        if gpu_types and not any(_same_model(kind, device.model) for kind in gpu_types)
    ]
    if foreign:
        raise ProviderStartError(
            f"Daytona attached {', '.join(foreign)}, which gpu_types {list(gpu_types)} excludes"
        )
    # Daytona holds the GPUs for the sandbox's life, so the sandbox is the lease.
    version = data.get("daemonVersion")
    return GpuAllocation(
        requested_count=wanted,
        provider_addresses=tuple(device.id for device in devices),
        lease_keys=tuple(f"{HANDLE_PREFIX}{data['id']}/{device.id}" for device in devices),
        observed_devices=devices,
        runtime_identity=f"daytona-daemon/{version}" if version else None,
    )


def _same_model(kind: str, model: str) -> bool:
    """Whether Daytona's type name (``RTX-PRO-6000``) names the model nvidia-smi reports
    (``NVIDIA RTX PRO 6000 Blackwell Server Edition``)."""

    def squeeze(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    return squeeze(kind) in squeeze(model)


async def _seal_daemon(client: GuestClient, agent_user: str) -> None:
    """Keep every non-root user away from Daytona's daemon, and prove it from the agent."""
    code, stdout, stderr, _ = await client.exec(
        ["python3", "-c", _SEAL, ",".join(map(str, DAEMON_PORTS))], timeout_sec=60
    )
    if code != 0:
        raise ProviderCapabilityError(
            f"could not restrict access to Daytona's daemon in this sandbox: {stderr.strip()}"
        )
    ports = json.loads(stdout)
    code, stdout, stderr, _ = await client.exec(
        ["python3", "-c", _PROBE, ",".join(map(str, ports))],
        run_as=agent_user,
        timeout_sec=120,
    )
    try:
        reached = json.loads(stdout)
    except ValueError:
        reached = None
    if code != 0 or reached is None:
        raise ProviderStartError(f"could not probe the daemon ports as {agent_user}: {stderr}")
    if reached:
        raise ProviderCapabilityError(
            f"{agent_user} can still reach Daytona's daemon at {', '.join(reached)}; "
            "a sandbox where the agent can run commands as root is not handed out"
        )


async def _handshakes(client: GuestClient, address: str, name: str) -> bool:
    _, stdout, _, _ = await client.exec(
        ["python3", "-c", _HANDSHAKE, address, name, "3"], timeout_sec=30
    )
    return stdout.strip() == "open"


async def _created(api: DaytonaApi, body: dict[str, Any]) -> str:
    """Create a sandbox and return its id, or delete it when the id cannot be returned.

    A create that is cancelled, or whose reply is lost, may still have made the sandbox.
    """
    creating = asyncio.ensure_future(api.create_sandbox(body))
    try:
        created = await asyncio.shield(creating)
    except BaseException:
        await asyncio.shield(_reclaim(api, creating, str(body["name"])))
        raise
    return str(created["id"])


async def _reclaim(api: DaytonaApi, creating: asyncio.Future[dict[str, Any]], name: str) -> None:
    """Delete what an unfinished or failed create made; never raises.

    The reply names the sandbox. Without one, the name it was created under is unique to
    this sandbox, so it is looked up by that.
    """
    await asyncio.wait({creating}, timeout=_CREATE_REPLY_SEC)
    created: str | None = None
    if creating.done() and not creating.cancelled() and creating.exception() is None:
        created = str(creating.result()["id"])
    else:
        creating.cancel()
        with contextlib.suppress(Exception):
            found = await api.get_sandbox(name)
            created = None if found is None else str(found["id"])
    if created is not None:
        await _discard(api, created)


async def _discard(
    api: DaytonaApi,
    daytona_id: str | None,
    *,
    ssh: _SshHost | None = None,
    clients: Sequence[GuestClient | None] = (),
    tunnel: _Tunnel | None = None,
) -> None:
    """Undo a failed start, leaving nothing behind and never raising."""
    if tunnel is not None:
        with contextlib.suppress(Exception):
            await tunnel.close()
    for client in clients:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
    if daytona_id is not None:
        with contextlib.suppress(Exception):
            await api.delete_sandbox(daytona_id)
    if ssh is not None:
        ssh.close()


# --- pure helpers -------------------------------------------------------------------


def _tunnel_plan(
    request: SandboxRequest,
) -> tuple[str | None, list[tuple[int, int]], list[tuple[str, int]]]:
    """What the tunnel carries: the Gateway's URL on the sandbox's loopback, at the port the
    Host chose, the port to listen on, and the Host address behind it.

    Only the Gateway. The Host's egress proxy is not used: allowlisted hosts are reached
    directly, under Daytona's own allowlist.
    """
    url = request.gateway_url
    if not url:
        return None, [], []
    parts = urlsplit(url)
    if parts.port is None:
        raise ProviderCapabilityError(f"{url!r} has no explicit port to tunnel")
    host = parts.hostname or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    local = parts._replace(netloc=f"127.0.0.1:{parts.port}").geturl()
    return local, [(parts.port, 0)], [(host, parts.port)]


def _domain_allowlist(hosts: Sequence[str]) -> str:
    """The Task's allowed hosts as Daytona's domain allowlist.

    ALE allows a declared name and its subdomains, which Daytona writes ``*.name``. Daytona
    matches names, as ALE does, but takes no address, port or pattern, and at most
    ``_DOMAIN_LIMIT`` entries; a Task that needs those is refused rather than widened.
    """
    names = sorted({host.lower().rstrip(".") for host in hosts})
    unusable = [name for name in names if not _DNS_NAME.fullmatch(name)]
    if unusable:
        raise ProviderCapabilityError(
            f"Daytona's allowlist takes domain names; {', '.join(unusable)} is not one"
        )
    if len(names) > _DOMAIN_LIMIT:
        raise ProviderCapabilityError(
            f"Daytona's allowlist takes at most {_DOMAIN_LIMIT} domains, not {len(names)}"
        )
    return ",".join(f"*.{name}" for name in names)


def _egress_probe(allowed_hosts: Sequence[str]) -> tuple[str, str]:
    """A public TLS endpoint whose name the allowlist does not cover, to probe egress with."""
    allowed = [host.lower().rstrip(".") for host in allowed_hosts]
    for address, name in _EGRESS_PROBES:
        if not any(name == host or name.endswith(f".{host}") for host in allowed):
            return address, name
    raise ProviderCapabilityError(
        "the allowlist covers every endpoint the Provider checks egress with: "
        + ", ".join(name for _, name in _EGRESS_PROBES)
    )


def _observed_allocation(request: SandboxRequest, data: dict[str, Any]) -> ResourceAllocation:
    """What Daytona gave, not what was asked; any difference stops the sandbox."""
    resources = request.resources
    got = (int(data["cpu"]), int(data["memory"]) * 1024, int(data["disk"]) * 1024)
    wanted_storage = resources.storage_mb
    if (
        got[0] != resources.cpus
        or got[1] != resources.memory_mb
        or (wanted_storage is not None and got[2] != wanted_storage)
    ):
        raise ProviderStartError(
            f"Daytona gave {got[0]} CPU, {got[1]} MB memory and {got[2]} MB disk, not the "
            f"{resources.cpus} CPU and {resources.memory_mb} MB requested"
        )
    if int(data.get("gpu") or 0) != resources.gpus:
        raise ProviderStartError(
            f"Daytona gave {data.get('gpu') or 0} GPUs, not the {resources.gpus} requested"
        )
    return ResourceAllocation(
        cpus=got[0],
        memory_mb=got[1],
        storage_mb=got[2],
        sudo=request.sudo,
        network_mode=request.network.mode,
        provider="daytona",
    )


def _sandbox_name(sandbox_id: str) -> str:
    return ("ale-" + re.sub(r"[^a-z0-9-]+", "-", sandbox_id.lower())).strip("-")[:60]


def _parse_ssh(command: str) -> tuple[str, str, str]:
    """User (the access token), host and port from the ``ssh`` command Daytona returns."""
    parts = shlex.split(command)
    target = next((part for part in parts if "@" in part), "")
    user, _, host = target.partition("@")
    port = parts[parts.index("-p") + 1] if "-p" in parts else "22"
    if not user or not host:
        raise ProviderStartError("Daytona returned an SSH command this engine cannot read")
    return user, host, port


def _from_reference(data: dict[str, Any]) -> str:
    content = str((data.get("buildInfo") or {}).get("dockerfileContent") or "").strip()
    return content.removeprefix("FROM").strip() if content.startswith("FROM") else ""


def _image_unavailable(message: str) -> bool:
    return bool(
        re.search(
            r"401|403|unauthorized|manifest unknown|not found|failed to resolve source metadata"
            r"|pull access denied",
            message,
            re.IGNORECASE,
        )
    )
