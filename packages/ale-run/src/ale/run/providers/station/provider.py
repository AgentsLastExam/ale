r"""Station Provider: Windows Sandboxes in the Lanes of a machine its operator keeps.

A Station is a Windows Server machine with licensed software installed for every account,
which ``ale-stationd`` (``stationd.py``) serves. Each Lane is an account with a Remote
Desktop session of its own and serves one Sandbox at a time. This leaf works with what the
Station gives:

* **Requests.** Every request is one ``ssh -T`` session to the Station's ``ale-host``
  account, whose keys run only ``ale-stationd``'s client; the request is the session's
  command, and the reply's first line is JSON.
* **Image.** A Task names software; ``prepare_image`` resolves the ids against the
  Station's manifest, and the prepared identity is the hash of their entries. Nothing is
  built or framed.
* **Framework.** One framework serves every Lane, in the Station's ``C:\ProgramData\ALE``.
  The preflight installs this engine's when the Station runs another, which it can only
  while no Lane is leased, and refuses an ``ale-stationd`` other than this engine's copy.
* **Lease.** ``create`` leases a Lane whose session is active at the Run's resolution,
  asking again while the Station is busy for up to ``lane_wait_sec``. The token names the
  Lane and its generation, which the RunLock records with the Lane's baseline.
* **Channel.** guestd runs as SYSTEM in the Lane's session over two SSH sessions, one for
  commands and one for file reads, and runs the agent's commands with the Lane's limited
  token. A third carries ``guestd/tunnel.py``, which delivers the Gateway at port 7412 of
  the lease's own loopback address.
* **Files.** Each Lane stages a Task's files in a stage root of its own, which only the
  framework opens, and every file the Provider uploads or writes for the agent admits no
  other account, since every Lane may open by its path a file that admits it.
* **Network.** Lanes share the Station's network stack, so a Lane's network is open and its
  profile applies ``open`` alone.
* **Release.** ``destroy`` closes the sessions and releases the lease; the Station then
  resets the Lane to its baseline and checks it before it leases it again, or takes it out
  of service. A release that does not arrive ends the lease 120 s after its last session.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import shutil
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from ale.core.capability import Arch, CapabilityProfile, SandboxNeeds
from ale.core.config import StationProviderConfig
from ale.core.errors import (
    GuestUnreachableError,
    ProviderCapabilityError,
    ProviderCapacityError,
    ProviderStartError,
)
from ale.core.ids import content_hash
from ale.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
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
    Sandbox,
    SandboxFile,
    SandboxRequest,
    SandboxRole,
    SandboxState,
)
from ale.core.taskspec import NetworkMode, OperatingSystem
from ale.run.framework import station_framework_identity, station_framework_payload
from ale.run.providers.desktop import require_resolution
from ale.run.providers.privileges import reset_windows_acl, verify_windows_identities
from ale.run.providers.station import stationd
from ale.run.providers.tunnel import Tunnel, target
from ale.run.transport import GuestClient, StdioTransport

__all__ = ["StationProvider", "StationSandbox"]

#: The copy of ``ale-stationd`` this engine ships, which a Station must run.
STATIOND = Path(stationd.__file__)
#: What every request's SSH session is: a session of its own, which asks nothing and
#: forwards nothing, so that one dropped connection takes one request with it.
_SSH = (
    "ssh",
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ControlPath=none",
    "-o", "ClearAllForwardings=yes",
    "-o", "ForwardAgent=no",
    "-o", "ForwardX11=no",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
    "-o", "LogLevel=ERROR",
)  # fmt: skip
#: How long a request other than a lease or a framework push may take.
_REQUEST_SEC = 60.0
#: A lease runs the software's probes and waits on the Station for a Lane; its request
#: waits as long as both may take, and a minute more.
_LEASE_SEC = stationd.PROBE_TIMEOUT_SEC + stationd.LEASE_WAIT_SEC + 60
#: A framework push sends the framework's Python and Cua Driver and unpacks them.
_FRAMEWORK_SEC = 1800.0
#: How long to wait before asking again while a Station is busy, or pushing a framework.
_LEASE_RETRY_SEC = 5.0
_FRAMEWORK_RETRY_SEC = 10.0
#: How long guestd may take to answer in a Lane, and the desktop to give a screenshot.
_GUESTD_SEC = 120.0
_DESKTOP_SEC = 120.0
_SOFTWARE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class _Refused(Exception):
    """A request ``ale-stationd`` answered with ``{"ok": false}``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _Stationd:
    """The Host's end of ``ale-stationd``: each request is one SSH session."""

    def __init__(self, destination: str) -> None:
        self.destination = destination

    def argv(self, request: str) -> list[str]:
        """The command whose standard streams carry ``request`` and what follows it."""
        return [*_SSH, self.destination, request]

    async def call(
        self, request: str, *, timeout_sec: float, payload: bytes | None = None
    ) -> dict[str, Any]:
        """The reply to a request that is answered with one line, after ``payload``.

        The request may hold a lease's token, so errors name only its operation.
        """
        operation = request.split(maxsplit=1)[0]
        process = await asyncio.create_subprocess_exec(
            *self.argv(request),
            stdin=asyncio.subprocess.DEVNULL if payload is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(payload), timeout_sec)
        except TimeoutError:
            raise ProviderStartError(
                f"ale-stationd at {self.destination} did not answer {operation} within "
                f"{timeout_sec:g}s"
            ) from None
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                # Reaped even when cancelled, so no process outlives its event loop.
                await asyncio.shield(process.wait())
        try:
            reply = json.loads(stdout.partition(b"\n")[0])
        except ValueError:
            reply = None
        if not isinstance(reply, dict):
            detail = stderr.decode("utf-8", "replace").strip()[-500:]
            raise ProviderStartError(
                f"ale-stationd at {self.destination} did not answer {operation}: "
                f"{detail or f'ssh exited with {process.returncode}'}"
            )
        if not reply.get("ok"):
            raise _Refused(str(reply.get("error")), str(reply.get("message")))
        return reply


@dataclass(frozen=True)
class _Lease:
    """What ``ale-stationd`` answered a lease with."""

    token: str
    lane: str
    user: str
    home: str
    generation: int
    address: str
    stage: str
    baseline: str

    @classmethod
    def read(cls, reply: dict[str, Any]) -> _Lease:
        try:
            lease = cls(
                token=str(reply["token"]),
                lane=str(reply["lane"]),
                user=str(reply["user"]),
                home=str(reply["home"]),
                generation=int(reply["generation"]),
                address=str(reply["address"]),
                stage=str(reply["stage"]),
                baseline=str(reply["baseline"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderStartError(f"ale-stationd answered a lease without {error}") from error
        if not all((lease.lane, lease.user, lease.home, lease.address, lease.stage)):
            raise ProviderStartError("ale-stationd answered a lease with an empty field")
        return lease


class StationSandbox(Sandbox):
    """One Lane of a Station, driven through guestd over SSH while its lease lasts."""

    def __init__(
        self,
        *,
        sandbox_id: str,
        request: SandboxRequest,
        daemon: _Stationd,
        lease: _Lease,
        client: GuestClient,
        file_client: GuestClient,
        tunnel: Tunnel | None,
        local_gateway: str | None,
        resolved_image: ResolvedImage,
        allocation: ResourceAllocation,
    ) -> None:
        super().__init__(
            sandbox_id=sandbox_id,
            request=request,
            resolved_image=resolved_image,
            allocation=allocation,
            agent_user=lease.user,
            agent_home=lease.home,
        )
        self.lane = lease.lane
        self._stationd = daemon
        self._lease = lease
        self._client = client
        self._file_client = file_client
        self._tunnel = tunnel
        self._local_gateway = local_gateway
        #: What every command sees, as a container's process environment would hold it.
        self._environment = {
            **request.env,
            **({"ALE_GATEWAY_URL": local_gateway} if local_gateway else {}),
        }
        self.state = SandboxState.READY

    def _as(self, identity: Identity) -> str | None:
        return self.agent_user if identity is Identity.AGENT else None

    @property
    def gateway_url(self) -> str | None:
        """The Gateway at the lease's own loopback address, where the tunnel delivers it."""
        return self._local_gateway

    @property
    def stage_root(self) -> str:
        """The Lane's own stage root: every Lane shares ``C:\\ProgramData\\ALE``."""
        return self._lease.stage

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
            env=self._environment | (env or {}),
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
        if identity is Identity.AGENT:
            # Whatever access its directory gives: no other Lane may open it.
            await reset_windows_acl(self._client, str(path), agent=self.agent_user, private=True)

    # File reads have their own guestd session, so they proceed while a command runs.

    async def read_file(self, path: str, *, offset: int = 0, length: int | None = None) -> bytes:
        return await self._file_client.read_file(str(path), offset=offset, length=length)

    async def list_files(self, path: str) -> tuple[SandboxFile, ...]:
        return await self._file_client.list_files(path)

    async def file_digest(self, path: str, *, length: int) -> str:
        return await self._file_client.file_digest(path, length=length)

    async def upload_dir(
        self, source: str, target: str, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        run_as = self._as(identity)
        await self._client.upload_dir(source, str(target), run_as=run_as)
        # Users may not read it: they are the other Lanes, which open any file that admits
        # them by its path.
        await reset_windows_acl(self._client, str(target), agent=run_as, private=True)

    async def screenshot(self) -> bytes:
        return await self._client.screenshot()

    async def inject_input(self, actions: Sequence[object]) -> int:
        payload = [
            action if isinstance(action, dict) else action.model_dump(mode="json")  # type: ignore[union-attr]
            for action in actions
        ]
        return await self._client.inject_input(payload)

    async def open_egress(self) -> None:
        """A Lane's network is the Station's, open throughout."""

    async def close_egress(self) -> None:
        """Lanes share the Station's network stack, which nothing seals for one of them."""
        if self.request.network.mode is not NetworkMode.OPEN:
            raise ProviderCapabilityError(
                "a Station's Lanes cannot be sealed; it serves open Tasks"
            )

    async def destroy(self) -> None:
        """Idempotent; never raises once the Sandbox is gone from this process. The Station
        resets the Lane after the release, or after the lease's grace when none arrives."""
        if self.state is SandboxState.DESTROYED:
            return
        self.state = SandboxState.DESTROYED
        try:
            await _discard(
                self._stationd, self._lease, (self._client, self._file_client), self._tunnel
            )
        finally:
            self.release_resources()


class StationProvider(Provider):
    """Leases the Lanes of the Station ``config.destination`` names."""

    name = "station"

    def __init__(
        self, config: StationProviderConfig, desktop_resolution: DesktopResolution
    ) -> None:
        self.config = config
        #: The Run's resolution, which every Lane of the Station must have.
        self.desktop_resolution = desktop_resolution
        #: The machine, as the SSH destination names it: what the RunLock calls it.
        self.station = config.destination.rpartition("@")[2]
        self._stationd = _Stationd(config.destination)
        self._software: dict[str, dict[str, str]] | None = None

    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        return (
            CapabilityProfile(
                provider=self.id,
                kind=ImageKind.STATION,
                os=OperatingSystem.WINDOWS,
                arch=Arch.X86_64,
                gui=True,
                network_modes=frozenset({NetworkMode.OPEN}),
            ),
        )

    def refusals(self, needs: SandboxNeeds, *, user: str, role: SandboxRole) -> tuple[str, ...]:
        problems: list[str] = []
        if role is not SandboxRole.SOLVER:
            problems.append(
                f"a Station runs a Task's solver alone, not a {role} Sandbox: verification "
                "never runs on a machine its operator keeps"
            )
        if needs.resources.sudo:
            problems.append("a Lane's agent never elevates, and the Task asks for [agent].sudo")
        return tuple(problems)

    async def preflight(self, needs: Sequence[SandboxNeeds] = ()) -> None:
        """The Station runs this engine's ``ale-stationd`` and framework, at the Run's
        resolution, with a Lane in service; its framework is installed when it differs."""
        if shutil.which("ssh") is None:
            raise ProviderStartError(
                "ssh is not installed; the station Provider reaches its Station with it"
            )
        hello = await self._hello()
        self._check(hello)
        identity = station_framework_identity()
        if hello.get("framework") != identity:
            await self._install_framework(identity)

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if image.kind is not ImageKind.STATION:
            raise ProviderCapabilityError(
                f"the station Provider serves software a Station has, not a {image.kind} image"
            )
        reference = image.reference if isinstance(image, ImageRef) else image.runtime_ref
        if self._software is None:
            software = (await self._hello()).get("software")
            if not isinstance(software, dict):
                raise ProviderStartError("ale-stationd's hello names no software manifest")
            self._software = software
        prepared = self._resolve(reference, self._software)
        if isinstance(image, PreparedTaskImage) and image != prepared:
            raise ProviderCapabilityError(
                f"the Station's manifest changed since {reference} was prepared on it"
            )
        return prepared

    async def create(self, request: SandboxRequest) -> Sandbox:
        await self.accepts(request)
        lease = await self._lease(request)
        clients: list[GuestClient] = []
        tunnel: Tunnel | None = None
        try:
            local_gateway, targets = _gateway(request, lease.address)
            if targets:
                command = f"tunnel {lease.token} {SANDBOX_GATEWAY_PORT}:0"

                async def tunnel_argv() -> list[str]:
                    return self._stationd.argv(command)

                tunnel = Tunnel(tunnel_argv, targets=targets)
                await tunnel.start()
            client = await _connect(self._stationd, lease)
            clients.append(client)
            file_client = await _connect(self._stationd, lease)
            clients.append(file_client)
            await verify_windows_identities(client, lease.user, sudo=False)
            await _await_desktop(client)
            resolution = await require_resolution(client, request.desktop_resolution)
        except BaseException:
            await asyncio.shield(_discard(self._stationd, lease, clients, tunnel))
            raise
        return StationSandbox(
            sandbox_id=f"{request.episode_id}-{uuid.uuid4().hex[:6]}",
            request=request,
            daemon=self._stationd,
            lease=lease,
            client=client,
            file_client=file_client,
            tunnel=tunnel,
            local_gateway=local_gateway,
            resolved_image=ResolvedImage(
                kind=ImageKind.STATION,
                prepared_identity=request.prepared_image.prepared_identity,
                observed_identity=content_hash(
                    {
                        "software": request.prepared_image.prepared_identity,
                        "baseline": lease.baseline,
                    }
                ),
                observed_ref=f"station:{self.station}/{lease.lane}@{lease.generation}",
            ),
            allocation=ResourceAllocation(
                # As asked, and not limited: a Lane's processes share the Station's CPUs and
                # memory with the other Lanes'.
                cpus=request.resources.cpus,
                memory_mb=request.resources.memory_mb,
                sudo=False,
                network_mode=request.network.mode,
                provider=self.id,
                leaf=self.id,
                desktop_resolution=resolution,
            ),
        )

    # --- inside ---

    async def _hello(self) -> dict[str, Any]:
        try:
            return await self._stationd.call("hello", timeout_sec=_REQUEST_SEC)
        except _Refused as refused:
            raise ProviderStartError(f"ale-stationd refused hello: {refused}") from refused

    def _check(self, hello: dict[str, Any]) -> None:
        """Refuse a Station this engine cannot drive, with what to do about it."""
        where = self.config.destination
        if hello.get("protocol") != stationd.PROTOCOL:
            raise ProviderStartError(
                f"the Station at {where} speaks ale-stationd protocol {hello.get('protocol')}, "
                f"not this engine's {stationd.PROTOCOL}; run scripts/setup_station.py from "
                "this checkout"
            )
        if hello.get("stationd") != hashlib.sha256(STATIOND.read_bytes()).hexdigest():
            raise ProviderStartError(
                f"the Station at {where} runs another ale-stationd than this engine's; run "
                "scripts/setup_station.py from this checkout to install it"
            )
        if hello.get("resolution") != str(self.desktop_resolution):
            raise ProviderCapabilityError(
                f"the Station's Lanes are {hello.get('resolution')}, not the Run's "
                f"desktop_resolution {self.desktop_resolution}; set desktop_resolution to "
                "theirs, which their keep-alive connections decide"
            )
        lanes = hello.get("lanes") or []
        if not any(lane.get("state") != stationd.QUARANTINED for lane in lanes):
            raise ProviderStartError(
                f"no Lane of the Station at {where} is in service; "
                f"`ssh -T {where} status` says why each is quarantined"
            )

    async def _install_framework(self, identity: str) -> None:
        """Push this engine's framework, waiting while another push runs."""
        payload = await asyncio.to_thread(station_framework_payload)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _FRAMEWORK_SEC
        while True:
            try:
                await self._stationd.call(
                    f"framework {identity} {len(payload)}",
                    payload=payload,
                    timeout_sec=_FRAMEWORK_SEC,
                )
                return
            except _Refused as refused:
                if refused.code != "framework-busy" or loop.time() > deadline:
                    raise ProviderStartError(
                        f"the Station at {self.config.destination} runs another framework than "
                        f"this engine's, and did not take this one's: {refused.message}"
                    ) from refused
            await asyncio.sleep(_FRAMEWORK_RETRY_SEC)
            if (await self._hello()).get("framework") == identity:
                return  # another Run of this engine pushed it

    def _resolve(self, reference: str, software: dict[str, dict[str, str]]) -> PreparedTaskImage:
        ids = reference.split(",")
        if ids != sorted(set(ids)) or not all(_SOFTWARE.fullmatch(name) for name in ids):
            raise ProviderCapabilityError(
                f"{reference!r} is not software ids, sorted and joined by commas"
            )
        if unknown := [name for name in ids if name not in software]:
            offered = ", ".join(sorted(software)) or "none"
            raise ProviderCapabilityError(
                f"the Station at {self.config.destination} has no {', '.join(unknown)}; "
                f"its manifest offers {offered}"
            )
        return PreparedTaskImage(
            kind=ImageKind.STATION,
            source="external-ref",
            input_identity=content_hash({"software": ids}),
            runtime_ref=reference,
            prepared_identity=content_hash({"software": {name: software[name] for name in ids}}),
            resolved_reference=f"station:{self.station}",
        )

    async def _lease(self, request: SandboxRequest) -> _Lease:
        """A Lane, asking again while the Station has none free, for up to ``lane_wait_sec``."""
        software = request.prepared_image.runtime_ref.split(",")
        command = " ".join(
            ("lease", station_framework_identity(), str(request.desktop_resolution), *software)
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.lane_wait_sec
        while True:
            try:
                return _Lease.read(await self._stationd.call(command, timeout_sec=_LEASE_SEC))
            except _Refused as refused:
                if refused.code not in ("busy", "framework-busy"):
                    raise _refusal(refused) from refused
                if loop.time() >= deadline:
                    raise ProviderCapacityError(
                        f"no Lane of the Station at {self.config.destination} came free within "
                        f"sandbox.station.lane_wait_sec ({self.config.lane_wait_sec}s): "
                        f"{refused.message}"
                    ) from refused
            # A busy Station has already waited for a Lane; a push in progress has not.
            await asyncio.sleep(_LEASE_RETRY_SEC)


# --- steps of create and destroy ---------------------------------------------------


def _refusal(refused: _Refused) -> Exception:
    """What a lease refusal other than a busy Station raises."""
    if refused.code in ("unknown-software", "resolution"):
        return ProviderCapabilityError(f"the Station refused the lease: {refused.message}")
    if refused.code == "framework-mismatch":
        return ProviderStartError(
            f"{refused.message}, not this engine's: another Run installed its own after this "
            "Run's preflight installed this one, and a Run installs its framework only then"
        )
    # A failed probe means the Station cannot run the software now: an infrastructure error.
    return ProviderStartError(f"the Station refused the lease: {refused}")


def _gateway(request: SandboxRequest, address: str) -> tuple[str | None, list[tuple[str, int]]]:
    """The Gateway's URL in the Lane, at its loopback address and the fixed port, and the Host
    address the tunnel dials for it."""
    if not request.gateway_url:
        return None, []
    local = urlsplit(request.gateway_url)._replace(netloc=f"{address}:{SANDBOX_GATEWAY_PORT}")
    return local.geturl(), [target(request.gateway_url)]


async def _connect(daemon: _Stationd, lease: _Lease) -> GuestClient:
    """A guestd session in the Lane, once it says it serves the Lane's account and desktop."""
    transport = StdioTransport(daemon.argv(f"guestd {lease.token}"))
    await transport.start()
    client = GuestClient(transport)
    try:
        health = await asyncio.wait_for(client.health(), timeout=_GUESTD_SEC)
    except BaseException as error:
        await client.close()
        if isinstance(error, (GuestUnreachableError, TimeoutError)):
            raise ProviderStartError(f"guestd did not answer in {lease.lane}: {error}") from error
        raise
    served = (
        health.get("os"),
        str(health.get("agent_user") or "").casefold(),
        PureWindowsPath(str(health.get("agent_home") or "")),
        health.get("gui"),
    )
    if served != ("windows", lease.user.casefold(), PureWindowsPath(lease.home), True):
        await client.close()
        raise ProviderStartError(
            f"guestd in {lease.lane} serves {served[1] or 'no account'} at {served[2]} "
            f"(desktop: {served[3]}), not {lease.user} at {lease.home} with its desktop"
        )
    return client


async def _await_desktop(client: GuestClient) -> None:
    """Block until the Lane's desktop gives a screenshot through its Cua Driver."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _DESKTOP_SEC
    while True:
        try:
            await client.screenshot()
            return
        except GuestUnreachableError as error:
            if loop.time() >= deadline:
                raise ProviderStartError(
                    f"the Lane's desktop gave no screenshot within {_DESKTOP_SEC:g}s: {error}"
                ) from error
        await asyncio.sleep(1.0)


async def _discard(
    daemon: _Stationd,
    lease: _Lease,
    clients: Sequence[GuestClient],
    tunnel: Tunnel | None,
) -> None:
    """Close the sessions and release the lease, never raising: a lease whose release did
    not arrive ends ``stationd.GRACE_SEC`` after its last session closed."""
    for client in clients:
        with contextlib.suppress(Exception):
            await client.close()
    if tunnel is not None:
        with contextlib.suppress(Exception):
            await tunnel.close()
    try:
        await daemon.call(f"release {lease.token}", timeout_sec=_REQUEST_SEC)
    except Exception as error:
        logging.getLogger("ale.execution").warning(
            "could not release Lane %s; the Station ends its lease %gs after its last session: %s",
            lease.lane,
            stationd.GRACE_SEC,
            error,
        )
