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
import fcntl
import json
import os
import stat
from collections.abc import Awaitable, Callable, Iterator, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Protocol

from ale.core.errors import GuestUnreachableError, OutputStreamError, ProviderStartError
from ale.core.sandbox import ExecOutputSink

__all__ = ["GuestClient", "StdioTransport", "TcpTransport", "Transport"]

_READ_LIMIT = 16 * 1024 * 1024  # generous: file chunks arrive base64-encoded
#: Bytes per write_file message; base64 of this stays well under the guest's 8 MiB line.
_WRITE_CHUNK_BYTES = 4 * 1024 * 1024
_OUTPUT_PREVIEW_BYTES = 64 * 1024
#: Body bytes gathered before one transport write: large enough that a tree of small files
#: costs a handful of writes, small enough to keep memory flat for a large one.
_BODY_BUFFER_BYTES = 1024 * 1024
#: Bytes read from one large file per write; such blocks are sent as they are, uncopied.
_FILE_BLOCK_BYTES = 4 * 1024 * 1024
#: The session pipe's capacity. Linux defaults to 64 KiB, which turns each megabyte of an
#: upload into sixteen event-loop wakeups; 1 MiB is the unprivileged maximum.
_PIPE_BYTES = 1024 * 1024
#: The first guest protocol with ``put_tree``.
_STREAMED_UPLOADS = 2


class Transport(Protocol):
    """A duplex channel to one guest service: bytes out, reply lines back."""

    async def send(self, data: bytes) -> None: ...

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
        assert self._proc.stdin is not None
        pipe = self._proc.stdin.get_extra_info("pipe")
        with contextlib.suppress(AttributeError, OSError):
            fcntl.fcntl(pipe.fileno(), fcntl.F_SETPIPE_SZ, _PIPE_BYTES)

    async def send(self, data: bytes) -> None:
        proc = self._require()
        assert proc.stdin is not None
        proc.stdin.write(data)
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
    """An authenticated socket to a guest service reached through a forwarded port.

    The forwarded port is reachable from inside the guest (loopback) and from the Host,
    so every connection first presents the sandbox's framework token. The first token a
    freshly booted guest receives claims it; see ``docs/specs/security.md``.
    """

    def __init__(self, host: str, port: int, *, token: str) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def start(self, *, timeout_sec: float = 120, interval_sec: float = 1.0) -> None:
        """Connect and exchange one message, retrying until the deadline passes.

        Connecting is not evidence that anyone is listening. When the port is published by
        a container runtime, its forwarder binds immediately and accepts long before the
        guest inside has booted — the connection succeeds and the first read is reset.
        Readiness is therefore a request that came back, not a socket that opened; the
        request is the authentication every connection makes first.
        """
        deadline = asyncio.get_running_loop().time() + timeout_sec
        last: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    self._host, self._port, limit=_READ_LIMIT
                )
                await self.send(
                    json.dumps({"id": 0, "op": "auth", "params": {"token": self._token}}).encode()
                    + b"\n"
                )
                reply = json.loads(await asyncio.wait_for(self.recv(), timeout=interval_sec * 10))
            except (OSError, GuestUnreachableError, TimeoutError, ValueError) as exc:
                last = exc
                await self.close()
                await asyncio.sleep(interval_sec)
                continue
            if reply.get("ok"):
                return
            await self.close()
            code = (reply.get("error") or {}).get("code")
            if code == "unsupported":
                raise ProviderStartError(
                    f"guest service at {self._host}:{self._port} predates authenticated "
                    "connections; rebuild the image from the current ALE base"
                )
            raise ProviderStartError(
                f"guest service at {self._host}:{self._port} refused the framework token [{code}]"
            )
        raise ProviderStartError(
            f"guest service at {self._host}:{self._port} did not answer within "
            f"{timeout_sec:g}s ({last})"
        )

    async def send(self, data: bytes) -> None:
        if self._writer is None:
            raise GuestUnreachableError("transport is not started")
        self._writer.write(data)
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
        self._health: dict[str, Any] | None = None

    async def call(
        self,
        op: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_sec: float | None = None,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        body: Callable[[int], Iterator[bytes]] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Perform one operation; returns its data and any interim events.

        ``body`` produces the request body for its id. It runs in a worker thread, so
        reading a large tree from disk never stalls the event loop the Gateway shares.
        """
        async with self._lock:
            self._next_id += 1
            req_id = self._next_id
            await self._transport.send(_line({"id": req_id, "op": op, "params": params or {}}))
            if body is not None:
                blocks = body(req_id)
                while (block := await asyncio.to_thread(next, blocks, None)) is not None:
                    await self._transport.send(block)
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
        self._health = data
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
        """Write ``data`` to ``path``, appending pieces that fit the guest's message limit."""
        for offset in range(0, max(len(data), 1), _WRITE_CHUNK_BYTES):
            piece = data[offset : offset + _WRITE_CHUNK_BYTES]
            params: dict[str, Any] = {"path": path, "b64": base64.b64encode(piece).decode("ascii")}
            if offset:
                params["append"] = True
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

    async def upload_dir(self, source: str, target: str, *, run_as: str | None = None) -> None:
        """Stream the tree at ``source`` into ``target`` as one ``put_tree`` request.

        The guest applies the upload modes and owner as it writes. A Windows image build
        boots a base disk with the guest service it was saved with, which may predate
        ``put_tree``; only there are files sent one by one.
        """
        health = self._health or await self.health()
        if int(health.get("proto") or 1) < _STREAMED_UPLOADS:
            if health.get("os") != "windows":
                raise ProviderStartError(
                    "the guest service predates streamed uploads; rebuild the image"
                )
            await self._upload_files(Path(source), PureWindowsPath(target), run_as=run_as)
            return
        root = Path(source)
        if not root.is_dir():
            raise ProviderStartError(f"upload source is not a directory: {root}")
        params: dict[str, Any] = {"path": target}
        if run_as:
            params["run_as"] = run_as
        body = _TreeBody(root)
        try:
            await self.call("put_tree", params, body=body)
        except GuestUnreachableError:
            if body.error is not None:
                raise body.error from None
            raise

    async def _upload_files(
        self, root: Path, target: PureWindowsPath | PurePosixPath, *, run_as: str | None
    ) -> None:
        await self.mkdirs(str(target), run_as=run_as)
        for entry in sorted(root.rglob("*")):
            destination = target.joinpath(*entry.relative_to(root).parts)
            if entry.is_dir():
                await self.mkdirs(str(destination), run_as=run_as)
            elif entry.is_file():
                await self.mkdirs(str(destination.parent), run_as=run_as)
                await self.write_file(str(destination), entry.read_bytes(), run_as=run_as)

    async def mkdirs(self, path: str, *, run_as: str | None = None) -> None:
        params = {"path": path}
        if run_as:
            params["run_as"] = run_as
        await self.call("mkdirs", params)

    async def exists(self, path: str) -> bool:
        data, _ = await self.call("stat", {"path": path})
        return bool(data.get("exists"))

    async def list_files(self, path: str) -> tuple[str, ...]:
        data, _ = await self.call("list_files", {"path": path})
        return tuple(str(item) for item in data.get("files", ()))

    async def disk_usage(self, path: str) -> tuple[int, int, int]:
        data, _ = await self.call("disk_usage", {"path": path})
        return int(data["total"]), int(data["used"]), int(data["free"])

    async def screenshot(self) -> bytes:
        data, _ = await self.call("screenshot", timeout_sec=60)
        return base64.b64decode(data["png_b64"])

    async def inject_input(self, actions: Sequence[dict[str, Any]]) -> int:
        data, _ = await self.call("input", {"actions": list(actions)}, timeout_sec=120)
        return int(data.get("applied", 0))

    async def close(self) -> None:
        await self._transport.close()


def _line(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


class _TreeBody:
    """The ``put_tree`` body for a Host directory, in sorted depth-first order.

    Links are sent as links, never followed. A local failure (an unreadable file, a FIFO,
    a file that shrinks while read) never leaves the guest mid-frame: the current file is
    padded to its announced size, the body is aborted, and the failure is kept in
    ``error`` for the caller to raise once the guest has answered.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.error: ProviderStartError | None = None

    def __call__(self, req_id: int) -> Iterator[bytes]:
        buffer = bytearray()
        try:
            for name, entry in self._entries(""):
                if not entry.is_file(follow_symlinks=False):
                    buffer += self._header(req_id, name, entry)
                else:
                    with open(entry.path, "rb") as handle:
                        remaining = os.fstat(handle.fileno()).st_size
                        buffer += _line(
                            {"id": req_id, "entry": name, "kind": "file", "size": remaining}
                        )
                        while remaining:
                            try:
                                block = handle.read(min(remaining, _FILE_BLOCK_BYTES))
                                if not block:
                                    raise ProviderStartError(f"{name} changed while it was read")
                            except (OSError, ProviderStartError):
                                yield bytes(buffer)
                                buffer.clear()
                                while remaining:
                                    yield bytes(min(remaining, _FILE_BLOCK_BYTES))
                                    remaining -= min(remaining, _FILE_BLOCK_BYTES)
                                raise
                            remaining -= len(block)
                            if len(block) >= _BODY_BUFFER_BYTES:
                                if buffer:
                                    yield bytes(buffer)
                                    buffer.clear()
                                yield block
                                continue
                            buffer += block
                            if len(buffer) >= _BODY_BUFFER_BYTES:
                                yield bytes(buffer)
                                buffer.clear()
                if len(buffer) >= _BODY_BUFFER_BYTES:
                    yield bytes(buffer)
                    buffer.clear()
        except (OSError, ProviderStartError) as exc:
            self.error = (
                exc
                if isinstance(exc, ProviderStartError)
                else ProviderStartError(f"cannot upload from {self.root}: {exc}")
            )
            buffer += _line({"id": req_id, "abort": str(self.error)})
        else:
            buffer += _line({"id": req_id, "end": True})
        yield bytes(buffer)

    def _entries(self, prefix: str) -> Iterator[tuple[str, os.DirEntry[str]]]:
        with os.scandir(self.root / prefix if prefix else self.root) as listing:
            children = sorted(listing, key=lambda child: child.name)
        for child in children:
            name = f"{prefix}{child.name}"
            yield name, child
            if child.is_dir(follow_symlinks=False):
                yield from self._entries(name + "/")

    @staticmethod
    def _header(req_id: int, name: str, entry: os.DirEntry[str]) -> bytes:
        if entry.is_symlink():
            return _line(
                {"id": req_id, "entry": name, "kind": "symlink", "target": os.readlink(entry)}
            )
        if entry.is_dir(follow_symlinks=False):
            return _line({"id": req_id, "entry": name, "kind": "dir"})
        kind = stat.filemode(entry.stat(follow_symlinks=False).st_mode)[0]
        raise ProviderStartError(f"cannot upload {name}: unsupported file type {kind!r}")


def _collect(events: list[dict[str, Any]], name: str) -> str:
    blocks = [
        base64.b64decode(event["data"]["b64"]) for event in events if event.get("event") == name
    ]
    return b"".join(blocks).decode("utf-8", "replace")
