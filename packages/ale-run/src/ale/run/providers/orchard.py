"""Remote macOS sandboxes on an existing Orchard/Tart control plane.

The control plane owns placement. ALE owns only VMs created for one episode; it
never discovers spare machines, adopts existing VMs, or changes their base images.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ale.core.config import OrchardProviderConfig
from ale.core.errors import GuestUnreachableError, ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    Capabilities,
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
    SandboxState,
)
from ale.core.taskspec import NetworkMode, OperatingSystem
from ale.run import guestd
from ale.run.images import resolve_registry_reference
from ale.run.providers.orchard_client import OrchardClient
from ale.run.providers.orchard_desktop import await_desktop
from ale.run.providers.privileges import verify_identities
from ale.run.transport import GuestClient

_IMAGE = re.compile(
    r"^(?P<repository>[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?/"
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*)"
    r"(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)
_ROOT = "/opt/ale"
_PATH = "/opt/ale/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_READ_CHUNK_BYTES = 64 * 1024

# This source executes only in a newly created VM, as root. Input is copied through
# stdin, never a command URL. Refuse symlinked framework roots before installing.
_BOOTSTRAP = r"""
import base64,json,os,pathlib,platform,pwd,sys
if os.geteuid()!=0 or platform.system()!='Darwin' or sys.version_info<(3,12):
    raise RuntimeError('macOS, root and Python 3.12+ are required')
account=pwd.getpwnam(sys.argv[1])
if account.pw_uid==0 or not pathlib.Path(account.pw_dir).is_dir():
    raise RuntimeError('agent must be an existing non-root account with a home')
# A solver without sudo must not be able to replace a later verifier's Python or
# the root-invoked desktop client. Check resolved targets and every ancestor.
if sys.argv[2]=='false':
    roots=[sys.executable,sys.prefix,sys.base_prefix,*sys.path]
    if sys.argv[3]: roots.append(sys.argv[3])
    for raw in roots:
        if not raw: continue
        original=pathlib.Path(raw).absolute()
        for candidate in [original,original.resolve()]:
            for p in [candidate,*candidate.parents]:
                if not p.exists(): continue
                st=p.stat()
                if st.st_uid!=0 or st.st_mode&0o022:
                    raise RuntimeError('unprotected framework runtime: '+str(p))
for raw in ['/opt','/opt/ale','/opt/ale/guestd']:
    p=pathlib.Path(raw)
    if p.is_symlink(): raise RuntimeError('framework path must not be a symlink')
    p.mkdir(exist_ok=True)
    if raw!='/opt': os.chown(p,0,0);p.chmod(0o700)
for name,data in json.load(sys.stdin).items():
    if pathlib.PurePosixPath(name).name!=name: raise RuntimeError('invalid source name')
    p=pathlib.Path('/opt/ale/guestd')/name
    if p.is_symlink(): raise RuntimeError('framework source must not be a symlink')
    p.write_bytes(base64.b64decode(data,validate=True));os.chown(p,0,0);p.chmod(0o600)
binpath=pathlib.Path('/opt/ale/bin')
if binpath.is_symlink(): raise RuntimeError('framework bin must not be a symlink')
binpath.mkdir(exist_ok=True);os.chown(binpath,0,0);binpath.chmod(0o700)
python=binpath/'python3'
if python.exists() or python.is_symlink(): python.unlink()
python.symlink_to(sys.executable)
print(json.dumps({'agent_home':account.pw_dir}))
"""


def _sources() -> bytes:
    root = Path(guestd.__file__).parent
    return json.dumps(
        {
            p.name: base64.b64encode(p.read_bytes()).decode("ascii")
            for p in sorted(root.glob("*.py"))
        },
        sort_keys=True,
    ).encode()


def _identity(reference: str) -> tuple[str, str]:
    match = _IMAGE.fullmatch(reference)
    if match is None or match["digest"] is None:
        raise ProviderCapabilityError(
            "Prepared Orchard images require a digest-pinned OCI image reference"
        )
    digest = match["digest"]
    # Preparation identity includes the guest service installed into the VM. The
    # observed base identity remains the image reference attested by Orchard.
    prepared = (
        "sha256:"
        + hashlib.sha256(reference.encode() + b"\0" + _BOOTSTRAP.encode() + _sources()).hexdigest()
    )
    return digest, prepared


@dataclass
class _OwnedVM:
    name: str
    uid: str
    stopped: bool = False


async def _delete_owned(client: OrchardClient, owned: _OwnedVM) -> None:
    name, uid = owned.name, owned.uid
    record = await client.get_vm(name)
    if record is None:
        if owned.stopped:
            return
        raise ProviderStartError(f"Orchard VM {name} is absent; physical stop is unconfirmed")
    if record.get("uid") != uid:
        raise ProviderStartError(f"Refusing to delete Orchard VM {name}: UID changed")
    if record.get("status") == "failed":
        # Orchard synthesizes running=false for failed/offline workers too. A
        # terminal record cannot accept PUT and is not physical stop evidence.
        raise ProviderStartError(
            f"Orchard VM {name} (UID {uid}) is terminal failed; controller rejects stop; "
            "physical stop unconfirmed, inspect worker before cleanup"
        )
    # PUT replaces VMSpec. Forward only public mutable fields, never credentials
    # or metadata from the controller's full VM record.
    fields = (
        "os",
        "arch",
        "runtime",
        "hostProcesses",
        "endpoints",
        "net-softnet",
        "netSoftnet",
        "netSoftnetAllow",
        "netSoftnetBlock",
        "suspendable",
        "usbAccessories",
    )
    spec = {key: record[key] for key in fields if key in record}
    spec["powerState"] = "stopped"
    try:
        changed = (
            record if record.get("powerState") == "stopped" else await client.update_vm(name, spec)
        )
        generation = changed.get("generation")
        if changed.get("uid") != uid or type(generation) is not int:
            raise ProviderStartError(f"Orchard stop identity/generation mismatch for {name}")
        async with asyncio.timeout(60):
            while True:
                record = await client.get_vm(name)
                if record is None or record.get("uid") != uid:
                    raise ProviderStartError(
                        f"Orchard VM {name} disappeared or changed during stop"
                    )
                observed = record.get("observedGeneration")
                conditions = record.get("conditions")
                stopped = isinstance(conditions, list) and any(
                    isinstance(item, dict)
                    and item.get("type") == "running"
                    and item.get("state") == "false"
                    for item in conditions
                )
                if type(observed) is int and observed >= generation and stopped:
                    owned.stopped = True
                    break
                await asyncio.sleep(1)
    except Exception as exc:
        # Keep the record inspectable when power-off cannot be proved. Deleting it
        # would destroy the only controller-side evidence needed for recovery.
        raise ProviderStartError(
            f"Orchard VM {name} (UID {uid}) stop unconfirmed; inspect worker cleanup"
        ) from exc
    current = await client.get_vm(name)
    if current is None or current.get("uid") != uid:
        raise ProviderStartError(f"Orchard VM {name} changed before deletion")
    await client.delete_vm(name)
    # The worker acknowledged stopped before DELETE. Disk removal is eventually
    # reconciled by Orchard; controller absence alone never proves a process exit.
    async with asyncio.timeout(60):
        while (record := await client.get_vm(name)) is not None:
            if record.get("uid") != uid:
                raise ProviderStartError(f"Orchard VM {name} was replaced during cleanup")
            await asyncio.sleep(1)


class OrchardSandbox(Sandbox):
    def __init__(
        self,
        *,
        request: SandboxRequest,
        gateway_host: str | None,
        orchard: OrchardClient,
        client: GuestClient,
        file_client: GuestClient,
        name: str,
        uid: str,
        agent_home: str,
        allocation: ResourceAllocation,
        resolved_image: ResolvedImage,
    ) -> None:
        super().__init__(
            sandbox_id=name,
            request=request,
            resolved_image=resolved_image,
            allocation=allocation,
            agent_user=request.user,
            agent_home=agent_home,
        )
        self._gateway_host = gateway_host
        self._orchard = orchard
        self._client = client
        self._file_client = file_client
        self._uid = uid
        self._owned = _OwnedVM(name, uid)
        self._destroy_lock = asyncio.Lock()
        self.state = SandboxState.READY

    @property
    def gateway_url(self) -> str | None:
        if not self.request.gateway_url:
            return None
        parsed = urlsplit(self.request.gateway_url)
        host = self._gateway_host
        if not host:
            raise ProviderCapabilityError("Orchard requires gateway_host for model access")
        if ":" in host:
            host = f"[{host}]"
        return urlunsplit(parsed._replace(netloc=host + (f":{parsed.port}" if parsed.port else "")))

    def _as(self, identity: Identity) -> str | None:
        return self.agent_user if identity is Identity.AGENT else None

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
        started = asyncio.get_running_loop().time()
        try:
            code, stdout, stderr, timed_out = await self._client.exec(
                argv,
                cwd=cwd,
                env=env,
                timeout_sec=timeout_sec,
                run_as=self._as(identity),
                output_sink=output_sink,
            )
        except (asyncio.CancelledError, TimeoutError, GuestUnreachableError) as primary:
            # A disconnected command channel does not establish guest process exit.
            # Stop and delete the owned VM; final guest evidence may be lost.
            try:
                await asyncio.shield(self.destroy())
            except Exception as cleanup_error:
                detail = (
                    f"Orchard cleanup incomplete for {self.sandbox_id} (UID {self._uid}): "
                    f"{cleanup_error}"
                )
                primary.add_note(detail)
                logging.getLogger("ale.execution").error(detail)
            raise
        return ExecResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
        )

    async def write_file(
        self, path: str, data: bytes, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        await self._client.write_file(str(path), data, run_as=self._as(identity))

    async def read_file(self, path: str, *, offset: int = 0, length: int | None = None) -> bytes:
        if offset < 0 or (length is not None and length < 0):
            raise ValueError("file offset and length must be nonnegative")
        # Each request bounds the burst through Orchard's exec subscriber queue.
        # Merely splitting a single response into events does not apply backpressure.
        chunks = bytearray()
        while True:
            count = (
                _READ_CHUNK_BYTES
                if length is None
                else min(_READ_CHUNK_BYTES, length - len(chunks))
            )
            block = await self._file_client.read_file(
                str(path), offset=offset + len(chunks), length=count
            )
            if len(block) > count:
                raise GuestUnreachableError("File transfer exceeded its requested byte count")
            chunks.extend(block)
            if len(block) < count or (length is not None and len(chunks) == length):
                return bytes(chunks)

    async def list_files(self, path: str) -> tuple[SandboxFile, ...]:
        return await self._file_client.list_files(str(path))

    async def file_digest(self, path: str, *, length: int) -> str:
        return await self._file_client.file_digest(str(path), length=length)

    async def upload_dir(
        self, source: str, target: str, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        await self._client.upload_dir(source, str(target), run_as=self._as(identity))

    async def open_egress(self) -> None:
        # OPEN is the only policy accepted before provisioning; no firewall is changed.
        if self.request.network.mode is not NetworkMode.OPEN:
            raise ProviderCapabilityError("Orchard supports only open networking")

    async def close_egress(self) -> None:
        # Sealing an OPEN task leaves networking unchanged, explicitly.
        if self.request.network.mode is not NetworkMode.OPEN:
            raise ProviderCapabilityError("Orchard cannot enforce restricted networking")

    async def screenshot(self) -> bytes:
        # Orchard exec has a bounded output queue. Read one small response at a
        # time instead of sending the whole PNG as a single base64 JSON line.
        path = f"{_ROOT}/screenshot-{uuid.uuid4().hex}.png"
        primary: BaseException | None = None
        try:
            async with asyncio.timeout(120):
                metadata, _ = await self._client.call("screenshot", {"path": path}, timeout_sec=60)
                size = metadata.get("bytes")
                if type(size) is not int or not 8 <= size <= 64 * 1024 * 1024:
                    raise GuestUnreachableError("Screenshot has an invalid or excessive byte size")
                png = await self.read_file(path, length=size)
                if len(png) != size:
                    raise GuestUnreachableError("Screenshot transfer returned an incomplete file")
                if not png.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(
                    png
                ).hexdigest() != metadata.get("sha256"):
                    raise GuestUnreachableError("Screenshot transfer failed integrity verification")
                return bytes(png)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                # Use the capture channel: its ordered operations ensure removal
                # happens after capture, including an abandoned capture response.
                await self._client.call("remove", {"path": path}, timeout_sec=65)
            except Exception as exc:
                if primary is None:
                    raise
                primary.add_note(f"Screenshot temporary file cleanup failed: {exc}")
                logging.getLogger("ale.execution").warning(
                    "Screenshot temporary file cleanup failed for %s: %s", self.sandbox_id, exc
                )

    async def inject_input(self, actions: Sequence[object]) -> int:
        payload = [a if isinstance(a, dict) else a.model_dump(mode="json") for a in actions]  # type: ignore[union-attr]
        return await self._client.inject_input(payload)

    async def destroy(self) -> None:
        async with self._destroy_lock:
            if self.state is SandboxState.DESTROYED:
                return
            # A failed cleanup retains its UID and stop acknowledgement for an
            # explicit retry, while releasing host-side sockets in every outcome.
            await self._orchard.start()
            try:
                await _delete_owned(self._orchard, self._owned)
                self.state = SandboxState.DESTROYED
                self.release_resources()
            finally:
                try:
                    await self._client.close()
                finally:
                    try:
                        await self._file_client.close()
                    finally:
                        await self._orchard.close()


class OrchardProvider(Provider):
    name = "orchard"

    def __init__(self, config: OrchardProviderConfig) -> None:
        self.config = config

    def _new_client(self) -> OrchardClient:
        username = os.environ.get(self.config.username_env, "")
        password = os.environ.get(self.config.password_env, "")
        if not username or not password:
            raise ProviderCapabilityError(
                "Orchard username/password environment variables are required"
            )
        return OrchardClient(
            self.config.endpoint,
            username=username,
            password=password,
            ca_file=self.config.ca_file,
            server_hostname=self.config.server_hostname,
        )

    def capabilities(self) -> Capabilities:
        return Capabilities(
            operating_systems=frozenset({OperatingSystem.MACOS}),
            gui=bool(self.config.driver_path and self.config.socket_path),
            network_modes=frozenset({NetworkMode.OPEN}),
        )

    async def preflight(self) -> None:
        client = self._new_client()
        try:
            await client.start()
            workers = await client.list_workers()
        except ProviderStartError as exc:
            raise ProviderStartError(
                f"Orchard preflight failed: {exc}. Check the controller endpoint, TLS and "
                "credentials; start local services with scripts/setup_macos.py if needed"
            ) from None
        finally:
            await client.close()
        if not workers:
            raise ProviderStartError(
                "No Orchard workers are registered; start and register a worker"
            )
        workers = [
            worker
            for worker in workers
            if worker.get("arch") == "arm64" and worker.get("runtime") == "tart"
        ]
        if not workers:
            raise ProviderStartError("No ARM64 Tart worker is registered with Orchard")
        workers = [worker for worker in workers if worker.get("scheduling_paused", False) is False]
        if not workers:
            raise ProviderStartError("Orchard worker scheduling is paused or unavailable")
        now = datetime.now(UTC)
        for worker in workers:
            stamp = worker.get("last_seen")
            if not isinstance(stamp, str):
                continue
            try:
                seen = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                age = (now - seen).total_seconds()
            except (ValueError, TypeError, OverflowError):
                continue
            # ALE's freshness check, independent of Orchard's configurable offline
            # timeout. Small host/worker clock skew is tolerated; capacity is not
            # checked here because busy workers must remain eligible for queueing.
            if -30 <= age <= 180:
                return
        raise ProviderStartError(
            "No eligible Orchard worker has a fresh heartbeat (180 seconds); "
            "check worker connectivity and host/worker clocks"
        )

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if image.kind is not ImageKind.VM:
            raise ProviderCapabilityError("Orchard only accepts VM images")
        reference = image.runtime_ref if isinstance(image, PreparedTaskImage) else image.reference
        match = _IMAGE.fullmatch(reference)
        if match is None:
            raise ProviderCapabilityError("Orchard requires a valid Tart OCI image reference")
        if isinstance(image, PreparedTaskImage) and match["digest"] is None:
            raise ProviderCapabilityError(
                "Prepared Orchard images must already be pinned by digest"
            )
        await self.preflight()
        if match["digest"] is None:
            reference = await resolve_registry_reference(reference)
        else:
            reference = f"{match['repository']}@{match['digest']}"
        digest, prepared = _identity(reference)
        result = PreparedTaskImage(
            kind=ImageKind.VM,
            source="external-ref",
            input_identity=digest,
            runtime_ref=reference,
            prepared_identity=prepared,
            resolved_reference=reference,
            base_materials=(reference,),
        )
        if isinstance(image, PreparedTaskImage) and image != result:
            raise ProviderCapabilityError("Prepared Orchard image does not match this framework")
        return result

    def accepts(self, request: SandboxRequest) -> None:
        super().accepts(request)
        if request.resources.gpus:
            raise ProviderCapabilityError("Orchard GPU allocations are not supported")
        if request.retention != "destroy":
            raise ProviderCapabilityError("Orchard retention is not supported; use destroy")
        if request.resources.storage_mb and request.resources.storage_mb % 1024:
            raise ProviderCapabilityError("Orchard storage_mb must be a whole GiB")
        if any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\0" in value
            for key, value in request.env.items()
        ):
            raise ProviderCapabilityError("Invalid guest environment variable")
        if any(
            key.startswith(("DYLD_", "LD_", "PYTHON", "ALE_CUA_"))
            or key in {"PATH", "HOME", "SHELL", "ENV", "BASH_ENV"}
            for key in request.env
        ):
            raise ProviderCapabilityError("Task cannot override trusted framework environment")
        if request.user == "root":
            raise ProviderCapabilityError("The agent account must not be root")

    async def _await_running(self, client: OrchardClient, name: str, uid: str) -> dict[str, object]:
        last: object = "not observed"
        try:
            async with asyncio.timeout(self.config.provision_timeout_sec):
                while True:
                    record = await client.get_vm(name)
                    if record is None or record.get("uid") != uid:
                        raise ProviderStartError(f"Orchard VM {name} disappeared or changed UID")
                    last = record.get("status")
                    if last == "failed":
                        raise ProviderStartError(f"Orchard VM {name} failed to start")
                    if last == "running":
                        return record
                    await asyncio.sleep(1)
        except TimeoutError as exc:
            raise ProviderStartError(
                f"Orchard VM {name} provisioning exceeded {self.config.provision_timeout_sec:g}s "
                f"while waiting for scheduling, image pull and boot; last status: {last}"
            ) from exc

    async def _resolve_gateway_host(
        self, guest: GuestClient, request: SandboxRequest
    ) -> str | None:
        if not request.gateway_url:
            return None
        if self.config.gateway_host:
            return self.config.gateway_host
        # In Tart's default NAT network, the guest's router is its Mac host.
        # Resolve per VM so a customized vmnet subnet needs no ALE configuration.
        code, stdout, _, timed_out = await guest.exec(
            ["/sbin/route", "-n", "get", "-inet", "default"],
            run_as=request.user,
            timeout_sec=15,
        )
        match = re.search(r"^\s*gateway:\s*(\S+)\s*$", stdout, re.MULTILINE)
        if code == 0 and not timed_out and match:
            try:
                address = ipaddress.IPv4Address(match[1])
            except ValueError:
                pass
            else:
                if not (address.is_loopback or address.is_unspecified or address.is_multicast):
                    return str(address)
        raise ProviderStartError(
            "Could not discover the local Tart host from the VM's default IPv4 route; "
            "use default NAT networking or set vm.orchard.gateway_host in the run configuration"
        )

    async def create(self, request: SandboxRequest) -> OrchardSandbox:
        self.accepts(request)
        image = await self.prepare_image(request.prepared_image)
        name = "ale-" + uuid.uuid4().hex
        client = self._new_client()
        uid: str | None = None
        guest: GuestClient | None = None
        file_guest: GuestClient | None = None
        readiness_started = False
        try:
            await client.start()
            if await client.get_vm(name) is not None:
                raise ProviderStartError(f"Orchard VM name collision: {name}")
            spec: dict[str, object] = {
                "name": name,
                "image": image.runtime_ref,
                "cpu": request.resources.cpus,
                "memory": request.resources.memory_mb,
                "os": "darwin",
                "arch": "arm64",
                "runtime": "tart",
                "headless": True,
                "restart_policy": "Never",
                "imagePullPolicy": "IfNotPresent",
                "resources": {
                    "org.cirruslabs.tart-vms": 1,
                    "org.cirruslabs.logical-cores": request.resources.cpus,
                    "org.cirruslabs.memory-mib": request.resources.memory_mb,
                },
            }
            if request.resources.storage_mb:
                spec["diskSize"] = request.resources.storage_mb // 1024
            try:
                created = await client.create_vm(spec)
            except BaseException as exc:
                # There is no control-plane idempotency key. Query once to aid manual
                # recovery, never adopt/delete a VM without the returned ownership UID.
                try:
                    record = await asyncio.shield(client.get_vm(name))
                except BaseException as reconcile_error:
                    raise ProviderStartError(
                        f"Orchard create outcome uncertain for {name}; reconciliation failed; "
                        "inspect and clean it up manually; creation was not retried"
                    ) from reconcile_error
                detail = f", UID {record.get('uid')}" if record is not None else ""
                raise ProviderStartError(
                    f"Orchard create outcome uncertain for {name}{detail}; "
                    "inspect and clean it up manually; creation was not retried"
                ) from exc
            uid = str(created["uid"])
            record = await self._await_running(client, name, uid)
            readiness_started = True
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.config.boot_timeout_sec
            async with asyncio.timeout_at(deadline):
                for key in ("image", "cpu", "memory", "os", "arch", "runtime"):
                    if record.get(key) != spec[key]:
                        raise ProviderStartError(f"Orchard VM does not attest requested {key}")
                observed = str(record.get("image_fqn") or "")
                observed_identity, _ = _identity(observed)
                if observed_identity != image.input_identity:
                    raise ProviderStartError("Orchard image differs from prepared content identity")
                if (
                    record.get("assignedCPU") != request.resources.cpus
                    or record.get("assignedMemory") != request.resources.memory_mb
                ):
                    raise ProviderStartError("Orchard assigned resources differ from request")
                bootstrap = await client.exec(
                    name,
                    [
                        "sudo",
                        "-n",
                        "/usr/bin/env",
                        "-i",
                        "PATH=/usr/bin:/bin",
                        self.config.python_path,
                        "-I",
                        "-S",
                        "-c",
                        _BOOTSTRAP,
                        request.user,
                        "true" if request.sudo else "false",
                        self.config.driver_path or "",
                    ],
                    stdin=_sources(),
                    timeout_sec=deadline - loop.time(),
                    deadline=deadline,
                )
                if bootstrap.exit_code:
                    raise ProviderStartError(
                        "Could not install guestd: image needs root bootstrap and Python 3.12+"
                    )
                home = json.loads(bootstrap.stdout)["agent_home"]
                env = dict(request.env) | {
                    "ALE_AGENT_USER": request.user,
                    "ALE_AGENT_HOME": home,
                    "PATH": _PATH,
                    "HOME": "/var/root",
                }
                if self.config.driver_path:
                    env["ALE_CUA_DRIVER"] = self.config.driver_path
                if self.config.socket_path:
                    env["ALE_CUA_DRIVER_SOCKET"] = self.config.socket_path
                guest_argv = [
                    "sudo",
                    "-n",
                    "/usr/bin/env",
                    "-i",
                    *(f"{k}={v}" for k, v in env.items()),
                    self.config.python_path,
                    "-I",
                    "-S",
                    "-c",
                    "import runpy,sys; sys.path.insert(0,'/opt/ale/guestd'); "
                    "runpy.run_path('/opt/ale/guestd/main.py',run_name='__main__')",
                    "--stdio",
                ]
                guest = GuestClient(
                    await client.connect_guest(name, guest_argv, deadline=deadline),
                    acknowledge_exec_output=True,
                )
                # A separate stdio process permits live evidence reads while the
                # command session is occupied by a long-running native Harness.
                file_guest = GuestClient(
                    await client.connect_guest(name, guest_argv, deadline=deadline)
                )
                await file_guest.health()
                health = await guest.health()
                if (
                    health.get("os") != "macos"
                    or health.get("framework_uid") != 0
                    or not isinstance(health.get("agent_uid"), int)
                    or health["agent_uid"] == 0
                    or health.get("agent_user") != request.user
                    or health.get("agent_home") != home
                ):
                    raise ProviderCapabilityError("Guest macOS/account/root identity check failed")
                gateway_host = await self._resolve_gateway_host(guest, request)
                await verify_identities(guest, request.user, sudo=request.sudo)
                # Query all grants as root; a failed noninteractive sudo probe alone
                # would miss password-protected elevation rights.
                code, _, _, timed_out = await guest.exec(
                    ["/usr/bin/sudo", "-l", "-U", request.user], timeout_sec=15
                )
                if timed_out or code not in (0, 1):
                    raise ProviderCapabilityError("Could not verify agent sudo policy")
                if not request.sudo and code == 0:
                    raise ProviderCapabilityError("Image grants sudo but task declares sudo=false")
                total, _, _ = await guest.disk_usage("/")
                storage = total // (1024 * 1024)
                if request.resources.storage_mb and storage < request.resources.storage_mb:
                    raise ProviderCapabilityError("VM root disk is smaller than requested storage")
                if self.config.driver_path and self.config.socket_path:
                    await await_desktop(
                        guest,
                        agent_user=request.user,
                        python_path=self.config.python_path,
                        driver_path=self.config.driver_path,
                        socket_path=self.config.socket_path,
                        timeout_sec=deadline - loop.time(),
                    )
            return OrchardSandbox(
                request=request,
                gateway_host=gateway_host,
                orchard=client,
                client=guest,
                file_client=file_guest,
                name=name,
                uid=uid,
                agent_home=home,
                resolved_image=ResolvedImage(
                    kind=ImageKind.VM,
                    prepared_identity=image.prepared_identity,
                    observed_identity=observed_identity,
                    observed_ref=observed,
                ),
                allocation=ResourceAllocation(
                    cpus=int(record["assignedCPU"]),
                    memory_mb=int(record["assignedMemory"]),
                    storage_mb=storage,
                    sudo=request.sudo,
                    network_mode=request.network.mode,
                    provider=self.name,
                ),
            )
        except BaseException as provision_error:
            try:
                if uid is not None:
                    try:
                        await asyncio.shield(_delete_owned(client, _OwnedVM(name, uid)))
                    except ProviderStartError as cleanup_error:
                        raise ProviderStartError(
                            f"Provisioning failed ({type(provision_error).__name__}); "
                            f"cleanup incomplete: {cleanup_error}"
                        ) from provision_error
            finally:
                try:
                    if guest is not None:
                        await guest.close()
                finally:
                    try:
                        if file_guest is not None:
                            await file_guest.close()
                    finally:
                        await client.close()
            if readiness_started and isinstance(provision_error, TimeoutError):
                raise ProviderStartError(
                    f"Orchard VM {name} guest readiness exceeded {self.config.boot_timeout_sec:g}s"
                ) from provision_error
            raise
