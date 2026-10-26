"""Verified Orchard REST and WebSocket boundary; no Lab service dependency.

Closing an exec channel does not prove the guest command stopped. The owning
Provider must tear down its VM on cancellation or uncertain execution failure.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import shlex
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from ale.core.errors import GuestUnreachableError, ProviderStartError

_CHUNK_BYTES = 16 * 1024
_FRAME_BYTES = 1024 * 1024
_LINE_BYTES = 16 * 1024 * 1024


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

    @contextlib.asynccontextmanager
    async def forward_vnc(self, name: str, uid: str):
        """A scoped loopback listener for one owned VM's Screen Sharing port."""
        tasks: set[asyncio.Task] = set()

        def accept(reader, writer):
            task = asyncio.create_task(self._relay_vnc(name, uid, reader, writer))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        try:
            yield server.sockets[0].getsockname()[1]
        finally:
            server.close()
            pending = list(tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await server.wait_closed()

    async def _relay_vnc(self, name, uid, reader, writer):
        socket = None
        upstream = None
        try:
            record = await self.get_vm(name)
            if record is None or record.get("uid") != uid:
                return
            url = self._url + "/vms/" + quote(name, safe="") + "/port-forward"
            async with asyncio.timeout(30):
                socket = await self._require_session().ws_connect(
                    url,
                    params={"port": "5900", "wait": "20"},
                    ssl=self._ssl,
                    server_hostname=self._server_hostname,
                    max_msg_size=16 * _FRAME_BYTES,
                )

            async def send():
                while data := await reader.read(64 * 1024):
                    await socket.send_bytes(data)
                await socket.close()

            upstream = asyncio.create_task(send())
            async for message in socket:
                if message.type != aiohttp.WSMsgType.BINARY:
                    break
                writer.write(message.data)
                await writer.drain()
        except (aiohttp.ClientError, OSError, TimeoutError, ProviderStartError):
            # Transport failure closes RFB; never expose controller credentials.
            pass
        finally:
            if upstream is not None:
                upstream.cancel()
                await asyncio.gather(upstream, return_exceptions=True)
            if socket is not None:
                await socket.close()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

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
