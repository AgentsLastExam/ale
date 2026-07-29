"""Host side of the guest protocol.

One client, two transports. Containers get a piped ``exec`` session (no published
ports, no readiness handshake, and roughly an order of magnitude less latency than an
in-container HTTP server); virtual machines get a socket through a forwarded port.
Callers see neither difference.

Requests are serialised per connection: the guest answers one at a time, and a trace
that can be replayed is worth more than overlapping streams.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from ale.core.errors import GuestUnreachableError, OutputStreamError, ProviderStartError
from ale.core.sandbox import ExecOutputSink

__all__ = ["GuestClient", "StdioTransport", "TcpTransport", "Transport"]

_READ_LIMIT = 16 * 1024 * 1024  # generous: file chunks arrive base64-encoded
_OUTPUT_PREVIEW_BYTES = 64 * 1024


class Transport(Protocol):
    """A line-oriented duplex channel to one guest service."""

    async def send(self, line: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


class StdioTransport:
    """A long-lived ``docker exec -i`` session speaking JSON Lines."""

    def __init__(self, argv: Sequence[str]) -> None:
        self._argv = list(argv)
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_READ_LIMIT,
        )

    async def send(self, line: str) -> None:
        proc = self._require()
        assert proc.stdin is not None
        proc.stdin.write((line + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def recv(self) -> str:
        proc = self._require()
        assert proc.stdout is not None
        raw = await proc.stdout.readline()
        if not raw:
            stderr = b""
            if proc.stderr is not None:
                stderr = await proc.stderr.read()
            detail = stderr.decode("utf-8", "replace").strip()
            raise GuestUnreachableError(f"guest session closed{': ' + detail if detail else ''}")
        return raw.decode("utf-8").strip()

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, ProcessLookupError):
            proc.kill()

    def _require(self) -> asyncio.subprocess.Process:
        if self._proc is None:
            raise GuestUnreachableError("transport is not started")
        return self._proc


class TcpTransport:
    """A socket to a guest service reached through a forwarded port."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def start(self, *, timeout_sec: float = 120, interval_sec: float = 1.0) -> None:
        """Connect and exchange one message, retrying until the deadline passes.

        Connecting is not evidence that anyone is listening. When the port is published by
        a container runtime, its forwarder binds immediately and accepts long before the
        guest inside has booted — the connection succeeds and the first read is reset.
        Readiness is therefore a request that came back, not a socket that opened.
        """
        deadline = asyncio.get_running_loop().time() + timeout_sec
        last: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    self._host, self._port, limit=_READ_LIMIT
                )
                await self.send(json.dumps({"id": "ready", "op": "health", "params": {}}))
                await asyncio.wait_for(self.recv(), timeout=interval_sec * 10)
                return
            except (OSError, GuestUnreachableError, TimeoutError) as exc:
                last = exc
                await self.close()
                await asyncio.sleep(interval_sec)
        raise ProviderStartError(
            f"guest service at {self._host}:{self._port} did not answer within "
            f"{timeout_sec:g}s ({last})"
        )

    async def send(self, line: str) -> None:
        if self._writer is None:
            raise GuestUnreachableError("transport is not started")
        self._writer.write((line + "\n").encode("utf-8"))
        await self._writer.drain()

    async def recv(self) -> str:
        if self._reader is None:
            raise GuestUnreachableError("transport is not started")
        raw = await self._reader.readline()
        if not raw:
            raise GuestUnreachableError("guest closed the connection")
        return raw.decode("utf-8").strip()

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(OSError, asyncio.CancelledError):
            await writer.wait_closed()


class GuestClient:
    """Typed operations over any transport.

    Interim events (streamed output, file chunks) are collected here so callers get one
    complete result instead of a stream to reassemble.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._lock = asyncio.Lock()
        self._next_id = 0

    async def call(
        self,
        op: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_sec: float | None = None,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Perform one operation; returns its data and any interim events."""
        async with self._lock:
            self._next_id += 1
            req_id = self._next_id
            payload = json.dumps(
                {"id": req_id, "op": op, "params": params or {}},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await self._transport.send(payload)
            events: list[dict[str, Any]] = []
            callback_error: BaseException | None = None
            while True:
                line = await asyncio.wait_for(self._transport.recv(), timeout=timeout_sec)
                if not line:
                    continue
                message = json.loads(line)
                if message.get("id") != req_id:
                    continue  # a stale reply from an abandoned call
                if "event" in message:
                    if event_callback is None:
                        events.append(message)
                    elif callback_error is None:
                        try:
                            await event_callback(message)
                        except BaseException as exc:
                            callback_error = exc
                    continue
                if message.get("ok"):
                    if callback_error is not None:
                        raise OutputStreamError(
                            f"output callback failed: {callback_error}"
                        ) from callback_error
                    return message.get("data") or {}, events
                error = message.get("error") or {}
                raise GuestUnreachableError(
                    f"guest operation {op!r} failed [{error.get('code')}]: {error.get('message')}"
                )

    async def health(self) -> dict[str, Any]:
        data, _ = await self.call("health", timeout_sec=30)
        return data

    async def exec(
        self,
        argv: Sequence[str] | None = None,
        *,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        run_as: str | None = None,
        output_sink: ExecOutputSink | None = None,
    ) -> tuple[int | None, str, str, bool]:
        params: dict[str, Any] = {}
        if run_as:
            params["run_as"] = run_as
        if argv:
            params["argv"] = list(argv)
        if shell:
            params["shell"] = shell
        if cwd:
            params["cwd"] = cwd
        if env:
            params["env"] = env
        if timeout_sec:
            params["timeout_sec"] = timeout_sec

        # Allow the guest's own timeout to fire first: it can still report partial output.
        wait = timeout_sec + 30 if timeout_sec else None
        stdout = bytearray()
        stderr = bytearray()

        async def receive(message: dict[str, Any]) -> None:
            name = message.get("event")
            if name not in {"stdout_chunk", "stderr_chunk"}:
                return
            block = base64.b64decode(message["data"]["b64"])
            preview = stdout if name == "stdout_chunk" else stderr
            limit = None if output_sink is None else _OUTPUT_PREVIEW_BYTES
            if limit is None:
                preview.extend(block)
            elif len(preview) < limit:
                preview.extend(block[: limit - len(preview)])
            if output_sink is not None:
                await output_sink(
                    "stdout" if name == "stdout_chunk" else "stderr",
                    block,
                )

        data, _ = await self.call(
            "exec",
            params,
            timeout_sec=wait,
            event_callback=receive,
        )
        return (
            int(data["exit_code"]) if data.get("exit_code") is not None else None,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
            bool(data.get("timed_out")),
        )

    async def write_file(
        self, path: str, data: bytes, *, mode: str | None = None, run_as: str | None = None
    ) -> None:
        params: dict[str, Any] = {"path": path, "b64": base64.b64encode(data).decode("ascii")}
        if mode:
            params["mode"] = mode
        if run_as:
            params["run_as"] = run_as
        await self.call("write_file", params)

    async def read_file(self, path: str) -> bytes:
        _, events = await self.call("read_file", {"path": path})
        return b"".join(
            base64.b64decode(event["data"]["b64"])
            for event in events
            if event.get("event") == "chunk"
        )

    async def mkdirs(self, path: str) -> None:
        await self.call("mkdirs", {"path": path})

    async def exists(self, path: str) -> bool:
        data, _ = await self.call("stat", {"path": path})
        return bool(data.get("exists"))

    async def screenshot(self) -> bytes:
        data, _ = await self.call("screenshot", timeout_sec=60)
        return base64.b64decode(data["png_b64"])

    async def inject_input(self, actions: Sequence[dict[str, Any]]) -> int:
        data, _ = await self.call("input", {"actions": list(actions)}, timeout_sec=120)
        return int(data.get("applied", 0))

    async def close(self) -> None:
        await self._transport.close()


def _collect(events: list[dict[str, Any]], name: str) -> str:
    blocks = [
        base64.b64decode(event["data"]["b64"]) for event in events if event.get("event") == name
    ]
    return b"".join(blocks).decode("utf-8", "replace")
