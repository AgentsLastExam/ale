"""Verified Orchard REST and WebSocket boundary, and the local Orchard ALE runs on.

Closing an exec channel does not prove the guest command stopped. The owning
Provider must tear down its VM on cancellation or uncertain execution failure.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
import shlex
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from ale.core.errors import GuestUnreachableError, ProviderStartError
from ale.run.framework import MACOS_FRAMER
from ale.run.process import run

_CHUNK_BYTES = 16 * 1024
_FRAME_BYTES = 1024 * 1024
_LINE_BYTES = 16 * 1024 * 1024
#: Where ``scripts/setup_macos.py`` keeps the local controller, worker and their secrets.
STATE_DIR = Path.home() / ".local/share/ale/macos"
#: Covers scheduling, an image pull and boot.
PROVISION_TIMEOUT_SEC = 1800
#: The account Orchard logs in to every VM as, with its default password ``admin``.
CONTROL_ACCOUNT = "admin"
#: Where ``scripts/setup_macos.py`` installs ``framework_macos.sh``, owned by root, with a
#: sudoers rule that lets the Orchard user run this program alone as root.
FRAMER = Path("/usr/local/libexec/ale/framework-macos")
#: Pulling a macOS image of tens of gigabytes.
PULL_TIMEOUT_SEC = 7200
#: A guest's own shutdown, which writes everything it holds to disk.
SHUTDOWN_TIMEOUT_SEC = 180


@dataclass(frozen=True)
class OrchardExecResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


async def _refuse_redirect(
    session: aiohttp.ClientSession,
    context: object,
    params: aiohttp.TraceRequestRedirectParams,
) -> None:
    # WebSocket handshakes use ClientSession.request, including its redirect hooks.
    raise GuestUnreachableError("Orchard redirects are not permitted")


class OrchardClient:
    """One authenticated controller connection; never retries mutations."""

    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        ca_file: str | None = None,
        server_hostname: str | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Orchard requires an HTTPS URL without credentials, query or fragment")
        self._url = base_url.rstrip("/")
        if not self._url.endswith("/v1"):
            self._url += "/v1"
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self._headers = {"Authorization": "Basic " + credentials}
        self._ssl = ssl.create_default_context(cafile=ca_file)
        self._server_hostname = server_hostname
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None:
            trace = aiohttp.TraceConfig()
            trace.on_request_redirect.append(_refuse_redirect)
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                trace_configs=[trace],
                trust_env=False,
                # REST calls and WebSocket handshakes have separate deadlines.
                timeout=aiohttp.ClientTimeout(total=None),
            )

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    async def __aenter__(self) -> OrchardClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise ProviderStartError("Orchard client has not been started")
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        missing_ok: bool = False,
    ) -> object:
        try:
            async with self._require_session().request(
                method,
                self._url + path,
                json=body,
                ssl=self._ssl,
                server_hostname=self._server_hostname,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 404 and missing_ok:
                    return None
                if response.status < 200 or response.status >= 300:
                    raise ProviderStartError(f"Orchard request failed (HTTP {response.status})")
                if response.status == 204:
                    return None
                body = bytearray()
                while chunk := await response.content.read(64 * 1024):
                    body.extend(chunk)
                    if len(body) > _FRAME_BYTES:
                        raise ProviderStartError("Orchard response exceeds its size limit")
                return json.loads(body) if body else None
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
            raise ProviderStartError(
                "Orchard connection or response failed; the request was not retried"
            ) from None

    async def list_workers(self) -> list[dict[str, object]]:
        result = await self._request("GET", "/workers")
        if not isinstance(result, list) or any(not isinstance(worker, dict) for worker in result):
            raise ProviderStartError("Orchard workers response has an invalid shape")
        return result

    async def list_vms(self) -> list[dict[str, object]]:
        result = await self._request("GET", "/vms")
        if not isinstance(result, list) or any(not isinstance(vm, dict) for vm in result):
            raise ProviderStartError("Orchard VMs response has an invalid shape")
        return result

    async def get_vm(self, name: str) -> dict[str, object] | None:
        result = await self._request("GET", "/vms/" + quote(name, safe=""), missing_ok=True)
        if result is not None and not isinstance(result, dict):
            raise ProviderStartError("Orchard VM response has an invalid shape")
        return result

    async def create_vm(self, spec: Mapping[str, object]) -> dict[str, object]:
        result = await self._request("POST", "/vms", spec)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("uid"), str)
            or not result["uid"]
        ):
            raise ProviderStartError(
                "Orchard creation did not return a VM UID; do not retry creation"
            )
        return result

    async def update_vm(self, name: str, spec: Mapping[str, object]) -> dict[str, object]:
        """Update an owned VM specification; caller supplies preserved VMSpec fields.

        Orchard's endpoint does not offer a UID precondition. The Provider must
        check ownership before calling and validate the response UID afterwards.
        """
        result = await self._request("PUT", "/vms/" + quote(name, safe=""), spec)
        if not isinstance(result, dict):
            raise ProviderStartError("Orchard update returned an invalid VM response")
        return result

    async def delete_vm(self, name: str) -> None:
        await self._request("DELETE", "/vms/" + quote(name, safe=""), missing_ok=True)

    async def _socket(
        self, name: str, argv: Sequence[str], *, deadline: float | None = None
    ) -> aiohttp.ClientWebSocketResponse:
        parsed = urlsplit(self._url)
        url = urlunsplit(
            ("wss", parsed.netloc, parsed.path + "/vms/" + quote(name, safe="") + "/exec", "", "")
        )
        remaining = 30.0 if deadline is None else deadline - asyncio.get_running_loop().time()
        try:
            if remaining <= 0:
                raise TimeoutError("Guest startup deadline expired")
            async with asyncio.timeout(remaining):
                return await self._require_session().ws_connect(
                    url,
                    params={
                        "command": shlex.join(argv),
                        "interactive": "true",
                        # Orchard waits for SSH readiness before executing the command.
                        "wait": str(math.ceil(remaining)),
                    },
                    ssl=self._ssl,
                    server_hostname=self._server_hostname,
                    max_msg_size=_FRAME_BYTES,
                )
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
            raise GuestUnreachableError(
                "Orchard exec connection failed; command was not retried"
            ) from None

    async def connect_guest(
        self, name: str, argv: Sequence[str], *, deadline: float | None = None
    ) -> OrchardTransport:
        return OrchardTransport(await self._socket(name, argv, deadline=deadline))

    async def exec(
        self,
        name: str,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        timeout_sec: float = 120,
        max_output_bytes: int = _LINE_BYTES,
        deadline: float | None = None,
    ) -> OrchardExecResult:
        """Bounded bootstrap command; read output while sending stdin."""
        if timeout_sec <= 0 or max_output_bytes < 0:
            raise ValueError("Orchard exec limits must be positive")
        socket = await self._socket(name, argv, deadline=deadline)
        stdout, stderr = bytearray(), bytearray()

        async def receive() -> int:
            while True:
                frame = await _receive_frame(socket)
                kind = frame.get("type")
                if kind == "exit":
                    result = frame.get("exit")
                    code = result.get("code") if isinstance(result, dict) else None
                    if type(code) is not int:
                        raise GuestUnreachableError("Orchard exit response is invalid")
                    return code
                if kind not in {"stdout", "stderr"}:
                    raise GuestUnreachableError("Orchard command failed or returned invalid output")
                data = _decode_data(frame)
                if len(stdout) + len(stderr) + len(data) > max_output_bytes:
                    raise GuestUnreachableError("Orchard command output exceeds its limit")
                (stdout if kind == "stdout" else stderr).extend(data)

        reader = asyncio.create_task(receive())
        try:
            async with asyncio.timeout(timeout_sec):
                await _send_bytes(socket, stdin)
                await socket.send_str(json.dumps({"type": "stdin", "data": ""}))
                code = await reader
            return OrchardExecResult(code, bytes(stdout), bytes(stderr))
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise GuestUnreachableError(
                "Orchard command interrupted; its completion is unknown"
            ) from None
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, GuestUnreachableError):
                await reader
            await socket.close()


async def _send_bytes(socket: aiohttp.ClientWebSocketResponse, payload: bytes) -> None:
    for offset in range(0, len(payload), _CHUNK_BYTES):
        await socket.send_str(
            json.dumps(
                {
                    "type": "stdin",
                    "data": base64.b64encode(payload[offset : offset + _CHUNK_BYTES]).decode(
                        "ascii"
                    ),
                }
            )
        )


async def _receive_frame(socket: aiohttp.ClientWebSocketResponse) -> dict[str, object]:
    try:
        message = await socket.receive()
        if message.type != aiohttp.WSMsgType.TEXT or len(message.data) > _FRAME_BYTES:
            raise ValueError
        frame = json.loads(message.data)
        if not isinstance(frame, dict):
            raise ValueError
        return frame
    except (aiohttp.ClientError, OSError, ValueError, TypeError):
        raise GuestUnreachableError(
            "Orchard exec channel closed or returned an invalid frame"
        ) from None


def _decode_data(frame: dict[str, object]) -> bytes:
    try:
        data = frame["data"]
        if not isinstance(data, str):
            raise ValueError
        return base64.b64decode(data, validate=True)
    except (KeyError, ValueError, TypeError):
        raise GuestUnreachableError("Orchard exec returned invalid encoded output") from None


class OrchardTransport:
    """Persistent guestd stdio through Orchard, with bounded, eager output draining."""

    def __init__(
        self,
        socket: aiohttp.ClientWebSocketResponse,
        *,
        max_line_bytes: int = _LINE_BYTES,
        max_buffer_bytes: int = 2 * _LINE_BYTES,
    ) -> None:
        self._socket = socket
        self._max_line = max_line_bytes
        self._max_buffer = max_buffer_bytes
        self._buffered = 0
        self._lines: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._failure: GuestUnreachableError | None = None
        self._closed = False
        self._send_lock = asyncio.Lock()
        self._reader = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        pending = bytearray()
        try:
            while True:
                frame = await _receive_frame(self._socket)
                kind = frame.get("type")
                if kind == "stderr":
                    _decode_data(frame)  # Never include remote stderr in public exceptions.
                    continue
                if kind != "stdout":
                    raise GuestUnreachableError("Orchard guest service exited or failed")
                pending.extend(_decode_data(frame))
                if len(pending) + self._buffered > self._max_buffer:
                    raise GuestUnreachableError("Orchard guest output exceeds its buffer limit")
                while b"\n" in pending:
                    line, _, remaining = pending.partition(b"\n")
                    pending = bytearray(remaining)
                    if len(line) > self._max_line:
                        raise GuestUnreachableError("Orchard guest output exceeds its line limit")
                    if self._lines.qsize() >= 1024:
                        raise GuestUnreachableError("Orchard guest output exceeds its queue limit")
                    self._buffered += len(line) + 1
                    self._lines.put_nowait(bytes(line))
                if len(pending) > self._max_line:
                    raise GuestUnreachableError("Orchard guest output exceeds its line limit")
        except GuestUnreachableError as exc:
            self._failure = exc
        finally:
            self._lines.put_nowait(None)

    async def send(self, data: bytes) -> None:
        if self._failure is not None:
            raise self._failure
        if self._closed:
            raise GuestUnreachableError("Orchard guest channel is closed")
        try:
            async with self._send_lock:
                await _send_bytes(self._socket, data)
        except (aiohttp.ClientError, OSError):
            raise GuestUnreachableError(
                "Orchard guest send failed; request was not retried"
            ) from None

    async def recv(self) -> str:
        if self._failure is not None:
            raise self._failure
        if self._closed:
            raise GuestUnreachableError("Orchard guest channel is closed")
        line = await self._lines.get()
        if self._failure is not None:
            raise self._failure
        if line is None:
            raise GuestUnreachableError("Orchard guest channel is closed")
        self._buffered -= len(line) + 1
        try:
            return line.decode("utf-8")
        except UnicodeError:
            raise GuestUnreachableError("Orchard guest output is not UTF-8") from None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader
        await self._socket.close()


@dataclass(frozen=True)
class LocalOrchard:
    """The controller and Tart worker ``scripts/setup_macos.py`` runs on this Mac."""

    endpoint: str
    password: str
    ca_file: Path
    tart_binary: Path
    tart_home: Path

    @classmethod
    def load(cls, root: Path = STATE_DIR) -> LocalOrchard:
        try:
            state = json.loads((root / "state.json").read_text())
            password = (root / "password").read_text().strip()
            port = int(state["port"])
            tart = Path(state["binaries"]["tart"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ProviderStartError(
                "macOS Tasks run on the local Orchard; start it with "
                "`uv run scripts/setup_macos.py` and keep it running"
            ) from exc
        return cls(
            endpoint=f"https://localhost:{port}",
            password=password,
            ca_file=root / "controller.crt",
            tart_binary=tart,
            tart_home=root / "tart-home",
        )

    def client(self) -> OrchardClient:
        return OrchardClient(
            self.endpoint,
            username="bootstrap-admin",
            password=self.password,
            ca_file=str(self.ca_file),
        )

    async def tart(self, *args: str, timeout_sec: float = 600) -> str:
        """Run Tart on the worker's own images. Pruning stays off: the cache is the worker's."""
        env = {key: value for key, value in os.environ.items() if not key.startswith("TART_")}
        env |= {"TART_HOME": str(self.tart_home), "TART_NO_AUTO_PRUNE": "1"}
        code, stdout, stderr = await run(str(self.tart_binary), *args, env=env, timeout=timeout_sec)
        if code:
            raise ProviderStartError(f"tart {args[0]} failed: {stderr.strip()[-500:]}")
        return stdout

    async def local_images(self) -> set[str]:
        listing = json.loads(await self.tart("list", "--source", "local", "--format", "json"))
        return {str(entry["Name"]) for entry in listing}

    async def frame_disk(
        self, name: str, *, user: str, identity: str, payload: bytes
    ) -> dict[str, object]:
        """Install the framework into the stopped local VM ``name``; what it found as JSON."""
        check_framer()
        disk = self.tart_home / "vms" / name / "disk.img"
        code, stdout, stderr = await run(
            "/usr/bin/sudo", "-n", str(FRAMER), str(disk), user, CONTROL_ACCOUNT, identity,
            stdin=payload, timeout=600,
        )  # fmt: skip
        if code:
            detail = stderr.strip()[-1000:]
            raise ProviderStartError(f"could not install the macOS framework: {detail}")
        try:
            facts = json.loads(stdout)
        except ValueError:
            raise ProviderStartError("the macOS framer printed no result") from None
        if not isinstance(facts.get("agent_home"), str) or type(facts.get("agent_uid")) is not int:
            raise ProviderStartError("the macOS framer printed an invalid result")
        return facts

    async def _tart_vm(self, owned: OwnedVM) -> dict[str, object]:
        prefix = f"orchard-{owned.name}-{owned.uid}-"
        listing = json.loads(await self.tart("list", "--source", "local", "--format", "json"))
        found = [entry for entry in listing if str(entry["Name"]).startswith(prefix)]
        if len(found) != 1:
            raise ProviderStartError(f"cannot find the Tart VM of {owned.name} on the worker")
        return found[0]

    async def save(self, owned: OwnedVM, name: str) -> None:
        """Keep a stopped VM's disk as the local image ``name``; on APFS this copies no data."""
        source = await self._tart_vm(owned)
        await self.tart("clone", "--overwrite", str(source["Name"]), name)

    async def shut_down(self, client: OrchardClient, owned: OwnedVM) -> None:
        """Shut the guest down from inside, then wait until its Tart VM has stopped.

        Orchard stops a VM with ``tart stop --timeout 5``, which powers macOS off when its
        signed-in desktop does not shut down at once, losing what is still in memory, so a
        disk ALE keeps is shut down by its guest. Orchard then reports the VM failed, its
        record of a VM that exited by itself.
        """
        try:
            result = await client.exec(
                owned.name, ["sudo", "-n", "/sbin/shutdown", "-h", "now"], timeout_sec=30
            )
        except GuestUnreachableError:
            pass  # The guest went down with the command's session.
        else:
            if result.exit_code:
                detail = result.stderr.decode(errors="replace").strip()
                raise ProviderStartError(f"could not shut {owned.name} down: {detail}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SHUTDOWN_TIMEOUT_SEC
        while (await self._tart_vm(owned)).get("State") != "stopped":
            if loop.time() > deadline:
                raise ProviderStartError(
                    f"{owned.name} did not shut down within {SHUTDOWN_TIMEOUT_SEC}s"
                )
            await asyncio.sleep(2)
        owned.stopped = True


def check_framer() -> None:
    """Refuse to frame with an installed framer other than this ALE's own."""
    try:
        current = FRAMER.read_bytes() == MACOS_FRAMER.read_bytes()
    except OSError:
        current = False
    if not current:
        raise ProviderStartError(
            f"{FRAMER} is missing or differs from this ALE's; run "
            "`uv run scripts/setup_macos.py` to install it"
        )


@dataclass
class OwnedVM:
    name: str
    uid: str
    stopped: bool = False


def vm_spec(
    name: str, image: str, *, cpus: int, memory_mb: int, storage_mb: int | None = None
) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "image": image,
        "cpu": cpus,
        "memory": memory_mb,
        "os": "darwin",
        "arch": "arm64",
        "runtime": "tart",
        "headless": True,
        "restart_policy": "Never",
        "imagePullPolicy": "IfNotPresent",
        "resources": {
            "org.cirruslabs.tart-vms": 1,
            "org.cirruslabs.logical-cores": cpus,
            "org.cirruslabs.memory-mib": memory_mb,
        },
    }
    if storage_mb:
        spec["diskSize"] = storage_mb // 1024
    return spec


async def create_owned(client: OrchardClient, spec: Mapping[str, object]) -> OwnedVM:
    name = str(spec["name"])
    if await client.get_vm(name) is not None:
        raise ProviderStartError(f"Orchard VM name collision: {name}")
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
    return OwnedVM(name, str(created["uid"]))


async def await_running(client: OrchardClient, owned: OwnedVM) -> dict[str, object]:
    name, uid = owned.name, owned.uid
    last: object = "not observed"
    try:
        async with asyncio.timeout(PROVISION_TIMEOUT_SEC):
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
            f"Orchard VM {name} provisioning exceeded {PROVISION_TIMEOUT_SEC}s "
            f"while waiting for scheduling, image pull and boot; last status: {last}"
        ) from exc


async def stop_owned(client: OrchardClient, owned: OwnedVM) -> None:
    """Stop an owned VM and wait for its worker to acknowledge it."""
    if owned.stopped:
        return
    name, uid = owned.name, owned.uid
    record = await client.get_vm(name)
    if record is None:
        if owned.stopped:
            return
        raise ProviderStartError(f"Orchard VM {name} is absent; physical stop is unconfirmed")
    if record.get("uid") != uid:
        raise ProviderStartError(f"Refusing to stop Orchard VM {name}: UID changed")
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


async def delete_owned(client: OrchardClient, owned: OwnedVM) -> None:
    """Stop an owned VM, then delete it; never one whose UID changed."""
    await stop_owned(client, owned)
    name, uid = owned.name, owned.uid
    current = await client.get_vm(name)
    if current is None and owned.stopped:
        return
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
