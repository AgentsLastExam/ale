"""macOS sandboxes on the local Orchard controller and Tart worker.

``scripts/setup_macos.py`` runs both on this Mac. Every VM ALE creates is a clone of a local
image that carries the framework (``orchard_framing``). ALE owns only the VMs it creates,
which last one episode unless kept; it never adopts a VM it did not create, nor changes the
images it is given. Each sandbox the agent works in shows the desktop its image signs the
Task's account in to.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from ale.core.errors import GuestUnreachableError, ProviderCapabilityError, ProviderStartError
from ale.core.ids import content_hash
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
from ale.run.framework import MACOS_DRIVER, MACOS_DRIVER_SOCKET, MACOS_PYTHON
from ale.run.images import resolve_registry_reference
from ale.run.providers import orchard_client
from ale.run.providers.desktop import require_resolution
from ale.run.providers.orchard_client import (
    CONTROL_ACCOUNT,
    LocalOrchard,
    OrchardClient,
    OwnedVM,
    await_running,
    check_framer,
    create_owned,
    delete_owned,
    vm_spec,
)
from ale.run.providers.orchard_desktop import await_desktop
from ale.run.providers.orchard_framing import (
    FORGET_DISPLAYS,
    FramedImage,
    at_resolution,
    frame_image,
    framed_record,
    remove_locks,
)
from ale.run.providers.privileges import set_elevation, verify_identities
from ale.run.transport import GuestClient

_IMAGE = re.compile(
    r"^(?P<repository>[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?/"
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*)"
    r"(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)
_ROOT = "/opt/ale"
#: The framework's Python is named by its path, never found on PATH, as on Linux.
_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_READ_CHUNK_BYTES = 64 * 1024
#: From a framed clone's first boot to a guest whose account and desktop are checked.
_BOOT_TIMEOUT_SEC = 300
#: guestd from the sources framing installed, on the framework's Python.
_GUESTD = (
    "import runpy,sys; sys.path.insert(0,'/opt/ale/guestd'); "
    "runpy.run_path('/opt/ale/guestd/main.py',run_name='__main__')"
)
#: Prints the home of the account named by its argument, or nothing when there is none.
_ACCOUNT_HOME = (
    "import pwd,sys\ntry: print(pwd.getpwnam(sys.argv[1]).pw_dir)\nexcept KeyError: pass"
)

# Runs as root in the VM and forwards the fixed Gateway port on its loopback to the Host's
# Gateway until the VM is deleted or a later episode stops it. It forks only once it listens,
# so a port that cannot be bound fails the command; it prints the bound port and the relay's
# process ID.
_GATEWAY_RELAY = r"""
import asyncio,os,socket,sys
host,port,listen=sys.argv[1],int(sys.argv[2]),int(sys.argv[3])
listener=socket.create_server(('127.0.0.1',listen))
pid=os.fork()
if pid:
    print(listener.getsockname()[1],pid);sys.exit(0)
os.setsid()
null=os.open(os.devnull,os.O_RDWR)
for fd in (0,1,2): os.dup2(null,fd)
async def pipe(reader,writer):
    try:
        while data:=await reader.read(65536):
            writer.write(data);await writer.drain()
    except OSError:
        pass
async def relay(reader,writer):
    try:
        upstream_reader,upstream=await asyncio.open_connection(host,port)
    except OSError:
        writer.close();return
    flows=[asyncio.ensure_future(pipe(reader,upstream)),
           asyncio.ensure_future(pipe(upstream_reader,writer))]
    await asyncio.wait(flows,return_when=asyncio.FIRST_COMPLETED)
    for flow in flows: flow.cancel()
    writer.close();upstream.close()
async def main():
    server=await asyncio.start_server(relay,sock=listener)
    await server.serve_forever()
asyncio.run(main())
"""


#: How a handle names an Orchard VM: ``orchard:<name>:<uid>``.
HANDLE_PREFIX = "orchard:"
#: How a handle names a snapshot: ``orchard:snapshot:<id>``.
SNAPSHOT_PREFIX = f"{HANDLE_PREFIX}snapshot:"
#: The local Tart image that holds a snapshot, before its ID.
SNAPSHOT_IMAGE = "ale-snapshot-"

# Stops the Gateway relay listening on the fixed port, as a VM a later episode continues in
# has for the episode that kept it, and waits for the port to free.
_STOP_GATEWAY_RELAY = f"""
listening() {{ /usr/sbin/lsof -nP -t -iTCP@127.0.0.1:{SANDBOX_GATEWAY_PORT} -sTCP:LISTEN; }}
pids=$(listening) || exit 0
kill $pids
for _ in 1 2 3 4 5 6 7 8 9 10; do listening >/dev/null || exit 0; sleep 0.5; done
exit 1
"""


def _owned_vm(handle: str) -> tuple[str, str]:
    name, separator, uid = handle.removeprefix(HANDLE_PREFIX).partition(":")
    if not handle.startswith(HANDLE_PREFIX) or not separator or not name.startswith("ale-"):
        raise ProviderCapabilityError(f"{handle!r} is not an Orchard VM handle")
    return name, uid


@dataclass(frozen=True)
class _Retained:
    """A VM kept running past its episode, or a snapshot of one, as this Mac records it.

    Orchard keeps no labels on a VM, so the record is how ALE knows the VM is retained,
    and what a later episode boots or drives: the local image, and the account framing
    found in it.
    """

    kind: Literal["keep", "snapshot"]
    handle: str
    episode_id: str
    role: str
    prepared_identity: str
    """The Task's prepared image, which a later episode must have too."""
    user: str
    source: dict[str, object]
    """The :class:`FramedImage` it boots: the framed image, or the snapshot's image."""

    @property
    def boots(self) -> FramedImage:
        return FramedImage(**self.source)  # type: ignore[arg-type]


def _retained_dir() -> Path:
    return orchard_client.STATE_DIR / "retained"


def _record_path(handle: str) -> Path:
    return _retained_dir() / f"{hashlib.sha256(handle.encode()).hexdigest()[:32]}.json"


def _load(path: Path) -> _Retained | None:
    try:
        return _Retained(**json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def _record(handle: str) -> _Retained | None:
    return _load(_record_path(handle))


def _records() -> list[_Retained]:
    paths = sorted(_retained_dir().glob("*.json"))
    return [record for path in paths if (record := _load(path)) is not None]


def _remember(record: _Retained) -> None:
    path = _record_path(record.handle)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(asdict(record)))
    os.replace(partial, path)


def _forget(handle: str) -> None:
    _record_path(handle).unlink(missing_ok=True)


class OrchardSandbox(Sandbox):
    def __init__(
        self,
        *,
        request: SandboxRequest,
        local: LocalOrchard,
        orchard: OrchardClient,
        client: GuestClient,
        file_client: GuestClient,
        name: str,
        uid: str,
        agent_home: str,
        allocation: ResourceAllocation,
        resolved_image: ResolvedImage,
        source: FramedImage,
    ) -> None:
        super().__init__(
            sandbox_id=name,
            request=request,
            resolved_image=resolved_image,
            allocation=allocation,
            agent_user=request.user,
            agent_home=agent_home,
        )
        self._local = local
        self._orchard = orchard
        self._client = client
        self._file_client = file_client
        self._uid = uid
        self._owned = OwnedVM(name, uid)
        self._source = source
        self._destroy_lock = asyncio.Lock()
        self.state = SandboxState.READY

    @property
    def handle(self) -> str:
        return f"{HANDLE_PREFIX}{self._owned.name}:{self._uid}"

    @property
    def gateway_url(self) -> str | None:
        """The Gateway on the VM's own loopback, where the relay delivers it."""
        if not self.request.gateway_url:
            return None
        parsed = urlsplit(self.request.gateway_url)
        return urlunsplit(parsed._replace(netloc=f"127.0.0.1:{SANDBOX_GATEWAY_PORT}"))

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

    async def account_home(self, user: str) -> str | None:
        """The home of the account ``user`` in this VM, or None when it has no such account."""
        result = await self.exec([MACOS_PYTHON, "-I", "-S", "-c", _ACCOUNT_HOME, user])
        if not result.ok:
            raise ProviderStartError(f"could not look up the account {user!r}: {result.stderr}")
        return result.stdout.strip() or None

    async def save_image(self, image: PreparedTaskImage) -> None:
        """Stop this VM and keep its disk as the local image ``image`` names.

        A sandbox frames that image for its Task's account when it first uses it.
        """
        await self._local.shut_down(self._orchard, self._owned)
        await self._local.save(self._owned, image.runtime_ref)

    def _retained(
        self,
        kind: Literal["keep", "snapshot"],
        handle: str,
        source: FramedImage,
        roles: tuple[Literal["solver", "verifier"], ...],
        reason: str,
    ) -> RetainedSandbox:
        """Record what ``handle`` keeps, unless a record of it exists: a VM a later episode
        continued in and keeps again stays recorded as the episode that first kept it."""
        if _record(handle) is None:
            _remember(
                _Retained(
                    kind=kind,
                    handle=handle,
                    episode_id=self.request.episode_id,
                    role=self.request.role.value,
                    prepared_identity=self.request.prepared_image.prepared_identity,
                    user=self.request.user,
                    source=asdict(source),
                )
            )
        return RetainedSandbox(
            provider=OrchardProvider.name,
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"ale sandbox destroy {handle}",
        )

    async def retain(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        """Keep the VM running past its episode, holding its VM slot, for a later episode
        to continue in; its desktop session and processes stay as they are."""
        retained = self._retained("keep", self.handle, self._source, roles, reason)
        await asyncio.gather(
            self._client.close(), self._file_client.close(), return_exceptions=True
        )
        await self._orchard.close()
        return retained

    async def snapshot(
        self, *, roles: tuple[Literal["solver", "verifier"], ...], reason: str
    ) -> RetainedSandbox:
        """Shut the VM down from inside and keep its disk as a local image later VMs of the
        same Task image and account boot from. On APFS the image copies no data.

        The display mode its desktop saved is forgotten first, as framing forgets the first
        login's, so each VM booted from it starts at its own resolution.
        """
        snapshot_id = self._owned.name.removeprefix("ale-")
        image = f"{SNAPSHOT_IMAGE}{snapshot_id}"
        forgotten = await self._orchard.exec(self._owned.name, ["sudo", "-n", *FORGET_DISPLAYS])
        if forgotten.exit_code:
            detail = forgotten.stderr.decode(errors="replace").strip()
            raise ProviderStartError(f"could not forget the VM's display mode: {detail}")
        await self._local.shut_down(self._orchard, self._owned)
        await self._local.save(self._owned, image)
        source = FramedImage(
            name=image,
            identity=content_hash({"snapshot": snapshot_id, "of": self._source.identity}),
            agent_home=self._source.agent_home,
            agent_uid=self._source.agent_uid,
        )
        return self._retained("snapshot", f"{SNAPSHOT_PREFIX}{snapshot_id}", source, roles, reason)

    async def destroy(self) -> None:
        async with self._destroy_lock:
            if self.state is SandboxState.DESTROYED:
                return
            # A failed cleanup retains its UID and stop acknowledgement for an
            # explicit retry, while releasing host-side sockets in every outcome.
            await self._orchard.start()
            try:
                await delete_owned(self._orchard, self._owned)
                _forget(self.handle)
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

    def __init__(self, local: LocalOrchard | None = None) -> None:
        self._local_orchard = local

    def _local(self) -> LocalOrchard:
        if self._local_orchard is None:
            self._local_orchard = LocalOrchard.load()
        return self._local_orchard

    def capabilities(self) -> Capabilities:
        return Capabilities(
            operating_systems=frozenset({OperatingSystem.MACOS}),
            # Framing requires the image to sign the Task's account in to a desktop.
            gui=True,
            network_modes=frozenset({NetworkMode.OPEN}),
            snapshot=True,
        )

    async def preflight(self) -> None:
        check_framer()
        client = self._local().client()
        try:
            await client.start()
            workers = await client.list_workers()
        except ProviderStartError as exc:
            raise ProviderStartError(
                f"the local Orchard did not answer: {exc}. Start it with "
                "`uv run scripts/setup_macos.py` and keep it running"
            ) from None
        finally:
            await client.close()
        workers = [
            worker
            for worker in workers
            if worker.get("arch") == "arm64"
            and worker.get("runtime") == "tart"
            and worker.get("scheduling_paused", False) is False
        ]
        if not workers:
            raise ProviderStartError("the local Orchard has no ARM64 Tart worker taking VMs")
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
            # Small Host/worker clock skew is tolerated; capacity is not checked here
            # because a busy worker must remain eligible for queueing.
            if -30 <= age <= 180:
                return
        raise ProviderStartError(
            "the local Orchard worker has no fresh heartbeat (180 seconds); "
            "restart it with `uv run scripts/setup_macos.py`"
        )

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if image.kind is not ImageKind.VM:
            raise ProviderCapabilityError("Orchard only accepts VM images")
        if isinstance(image, PreparedTaskImage) and image.source != "external-ref":
            # A Task image ALE built and keeps on the local worker.
            if image.runtime_ref not in await self._local().local_images():
                raise ProviderCapabilityError(f"the local image {image.runtime_ref} is missing")
            return image
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
        digest = reference.rsplit("@", 1)[1]
        result = PreparedTaskImage(
            kind=ImageKind.VM,
            source="external-ref",
            input_identity=digest,
            runtime_ref=reference,
            prepared_identity=digest,
            resolved_reference=reference,
            base_materials=(reference,),
        )
        if isinstance(image, PreparedTaskImage) and image != result:
            raise ProviderCapabilityError("the prepared Orchard image does not match its reference")
        return result

    def accepts(self, request: SandboxRequest) -> None:
        super().accepts(request)
        if request.resources.gpus:
            raise ProviderCapabilityError("Orchard GPU allocations are not supported")
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
        if (
            request.user == CONTROL_ACCOUNT
            and not request.sudo
            and request.role is not SandboxRole.VERIFIER
        ):
            # Revoking would take the account's sudo from ALE too, which runs as root
            # through it.
            raise ProviderCapabilityError(
                f"ALE controls macOS VMs through the {CONTROL_ACCOUNT} account, which keeps "
                "passwordless sudo, so a Task that runs as it must declare [agent].sudo = true; "
                "use another account for a Task without elevated privileges"
            )

    async def _resolve_gateway_host(
        self, guest: GuestClient, request: SandboxRequest
    ) -> str | None:
        if not request.gateway_url:
            return None
        # In Tart's NAT network, the guest's router is its Mac host.
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
            "could not find the Mac from the VM's default IPv4 route; "
            "the local Tart worker must use its default NAT network"
        )

    async def _start_gateway_relay(
        self, guest: GuestClient, host: str, request: SandboxRequest, *, replace: bool = False
    ) -> None:
        """Relay the fixed Gateway port in the VM to this request's Gateway; ``replace``
        stops the relay an earlier episode started first."""
        assert request.gateway_url is not None
        port = urlsplit(request.gateway_url).port
        if port is None:
            raise ProviderCapabilityError(f"{request.gateway_url!r} has no explicit port to relay")
        if replace:
            code, _, stderr, timed_out = await guest.exec(
                ["/bin/sh", "-c", _STOP_GATEWAY_RELAY], timeout_sec=15
            )
            if code or timed_out:
                raise ProviderStartError(f"Could not stop the VM's Gateway relay: {stderr}")
        code, _, _, timed_out = await guest.exec(
            [
                MACOS_PYTHON,
                "-I",
                "-S",
                "-c",
                _GATEWAY_RELAY,
                host,
                str(port),
                str(SANDBOX_GATEWAY_PORT),
            ],
            timeout_sec=15,
        )
        if code or timed_out:
            raise ProviderStartError("Could not start the Gateway relay in the VM")

    async def _open_guests(
        self,
        client: OrchardClient,
        name: str,
        request: SandboxRequest,
        agent_home: str,
        *,
        agent: bool,
        deadline: float,
    ) -> tuple[GuestClient, GuestClient]:
        """Two guest service sessions in the VM, the second for file reads while the first
        runs a long command, after checking the guest is the account's framed macOS."""
        env = dict(request.env) | {
            "ALE_AGENT_USER": request.user,
            "ALE_AGENT_HOME": agent_home,
            "PATH": _PATH,
            "HOME": "/var/root",
        }
        if agent:
            env["ALE_CUA_DRIVER"] = MACOS_DRIVER
            env["ALE_CUA_DRIVER_SOCKET"] = MACOS_DRIVER_SOCKET
        guest_argv = [
            "sudo",
            "-n",
            "/usr/bin/env",
            "-i",
            *(f"{k}={v}" for k, v in env.items()),
            MACOS_PYTHON,
            "-I",
            "-S",
            "-c",
            _GUESTD,
            "--stdio",
        ]
        guest = GuestClient(
            await client.connect_guest(name, guest_argv, deadline=deadline),
            acknowledge_exec_output=True,
        )
        file_guest: GuestClient | None = None
        try:
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
                or health.get("agent_home") != agent_home
            ):
                raise ProviderCapabilityError("Guest macOS/account/root identity check failed")
        except BaseException:
            await guest.close()
            if file_guest is not None:
                await file_guest.close()
            raise
        return guest, file_guest

    def _boot_source(self, request: SandboxRequest) -> FramedImage | None:
        """What a VM for ``request`` boots, as this Mac recorded it: the snapshot it names,
        or the framed copy of its image."""
        if request.snapshot is None:
            return framed_record(request.prepared_image, user=request.user)
        record = _record(request.snapshot)
        if record is None or record.kind != "snapshot":
            raise ProviderCapabilityError(f"there is no Orchard snapshot {request.snapshot}")
        if (record.prepared_identity, record.user) != (
            request.prepared_image.prepared_identity,
            request.user,
        ):
            raise ProviderCapabilityError(
                f"{request.snapshot} was taken from another Task image or account"
            )
        return record.boots

    async def _drive(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
        source: FramedImage | None,
        *,
        relay: bool,
    ) -> OrchardSandbox:
        """A running ALE VM booted from ``source``, driven through new guest sessions;
        ``relay`` points its Gateway relay at this request's Gateway."""
        name, uid = _owned_vm(handle)
        if source is None or source.identity != image.observed_identity:
            raise ProviderCapabilityError(f"{handle!r} does not run an image recorded here")
        client = self._local().client()
        try:
            await client.start()
            record = await client.get_vm(name)
            if record is None or record.get("uid") != uid:
                raise ProviderStartError(f"Orchard VM {name} is gone or was replaced")
            if not str(record.get("image", "")).startswith(source.name):
                raise ProviderCapabilityError(f"{handle!r} does not run the recorded image")
            deadline = asyncio.get_running_loop().time() + 120
            guest, file_guest = await self._open_guests(
                client,
                name,
                request,
                source.agent_home,
                agent=request.role is not SandboxRole.VERIFIER,
                deadline=deadline,
            )
            try:
                if relay and (host := await self._resolve_gateway_host(guest, request)):
                    await self._start_gateway_relay(guest, host, request, replace=True)
            except BaseException:
                await guest.close()
                await file_guest.close()
                raise
        except BaseException:
            await client.close()
            raise
        return OrchardSandbox(
            request=request,
            local=self._local(),
            orchard=client,
            client=guest,
            file_client=file_guest,
            name=name,
            uid=uid,
            agent_home=source.agent_home,
            resolved_image=image,
            allocation=allocation,
            source=source,
        )

    async def reattach(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
    ) -> OrchardSandbox:
        """A running VM this Mac's Orchard started for ALE, driven again through new guest
        sessions; the Gateway relay inside it outlives the sessions that started it.

        A kept VM is checked against the image its record names: the request of a later
        episode that continues in one booted from a snapshot names no snapshot.
        """
        record = _record(handle)
        if record is not None and record.kind == "keep":
            source: FramedImage | None = record.boots
        else:
            source = self._boot_source(request)
        return await self._drive(handle, request, allocation, image, source, relay=False)

    async def attach_retained(
        self,
        handle: str,
        request: SandboxRequest,
        allocation: ResourceAllocation,
        image: ResolvedImage,
    ) -> OrchardSandbox:
        """A VM an earlier episode kept, for this episode to continue in, with its Gateway
        relay pointed at this episode's Gateway."""
        record = _record(handle)
        if (
            record is None
            or record.kind != "keep"
            or record.role not in {SandboxRole.SOLVER.value, SandboxRole.SHARED.value}
        ):
            raise ProviderCapabilityError(f"{handle!r} is not a retained Orchard solver VM")
        return await self._drive(handle, request, allocation, image, record.boots, relay=True)

    async def list_retained(self) -> list[dict[str, object]]:
        records = _records()
        if not records:
            return []  # touches no Orchard, which a Mac without one does not have
        local = self._local()
        images = await local.local_images()
        client = local.client()
        found: list[dict[str, object]] = []
        try:
            await client.start()
            for record in records:
                if record.kind == "keep":
                    name, uid = _owned_vm(record.handle)
                    vm = await client.get_vm(name)
                    present, running = (
                        vm is not None and vm.get("uid") == uid,
                        vm is not None and vm.get("status") == "running",
                    )
                else:
                    present, running = record.boots.name in images, False
                if not present:
                    _forget(record.handle)  # deleted outside ALE
                    continue
                found.append(
                    {
                        "provider": self.name,
                        "handle": record.handle,
                        "episode": record.episode_id,
                        "role": record.role,
                        "image": record.boots.name,
                        "gpus": (),
                        "running": running,
                        "cleanup_command": f"ale sandbox destroy {record.handle}",
                    }
                )
        finally:
            await client.close()
        return found

    async def destroy_retained(self, handle: str) -> None:
        record = _record(handle)
        if record is None:
            raise ProviderCapabilityError(f"{handle!r} is not a retained Orchard VM or snapshot")
        if record.kind == "keep":
            await self.discard(handle)
            return
        image = record.boots.name
        local = self._local()
        client = local.client()
        try:
            await client.start()
            vms = await client.list_vms()
        finally:
            await client.close()
        # Driving such a VM again after a crash needs the snapshot's record.
        if any(str(vm.get("image", "")).startswith(image) for vm in vms):
            raise ProviderStartError(f"VMs still boot from {handle}; destroy them first")
        # Its clones at each desktop resolution go first, so a failure leaves the image and
        # its record to try again from.
        images = await local.local_images()
        clones = sorted(name for name in images if name.startswith(f"{image}-"))
        for name in [*clones, *([image] if image in images else [])]:
            await local.tart("delete", name)
        remove_locks(image)
        _forget(handle)

    async def discard(self, handle: str) -> None:
        name, uid = _owned_vm(handle)
        client = self._local().client()
        try:
            await client.start()
            record = await client.get_vm(name)
            if record is None:
                _forget(handle)
                return
            if record.get("uid") != uid:
                raise ProviderStartError(f"Orchard VM {name} was replaced; not deleting it")
            await delete_owned(client, OwnedVM(name, uid))
            _forget(handle)
        finally:
            await client.close()

    async def create(self, request: SandboxRequest) -> OrchardSandbox:
        # A verifier runs no agent: it needs no desktop, and its account's rights are moot.
        return await self._create(request, agent=request.role is not SandboxRole.VERIFIER)

    async def create_builder(self, request: SandboxRequest) -> OrchardSandbox:
        """A VM of the framed base for ``build.sh``, which runs as root and needs no agent.

        Nothing is changed for an agent either: the image is kept as the build left it.
        """
        return await self._create(request, agent=False)

    async def _create(self, request: SandboxRequest, *, agent: bool) -> OrchardSandbox:
        self.accepts(request)
        image = await self.prepare_image(request.prepared_image)
        local = self._local()
        if request.snapshot is None:
            framed = await frame_image(local, image, user=request.user)
        else:
            source = self._boot_source(request)
            assert source is not None
            if source.name not in await local.local_images():
                raise ProviderCapabilityError(f"the snapshot {request.snapshot} is gone")
            framed = source
        boot = await at_resolution(local, framed, request.desktop_resolution)
        client = local.client()
        owned: OwnedVM | None = None
        guest: GuestClient | None = None
        file_guest: GuestClient | None = None
        readiness_started = False
        try:
            await client.start()
            spec = vm_spec(
                "ale-" + uuid.uuid4().hex,
                boot,
                cpus=request.resources.cpus,
                memory_mb=request.resources.memory_mb,
                storage_mb=request.resources.storage_mb,
            )
            owned = await create_owned(client, spec)
            record = await await_running(client, owned)
            readiness_started = True
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _BOOT_TIMEOUT_SEC
            async with asyncio.timeout_at(deadline):
                for key in ("image", "cpu", "memory", "os", "arch", "runtime"):
                    if record.get(key) != spec[key]:
                        raise ProviderStartError(f"Orchard VM does not attest requested {key}")
                if (
                    record.get("assignedCPU") != request.resources.cpus
                    or record.get("assignedMemory") != request.resources.memory_mb
                ):
                    raise ProviderStartError("Orchard assigned resources differ from request")
                guest, file_guest = await self._open_guests(
                    client, owned.name, request, framed.agent_home, agent=agent, deadline=deadline
                )
                gateway_host = await self._resolve_gateway_host(guest, request)
                if gateway_host is not None:
                    await self._start_gateway_relay(guest, gateway_host, request)
                if agent:
                    await set_elevation(
                        guest,
                        request.user,
                        sudo=request.sudo,
                        operating_system=OperatingSystem.MACOS,
                    )
                    await verify_identities(guest, request.user, sudo=request.sudo)
                total, _, _ = await guest.disk_usage("/")
                storage = total // (1024 * 1024)
                if request.resources.storage_mb and storage < request.resources.storage_mb:
                    raise ProviderCapabilityError("VM root disk is smaller than requested storage")
                desktop: DesktopResolution | None = None
                if agent:
                    await await_desktop(
                        guest,
                        agent_user=request.user,
                        agent_uid=framed.agent_uid,
                        timeout_sec=deadline - loop.time(),
                    )
                    desktop = await require_resolution(guest, request.desktop_resolution)
            return OrchardSandbox(
                request=request,
                local=local,
                orchard=client,
                client=guest,
                file_client=file_guest,
                name=owned.name,
                uid=owned.uid,
                agent_home=framed.agent_home,
                source=framed,
                resolved_image=ResolvedImage(
                    kind=ImageKind.VM,
                    prepared_identity=image.prepared_identity,
                    observed_identity=framed.identity,
                    observed_ref=framed.name,
                ),
                allocation=ResourceAllocation(
                    cpus=int(record["assignedCPU"]),
                    memory_mb=int(record["assignedMemory"]),
                    storage_mb=storage,
                    sudo=request.sudo,
                    network_mode=request.network.mode,
                    provider=self.name,
                    desktop_resolution=desktop,
                ),
            )
        except BaseException as provision_error:
            try:
                if owned is not None:
                    try:
                        await asyncio.shield(delete_owned(client, owned))
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
                    f"Orchard VM guest readiness exceeded {_BOOT_TIMEOUT_SEC}s"
                ) from provision_error
            raise
