"""The egress proxy behind ``network.mode: allowlist``.

A sandbox in allowlist mode sits on the same routeless bridge as one in ``block`` mode.
The difference is entirely in what this process will forward on its behalf, which is
what makes the allowlist enforceable without privileged rules on the host or filtering
tools inside an image we do not control. Traffic that ignores the proxy has nowhere to
go, so the failure mode is closed.

This is a raw TCP server rather than another route on the model gateway because
``CONNECT`` is not a path request — its target is ``host:port``, and an HTTP router
never sees it. Sharing the gateway's session registry keeps the two halves consistent:
one token, one episode, one allowlist.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib

from ale.run.gateway.session import GatewaySession, SessionRegistry

__all__ = ["EgressProxy"]

_MAX_HEADER = 16 * 1024
_CONNECT_TIMEOUT = 15.0


class EgressProxy:
    """Forwards to hosts an episode declared, and refuses everything else."""

    def __init__(self, sessions: SessionRegistry, *, host: str = "0.0.0.0", port: int = 0) -> None:
        self.sessions = sessions
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _CONNECT_TIMEOUT)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return

        if len(head) > _MAX_HEADER:
            await _refuse(writer, 431, "request header too large")
            return

        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            # Plain HTTP through a proxy would mean forwarding an absolute-form request,
            # which is a second code path and a second thing to get wrong. Tasks that
            # need egress use TLS, and TLS uses CONNECT.
            await _refuse(writer, 405, "this proxy accepts CONNECT only")
            return

        session = self._authenticate(lines)
        if session is None:
            await _refuse(writer, 407, "unknown or missing episode token")
            return

        host, _, port_text = parts[1].rpartition(":")
        if not host:
            host, port_text = parts[1], "443"

        if not session.may_reach(host):
            declared = ", ".join(sorted(session.allowed_hosts)) or "none"
            await _refuse(
                writer, 403, f"{host} is not in this task's allowlist (declared: {declared})"
            )
            return

        try:
            port = int(port_text)
            upstream_reader, upstream = await asyncio.wait_for(
                asyncio.open_connection(host, port), _CONNECT_TIMEOUT
            )
        except (OSError, ValueError, TimeoutError) as exc:
            await _refuse(writer, 502, f"could not reach {host}: {exc}")
            return

        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()

        # Established: the point is to reach a declared host, not to read what is said
        # to it, so from here the tunnel is opaque.
        await _splice(reader, writer, upstream_reader, upstream)

    def _authenticate(self, lines: list[str]) -> GatewaySession | None:
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() in {"proxy-authorization", "authorization"}:
                authorization = value.strip()
                if authorization.lower().startswith("basic "):
                    try:
                        credentials = base64.b64decode(
                            authorization.split(None, 1)[1], validate=True
                        ).decode()
                    except (ValueError, UnicodeDecodeError):
                        return None
                    token = credentials.partition(":")[0]
                else:
                    token = authorization.removeprefix("Bearer ").strip()
                return self.sessions.resolve(token)
        return None


async def _refuse(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    """Say no in a form a proxy client will surface to whoever ran the task."""
    body = message.encode()
    writer.write(
        f"HTTP/1.1 {status} Forbidden\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: text/plain\r\n"
        "Connection: close\r\n\r\n".encode()
        + body
    )
    with contextlib.suppress(ConnectionError):  # the client may already be gone
        await writer.drain()
    writer.close()


async def _splice(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    """Copy bytes both ways until either side hangs up."""

    async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while chunk := await src.read(65536):
                dst.write(chunk)
                await dst.drain()
        finally:
            dst.close()

    await asyncio.gather(
        pump(client_reader, upstream_writer),
        pump(upstream_reader, client_writer),
        return_exceptions=True,
    )
