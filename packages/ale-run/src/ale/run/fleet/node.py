"""The node agent: one process per Node that lends the machine's Providers to the Fleet.

It probes what the machine's own Providers can start, registers that with the control
plane, and sends a heartbeat with its full roster every few seconds. A Run that holds a
grant from the control plane creates its Sandbox here, then drives it through this
agent's API, which calls the very Provider and Sandbox objects a local Run would, so
every guarantee those give holds unchanged.

Each Sandbox has a lease its Run renews here. The agent destroys a Sandbox whose lease ran
out, so a Run that crashed or lost its way here leaves nothing behind, whatever the
control plane is doing. When the agent itself stops it destroys every Sandbox it holds: an
operator stops an agent to take its Node out of service, so it leaves nothing behind; a
crash, which nobody chose, keeps them for their Runs.

An agent that crashes leaves its Sandboxes running. It journals each one it holds, so the
agent that starts next on this machine reattaches, through the Provider, those whose lease
still runs and holds them again for their Runs, and destroys the rest.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from pydantic import BaseModel, ConfigDict, ValidationError

from ale.core.errors import (
    AleError,
    ProviderCapabilityError,
    ProviderStartError,
)
from ale.core.sandbox import (
    Identity,
    Provider,
    Sandbox,
    SandboxRequest,
)
from ale.core.taskspec import ImageKind, OperatingSystem
from ale.run import __version__
from ale.run.fleet.images import NodeImages
from ale.run.fleet.placement import Holds, NodeView, choose
from ale.run.fleet.protocol import (
    PROTOCOL_VERSION,
    STDERR,
    STDOUT,
    Arch,
    CreateSandbox,
    ErrorBody,
    ExecCall,
    ExecDone,
    GpuPool,
    Grant,
    Heartbeat,
    HeartbeatReply,
    ImageInputs,
    ImageState,
    LeaseRenewal,
    Listing,
    NodeDeclaration,
    NodeResources,
    Platform,
    RosterEntry,
    SandboxDemand,
    SandboxDescription,
    SandboxProfile,
    verify_grant,
)
from ale.run.fleet.tunnel import NodeListeners, with_port
from ale.run.framework import fleet_framework_identity
from ale.run.provenance import framework_provenance

__all__ = ["NodeAgent", "NodeSettings", "probe"]

_log = logging.getLogger(__name__)

HEARTBEAT_SEC = 5.0
_KEY = re.compile(r"sha256:[0-9a-f]{64}")
LEASE_CHECK_SEC = 1.0
#: The longest a restarted agent spends driving one Sandbox again before it destroys it
#: instead, well under the 90 seconds a Run waits for its Node to take connections again.
RECOVER_SEC = 60.0

#: What each Provider a node agent can drive starts, and on which architecture: ``None``
#: is the machine's own.
KINDS: dict[str, tuple[ImageKind, Arch | None]] = {
    "docker": (ImageKind.CONTAINER, None),
    "qemu": (ImageKind.VM, Arch.X86_64),
    "orchard": (ImageKind.VM, Arch.ARM64),
}


def _provider(name: str) -> Provider:
    if name == "docker":
        from ale.run.providers.docker import DockerProvider

        return DockerProvider()
    if name == "qemu":
        from ale.run.providers.qemu import QemuProvider

        return QemuProvider()
    if name == "orchard":
        from ale.run.providers.orchard import OrchardProvider

        return OrchardProvider()
    raise ProviderCapabilityError(f"a node agent cannot drive a {name!r} Provider")


@dataclass(frozen=True)
class NodeSettings:
    name: str
    control_url: str
    address: str
    """Where every Run's machine reaches this agent, such as ``http://NAME:7471``."""
    host: str = "0.0.0.0"
    port: int = 7471
    providers: tuple[str, ...] = ()
    """The Providers to lend; empty lends every one whose preflight passes here."""
    labels: dict[str, str] = field(default_factory=dict)
    reserve_cpus: int = 1
    reserve_memory_mb: int = 4096
    macos_vm_slots: int = 2
    """At most this many macOS VMs, when the machine runs them; never more than two."""
    state_dir: Path = Path(".")


# --- probing ------------------------------------------------------------------------------


def _machine_arch() -> Arch:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return Arch.ARM64
    if machine in {"x86_64", "amd64"}:
        return Arch.X86_64
    raise ProviderCapabilityError(f"unsupported machine architecture {machine}")


def _memory_mb() -> int:
    if platform.system() == "Darwin":
        output = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True
        ).stdout
        return int(output.strip()) // 2**20
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    raise ProviderCapabilityError("cannot read the machine's memory size")


def _gpus() -> tuple[GpuPool, ...]:
    if shutil.which("nvidia-smi") is None:
        return ()
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ()
    counts = Counter(line.strip() for line in result.stdout.splitlines() if line.strip())
    return tuple(GpuPool(model=model, count=count) for model, count in sorted(counts.items()))


def _platform() -> Platform:
    if platform.system() == "Darwin":
        return Platform(os="macos", os_version=platform.mac_ver()[0], arch=_machine_arch())
    return Platform(
        os=platform.system().lower(), os_version=platform.release(), arch=_machine_arch()
    )


def _boot_id() -> str:
    """This machine's boot. An agent that restarts keeps it; a machine that restarts gets a
    new one and has none of its Sandboxes."""
    linux = Path("/proc/sys/kernel/random/boot_id")
    if linux.is_file():
        return linux.read_text().strip()
    if platform.system() == "Darwin":
        found = subprocess.run(
            ["sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True, check=False
        ).stdout.strip()
        if found:
            return found
    raise ProviderCapabilityError("cannot read this machine's boot identity")


def profiles(name: str, provider: Provider, *, gpus: bool) -> tuple[SandboxProfile, ...]:
    """One profile per operating system a Provider's own capabilities name."""
    kind, arch = KINDS[name]
    capabilities = provider.capabilities()
    return tuple(
        SandboxProfile(
            provider=name,
            kind=kind,
            os=system,
            arch=arch or _machine_arch(),
            gui=capabilities.gui,
            network_modes=capabilities.network_modes,
            snapshot=capabilities.snapshot,
            gpus=gpus and name == "docker",
            max_cpus=capabilities.max_cpus,
            max_memory_mb=capabilities.max_memory_mb,
            macos_vm_slots=1 if system is OperatingSystem.MACOS else 0,
        )
        for system in sorted(capabilities.operating_systems)
    )


async def probe(
    settings: NodeSettings,
    *,
    make: Callable[[str], Provider] = _provider,
) -> tuple[NodeDeclaration, dict[str, Provider]]:
    """What this machine offers, from each Provider's own preflight and capabilities.

    A Provider named in ``settings.providers`` that fails its preflight stops the agent; when
    none is named, every one that passes is lent and the rest are logged.
    """
    names = settings.providers or tuple(KINDS)
    lent: dict[str, Provider] = {}
    for name in names:
        if name not in KINDS:
            raise ProviderCapabilityError(f"a node agent cannot drive a {name!r} Provider")
        _, arch = KINDS[name]
        if arch is not None and arch is not _machine_arch():
            if settings.providers:
                raise ProviderCapabilityError(f"{name} runs only on {arch} machines")
            continue
        provider = make(name)
        try:
            await provider.preflight()
        except AleError as error:
            if settings.providers:
                raise
            _log.info("not lending %s: %s", name, error)
            continue
        lent[name] = provider
    if not lent:
        raise ProviderCapabilityError("no Provider passed its preflight on this machine")
    gpus = _gpus() if "docker" in lent else ()
    offered = tuple(
        profile
        for name, provider in lent.items()
        for profile in profiles(name, provider, gpus=bool(gpus))
    )
    macos = any(profile.macos_vm_slots for profile in offered)
    framework = framework_provenance()
    declaration = NodeDeclaration(
        protocol=PROTOCOL_VERSION,
        name=settings.name,
        boot_id=_boot_id(),
        address=settings.address,
        agent_version=__version__,
        engine_commit=framework.commit,
        framework=fleet_framework_identity(),
        platform=_platform(),
        labels=settings.labels,
        resources=NodeResources(
            cpus=max((os.cpu_count() or 1) - settings.reserve_cpus, 0),
            memory_mb=max(_memory_mb() - settings.reserve_memory_mb, 0),
            disk_gb=shutil.disk_usage(settings.state_dir).total // 1024**3,
            gpus=gpus,
            macos_vm_slots=min(settings.macos_vm_slots, 2) if macos else 0,
        ),
        profiles=offered,
    )
    return declaration, lent


# --- the agent ----------------------------------------------------------------------------


@dataclass
class _Held:
    grant: Grant
    provider: Provider
    profile: SandboxProfile
    holds: Holds
    lease_expires_at: float
    state: Literal["creating", "ready", "destroying"] = "creating"
    sandbox: Sandbox | None = None
    created: asyncio.Future[SandboxDescription] | None = None
    destroyed: asyncio.Task[None] | None = None
    create_sec: float | None = None
    entry: _Entry | None = None


class _Entry(BaseModel):
    """What the agent journals about a Sandbox it holds, to drive it again after a crash."""

    model_config = ConfigDict(extra="forbid")

    grant: Grant
    provider: str
    holds: Holds
    request: SandboxRequest
    """As the Node's Provider got it, with the Node's own Gateway and proxy addresses."""
    lease_expires_at: float
    ports: dict[int, int] = {}
    """The Node ports this Sandbox reaches its Run's tunnel targets at."""
    handle: str | None = None
    """The Provider's name for the Sandbox, once it is created."""
    description: SandboxDescription | None = None


def _mismatch(demand: SandboxDemand, request: SandboxRequest) -> str | None:
    """How a creating request differs from what the grant reserved, if it does."""
    if request.image_kind is not demand.kind or request.os is not demand.os:
        return "kind or operating system"
    if request.network.mode is not demand.network_mode:
        return "network mode"
    if request.resources != demand.resources:
        return "resources"
    return None


def _error(error: BaseException) -> ErrorBody:
    return ErrorBody(error=type(error).__name__, message=str(error))


def _json(model: BaseModel, status: int = 200) -> web.Response:
    return web.Response(
        text=model.model_dump_json(), status=status, content_type="application/json"
    )


class NodeAgent:
    def __init__(
        self,
        settings: NodeSettings,
        token: str,
        *,
        declaration: NodeDeclaration,
        providers: dict[str, Provider],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._token = token
        self.declaration = declaration
        self._providers = providers
        self.images = NodeImages(settings.state_dir / "images", self._lend)
        self._clock = clock
        self.held: dict[str, _Held] = {}
        self._listeners: dict[str, NodeListeners] = {}
        self.draining = False
        self._session: ClientSession | None = None
        self._scratch = settings.state_dir / "uploads"
        self._scratch.mkdir(parents=True, exist_ok=True)
        # Requests carry episode credentials, such as the egress proxy's token.
        self._journal = settings.state_dir / "sandboxes"
        self._journal.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._background: set[asyncio.Future[None]] = set()

    def _later(self, work: Coroutine[object, object, None]) -> None:
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @property
    def name(self) -> str:
        return self.declaration.name

    def _lend(self, kind: ImageKind, operating_system: OperatingSystem) -> Provider:
        """The Provider that serves ``kind`` Sandboxes of ``operating_system`` here."""
        for profile in self.declaration.profiles:
            if profile.kind is kind and profile.os is operating_system:
                return self._providers[profile.provider]
        raise ProviderCapabilityError(f"this Node starts no {kind} {operating_system} Sandbox")

    # --- the control plane -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _post(self, path: str, model: BaseModel) -> dict:
        assert self._session is not None
        async with self._session.post(
            f"{self.settings.control_url}{path}",
            data=model.model_dump_json(),
            headers=self._headers() | {"Content-Type": "application/json"},
        ) as response:
            if response.status == 409:
                raise _Rejected(await response.text())
            if response.status >= 400:
                raise ProviderStartError(
                    f"the control plane answered {response.status}: {await response.text()}"
                )
            return await response.json()

    def roster(self) -> tuple[RosterEntry, ...]:
        return tuple(
            RosterEntry(
                sandbox_id=sandbox_id,
                run_id=held.grant.run_id,
                state=held.state,
                lease_expires_at=held.lease_expires_at,
                create_sec=held.create_sec,
            )
            for sandbox_id, held in sorted(self.held.items())
        )

    async def heartbeat(self) -> HeartbeatReply:
        beat = Heartbeat(
            boot_id=self.declaration.boot_id,
            roster=self.roster(),
            images=self.images.inventory(),
            disk_free_gb=self.images.free_gb(),
        )
        reply = HeartbeatReply.model_validate(
            await self._post(f"/v1/nodes/{self.name}/heartbeat", beat)
        )
        if not reply.registered:
            await self._post(f"/v1/nodes/{self.name}", self.declaration)
            return reply
        self.draining = reply.draining
        for sandbox_id in reply.destroy:
            if (held := self.held.get(sandbox_id)) is not None and held.state != "destroying":
                _log.info("destroying %s, which the control plane no longer holds", sandbox_id)
                self._later(self._destroy(held))
        return reply

    async def register(self) -> None:
        """Join the Fleet, waiting for the control plane to be up.

        A control plane that refuses this Node, because it cannot reach the agent at its
        address, stops the agent with its reason: that is for the operator to fix.
        """
        while True:
            try:
                await self._post(f"/v1/nodes/{self.name}", self.declaration)
            except _Rejected:
                raise
            except Exception as error:
                _log.warning("registering with %s failed: %s", self.settings.control_url, error)
                await asyncio.sleep(HEARTBEAT_SEC)
                continue
            _log.info("registered with %s", self.settings.control_url)
            return

    async def _heartbeats(self) -> None:
        while True:
            try:
                await self.heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _log.warning("heartbeat failed: %s", error)
            await asyncio.sleep(HEARTBEAT_SEC)

    def expire(self) -> None:
        """Destroy every Sandbox whose lease ran out."""
        now = self._clock()
        for sandbox_id, held in list(self.held.items()):
            if held.state != "destroying" and held.lease_expires_at < now:
                _log.info("the lease of %s ran out; destroying it", sandbox_id)
                self._later(self._destroy(held))

    async def _leases(self) -> None:
        while True:
            self.expire()
            await asyncio.sleep(LEASE_CHECK_SEC)

    # --- the journal ----------------------------------------------------------------

    def _write(self, held: _Held) -> None:
        if held.entry is None:
            return
        held.entry.lease_expires_at = held.lease_expires_at
        path = self._journal / f"{held.grant.sandbox_id}.json"
        partial = path.with_suffix(".partial")
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(held.entry.model_dump_json())
        os.replace(partial, path)

    def _forget(self, sandbox_id: str) -> None:
        (self._journal / f"{sandbox_id}.json").unlink(missing_ok=True)

    async def recover(self) -> None:
        """Drive again, or destroy, the Sandboxes an earlier agent on this machine held.

        One whose lease still runs is reattached through its Provider and held as before,
        with its Run's tunnel ports, so the Run keeps it. Any other, or one that cannot be
        reattached within ``RECOVER_SEC``, is destroyed. One whose creation never finished
        has no handle and is left to its Provider. All recover at once, so a slow one holds
        up none of the others.
        """
        for partial in self._journal.glob("*.partial"):
            # A write the crash cut short; it may hold an episode's credentials.
            partial.unlink(missing_ok=True)
        journal = sorted(self._journal.glob("*.json"))
        await asyncio.gather(*(self._recover(path) for path in journal))

    async def _recover(self, path: Path) -> None:
        try:
            entry = _Entry.model_validate_json(path.read_text())
        except (OSError, ValueError) as error:
            _log.warning("dropping %s, which cannot be read: %s", path, error)
            path.unlink(missing_ok=True)
            return
        sandbox_id = entry.grant.sandbox_id
        if entry.handle is None or entry.description is None:
            _log.warning("%s was still being created; left to its Provider", sandbox_id)
            path.unlink(missing_ok=True)
            return
        provider = self._providers.get(entry.provider)
        if provider is not None and entry.lease_expires_at > self._clock():
            try:
                async with asyncio.timeout(RECOVER_SEC):
                    await self._reattach(entry, provider)
            except Exception:
                _log.warning("could not drive %s again", sandbox_id, exc_info=True)
            else:
                _log.info("drove %s again, which its Run still holds", sandbox_id)
                return
        try:
            await (provider or _provider(entry.provider)).discard(entry.handle)
            _log.info("destroyed %s, which no Run holds any more", sandbox_id)
        except Exception:
            _log.warning("could not destroy %s", sandbox_id, exc_info=True)
        path.unlink(missing_ok=True)

    async def _reattach(self, entry: _Entry, provider: Provider) -> None:
        description = entry.description
        assert description is not None and entry.handle is not None
        request = entry.request
        profile = next(
            (
                p
                for p in self.declaration.profiles
                if p.provider == entry.provider
                and p.kind is request.image_kind
                and p.os is request.os
            ),
            None,
        )
        if profile is None:
            raise ProviderCapabilityError(f"this Node no longer lends {entry.provider} for it")
        sandbox = await provider.reattach(
            entry.handle, request, description.allocation, description.resolved_image
        )
        created: asyncio.Future[SandboxDescription] = asyncio.get_running_loop().create_future()
        created.set_result(description)
        held = _Held(
            grant=entry.grant,
            provider=provider,
            profile=profile,
            holds=entry.holds,
            lease_expires_at=entry.lease_expires_at,
            state="ready",
            sandbox=sandbox,
            created=created,
            create_sec=description.create_sec,
            entry=entry,
        )
        self.held[entry.grant.sandbox_id] = held
        self._listeners_for(entry.grant.run_id).keep(entry.ports)

    # --- Sandboxes --------------------------------------------------------------------

    def _view(self) -> NodeView:
        return NodeView(
            declaration=self.declaration,
            holds=tuple(held.holds for held in self.held.values()),
        )

    def _listeners_for(self, run_id: str) -> NodeListeners:
        if run_id not in self._listeners:
            self._listeners[run_id] = NodeListeners(run_id, host=self.settings.host)
        return self._listeners[run_id]

    async def _forget_run(self, run_id: str) -> None:
        listeners = self._listeners.get(run_id)
        if listeners is None or listeners.attached:
            return
        if any(held.grant.run_id == run_id for held in self.held.values()):
            return
        del self._listeners[run_id]
        await listeners.close()

    def admit(self, grant: Grant, request: SandboxRequest) -> tuple[Holds, SandboxProfile]:
        """Check a grant's demand against this Node's own count, which the ledger estimated."""
        if (difference := _mismatch(grant.demand, request)) is not None:
            raise ProviderCapabilityError(f"the request's {difference} differs from its grant")
        if request.prepared_image != self.images.prepared(grant.demand.image):
            raise _Refused("the request's image is not the one this Node prepared for its grant")
        view = self._view()
        view = NodeView(
            declaration=view.declaration,
            holds=view.holds,
            images=frozenset({grant.demand.image}),
        )
        choice, reason = choose(grant.demand, [view])
        if choice is None:
            raise _Refused(reason or "no profile here serves it")
        return choice.holds, choice.profile

    async def create(self, body: CreateSandbox, sandbox_id: str) -> SandboxDescription:
        try:
            grant = verify_grant(body.grant, self._token, node=self.name, now=self._clock())
        except ValueError as error:
            raise web.HTTPForbidden(text=str(error)) from None
        if grant.sandbox_id != sandbox_id:
            raise ProviderCapabilityError("the grant is for another Sandbox")
        held = self.held.get(sandbox_id)
        if held is None:
            holds, profile = self.admit(grant, body.request)
            held = _Held(
                grant=grant,
                provider=self._providers[profile.provider],
                profile=profile,
                holds=holds,
                lease_expires_at=self._clock() + body.lease_sec,
            )
            held.entry = _Entry(
                grant=grant,
                provider=profile.provider,
                holds=holds,
                request=body.request,
                lease_expires_at=held.lease_expires_at,
            )
            self.held[sandbox_id] = held
            held.created = asyncio.ensure_future(self._create(held, body))
            self._write(held)
        assert held.created is not None
        return await asyncio.shield(held.created)

    async def _create(self, held: _Held, body: CreateSandbox) -> SandboxDescription:
        started = time.monotonic()
        request = body.request
        sandbox_id = held.grant.sandbox_id
        entry = held.entry
        assert entry is not None
        try:
            listeners = self._listeners_for(held.grant.run_id)
            update: dict[str, object] = {}
            if request.gateway_url and body.gateway_target is not None:
                port = await listeners.port(body.gateway_target)
                entry.ports[body.gateway_target] = port
                update["gateway_url"] = with_port(request.gateway_url, port)
            if request.proxy_url and body.proxy_target is not None:
                port = await listeners.port(body.proxy_target)
                entry.ports[body.proxy_target] = port
                update["proxy_url"] = with_port(request.proxy_url, port)
            entry.request = request.model_copy(update=update)
            sandbox = await held.provider.create(entry.request)
        except BaseException:
            self.held.pop(sandbox_id, None)
            self._forget(sandbox_id)
            await self._forget_run(held.grant.run_id)
            raise
        held.sandbox = sandbox
        held.create_sec = round(time.monotonic() - started, 3)
        if held.state == "destroying":
            raise ProviderStartError(f"{sandbox_id} was destroyed while it was being created")
        held.state = "ready"
        _log.info("%s is ready after %.1fs", sandbox_id, held.create_sec)
        description = SandboxDescription(
            sandbox_id=sandbox_id,
            node=self.name,
            provider=held.profile.provider,
            resolved_image=sandbox.resolved_image,
            allocation=sandbox.allocation,
            agent_user=sandbox.agent_user,
            agent_home=sandbox.agent_home,
            gateway_url=sandbox.gateway_url,
            create_sec=held.create_sec,
        )
        entry.handle, entry.description = sandbox.handle, description
        self._write(held)
        return description

    async def _destroy(self, held: _Held) -> None:
        if held.destroyed is None:
            held.state = "destroying"
            held.destroyed = asyncio.ensure_future(self._teardown(held))
        await asyncio.shield(held.destroyed)

    async def _teardown(self, held: _Held) -> None:
        sandbox_id = held.grant.sandbox_id
        try:
            if held.created is not None and not held.created.done():
                held.created.cancel()
                await asyncio.gather(held.created, return_exceptions=True)
            if held.sandbox is not None:
                await held.sandbox.destroy()
        except Exception:
            _log.exception("destroying %s failed", sandbox_id)
        finally:
            self.held.pop(sandbox_id, None)
            self._forget(sandbox_id)
            await self._forget_run(held.grant.run_id)

    def renew(self, run_id: str, lease_sec: float) -> int:
        expires = self._clock() + lease_sec
        renewed = 0
        for held in self.held.values():
            if held.grant.run_id == run_id and held.state != "destroying":
                held.lease_expires_at = max(held.lease_expires_at, expires)
                try:
                    self._write(held)
                except OSError as error:
                    # The lease holds here; only a crash before the next renewal loses it.
                    _log.warning("could not journal %s: %s", held.grant.sandbox_id, error)
                renewed += 1
        return renewed

    def ready(self, sandbox_id: str) -> Sandbox:
        held = self.held.get(sandbox_id)
        if held is None:
            raise web.HTTPNotFound(text=f"this Node holds no Sandbox {sandbox_id}")
        if held.state != "ready" or held.sandbox is None:
            raise web.HTTPConflict(text=f"{sandbox_id} is {held.state}")
        return held.sandbox

    async def stop(self) -> None:
        await asyncio.gather(
            *(self._destroy(held) for held in list(self.held.values())), return_exceptions=True
        )
        for listeners in list(self._listeners.values()):
            await listeners.close()

    # --- serving ----------------------------------------------------------------------

    def app(self) -> web.Application:
        return _Api(self).app()

    async def serve(self) -> None:
        """Drive again or destroy what an earlier agent on this machine held, then serve
        until cancelled, and then destroy every Sandbox held."""
        await self.recover()
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, self.settings.host, self.settings.port).start()
        self._session = ClientSession(timeout=ClientTimeout(total=30))
        # Leases hold whether or not the control plane is up, the recovered ones included.
        tasks: list[asyncio.Future[None]] = [asyncio.ensure_future(self._leases())]
        _log.info("node agent %s on %s", self.name, self.settings.address)
        try:
            # Listening first: the control plane connects back before it counts this Node.
            await self.register()
            tasks.append(asyncio.ensure_future(self._heartbeats()))
            await asyncio.Event().wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.shield(self.stop())
            await self._session.close()
            await runner.cleanup()


class _Refused(ProviderCapabilityError):
    """This Node's own admission refused a grant; the Run asks for another Node."""


class _Rejected(ProviderCapabilityError):
    """The control plane refused this Node, with its reason."""


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class _Api:
    def __init__(self, agent: NodeAgent) -> None:
        self.agent = agent

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.guard], client_max_size=2**30)
        sandbox = "/v1/sandboxes/{id}"
        app.add_routes(
            [
                web.get("/v1/node", self.node),
                web.post(sandbox, self.create),
                web.delete(sandbox, self.destroy),
                web.get(f"{sandbox}/exec", self.exec),
                web.get(f"{sandbox}/files", self.read_file),
                web.put(f"{sandbox}/files", self.write_file),
                web.get(f"{sandbox}/listing", self.list_files),
                web.get(f"{sandbox}/digest", self.file_digest),
                web.post(f"{sandbox}/upload", self.upload_dir),
                web.post(f"{sandbox}/egress", self.egress),
                web.get(f"{sandbox}/screenshot", self.screenshot),
                web.post(f"{sandbox}/input", self.inject_input),
                web.post("/v1/runs/{run}/lease", self.lease),
                web.get("/v1/runs/{run}/tunnel", self.tunnel),
                web.get("/v1/images/{key}", self.image),
                web.put("/v1/images/{key}/inputs", self.inputs),
                web.post("/v1/images/{key}", self.prepare),
            ]
        )
        return app

    @web.middleware
    async def guard(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        given = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(given.encode(), self.agent._token.encode()):
            raise web.HTTPUnauthorized(text="a Fleet token is required")
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except _Refused as error:
            return _json(_error(error), status=409)
        except AleError as error:
            return _json(_error(error), status=422)
        except Exception as error:
            _log.exception("%s %s failed", request.method, request.path)
            return _json(ErrorBody(error="ProviderStartError", message=str(error)), 500)

    async def node(self, _request: web.Request) -> web.Response:
        return _json(self.agent.declaration)

    async def create(self, request: web.Request) -> web.Response:
        try:
            body = CreateSandbox.model_validate_json(await request.read())
        except ValidationError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        return _json(await self.agent.create(body, request.match_info["id"]))

    async def destroy(self, request: web.Request) -> web.Response:
        held = self.agent.held.get(request.match_info["id"])
        if held is not None:
            await self.agent._destroy(held)
        return web.Response(status=204)

    async def exec(self, request: web.Request) -> web.StreamResponse:
        sandbox = self.agent.ready(request.match_info["id"])
        socket = web.WebSocketResponse(heartbeat=30)
        await socket.prepare(request)
        call = ExecCall.model_validate_json(await socket.receive_str(timeout=60))

        async def sink(stream: str, data: bytes) -> None:
            await socket.send_bytes(bytes([STDOUT if stream == "stdout" else STDERR]) + data)

        running = asyncio.ensure_future(
            sandbox.exec(
                call.argv,
                cwd=call.cwd,
                env=call.env,
                timeout_sec=call.timeout_sec,
                identity=call.identity,
                output_sink=sink if call.stream else None,  # type: ignore[arg-type]
            )
        )

        async def caller_left() -> None:
            async for message in socket:
                if message.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                    break

        watching = asyncio.ensure_future(caller_left())
        await asyncio.wait({running, watching}, return_when=asyncio.FIRST_COMPLETED)
        if not running.done():
            running.cancel()  # the Run cancelled the command or lost its connection
            await asyncio.gather(running, return_exceptions=True)
            return socket
        watching.cancel()
        try:
            done = ExecDone(result=running.result())
        except Exception as error:
            done = ExecDone(error=_error(error))
        await socket.send_str(done.model_dump_json())
        await socket.close()
        return socket

    async def read_file(self, request: web.Request) -> web.Response:
        sandbox = self.agent.ready(request.match_info["id"])
        query = request.query
        length = query.get("length")
        data = await sandbox.read_file(
            query["path"],
            offset=int(query.get("offset", "0")),
            length=int(length) if length is not None else None,
        )
        return web.Response(body=data, content_type="application/octet-stream")

    async def write_file(self, request: web.Request) -> web.Response:
        sandbox = self.agent.ready(request.match_info["id"])
        await sandbox.write_file(
            request.query["path"], await request.read(), identity=_identity(request)
        )
        return web.Response(status=204)

    async def list_files(self, request: web.Request) -> web.Response:
        sandbox = self.agent.ready(request.match_info["id"])
        return _json(Listing(files=await sandbox.list_files(request.query["path"])))

    async def file_digest(self, request: web.Request) -> web.Response:
        sandbox = self.agent.ready(request.match_info["id"])
        digest = await sandbox.file_digest(
            request.query["path"], length=int(request.query["length"])
        )
        return web.json_response({"digest": digest})

    async def upload_dir(self, request: web.Request) -> web.Response:
        """Unpack the Run's directory, sent as a tar stream, and upload it as a local Run would."""
        sandbox = self.agent.ready(request.match_info["id"])
        with tempfile.TemporaryDirectory(dir=self.agent._scratch) as scratch:
            archive = Path(scratch) / "tree.tar"
            with archive.open("wb") as handle:
                async for chunk in request.content.iter_chunked(2**20):
                    await asyncio.to_thread(handle.write, chunk)
            tree = Path(scratch) / "tree"
            await asyncio.to_thread(_unpack, archive, tree)
            await sandbox.upload_dir(
                str(tree), request.query["target"], identity=_identity(request)
            )
        return web.Response(status=204)

    async def egress(self, request: web.Request) -> web.Response:
        sandbox = self.agent.ready(request.match_info["id"])
        if (await request.json()).get("open"):
            await sandbox.open_egress()
        else:
            await sandbox.close_egress()
        return web.Response(status=204)

    async def screenshot(self, request: web.Request) -> web.Response:
        sandbox = self.agent.ready(request.match_info["id"])
        return web.Response(body=await sandbox.screenshot(), content_type="image/png")

    async def inject_input(self, request: web.Request) -> web.Response:
        sandbox = self.agent.ready(request.match_info["id"])
        applied = await sandbox.inject_input(await request.json())
        return web.json_response({"applied": applied})

    async def lease(self, request: web.Request) -> web.Response:
        renewal = LeaseRenewal.model_validate_json(await request.read())
        renewed = self.agent.renew(request.match_info["run"], renewal.lease_sec)
        return web.json_response({"renewed": renewed})

    async def tunnel(self, request: web.Request) -> web.StreamResponse:
        run_id = request.match_info["run"]
        socket = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
        await socket.prepare(request)
        listeners = self.agent._listeners_for(run_id)
        try:
            await listeners.attach(socket)
        finally:
            await self.agent._forget_run(run_id)
        return socket

    @staticmethod
    def _key(request: web.Request) -> str:
        key = request.match_info["key"]
        if not _KEY.fullmatch(key):
            raise web.HTTPBadRequest(text="an image is named by its sha256 key")
        return key

    async def image(self, request: web.Request) -> web.Response:
        return _json(ImageState(inputs=self.agent.images.has_inputs(self._key(request))))

    async def inputs(self, request: web.Request) -> web.Response:
        """Keep an image's build context, sent as a tar stream of the Task folder's part."""
        key = self._key(request)
        with tempfile.TemporaryDirectory(dir=self.agent._scratch) as scratch:
            archive = Path(scratch) / "inputs.tar"
            with archive.open("wb") as handle:
                async for chunk in request.content.iter_chunked(2**20):
                    await asyncio.to_thread(handle.write, chunk)
            tree = Path(scratch) / "task"
            await asyncio.to_thread(_unpack, archive, tree)
            await self.agent.images.keep_inputs(key, tree)
        return web.Response(status=204)

    async def prepare(self, request: web.Request) -> web.Response:
        try:
            inputs = ImageInputs.model_validate_json(await request.read())
        except ValidationError as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        if inputs.key != self._key(request):
            raise web.HTTPBadRequest(text="the inputs are for another image")
        return _json(await self.agent.images.prepare(inputs))


def _identity(request: web.Request) -> Identity:
    return Identity(request.query.get("identity", Identity.FRAMEWORK.value))


def _tree_member(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
    """Only what a Run's directory holds: files, directories and symbolic links, all inside.

    Hard links are refused, since one can name any file on the Node.
    """
    if not (member.isreg() or member.isdir() or member.issym()):
        raise ProviderCapabilityError(
            f"an upload holds {member.name!r}, which is not a file, directory or symbolic link"
        )
    return tarfile.tar_filter(member, destination)


def _unpack(archive: Path, tree: Path) -> None:
    tree.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(tree, filter=_tree_member)
