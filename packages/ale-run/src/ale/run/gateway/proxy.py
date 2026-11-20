"""The egress proxy behind an ``allowlist`` or ``blocklist`` network policy.

A sandbox under either sits on the same routeless bridge as one in ``block`` mode. The
difference is entirely in what this process will forward on its behalf, which is what makes
both lists enforceable without privileged rules on the host or filtering tools inside an
image we do not control. Traffic that ignores the proxy has nowhere to go, so the failure
mode is closed.

A blocklist admits every host but the listed ones, so a name alone would let a client
through by another spelling of a blocked host: one of its addresses, or a name that resolves
into a blocked range. Under a blocklist the proxy therefore also resolves the name itself,
refuses it when any of its addresses is blocked, is not public or is the Host's own, connects
to an address it checked, and reads the server name the client's TLS handshake asks for
before it forwards that handshake.

This is a raw TCP server rather than another route on the model gateway because
``CONNECT`` is not a path request — its target is ``host:port``, and an HTTP router
never sees it. It shares the gateway's session registry but accepts only an episode's
egress session, whose hosts are those of the phase that runs, and which the gateway in turn
refuses for model calls.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import socket

from ale.core.taskspec import NetworkMode, NetworkPolicy
from ale.run.gateway.session import GatewaySession, SessionRegistry

__all__ = ["EgressProxy"]

_MAX_HEADER = 16 * 1024
_CONNECT_TIMEOUT = 15.0
#: The most a client may send before its ClientHello is whole; real ones are a few KiB.
_MAX_HELLO = 64 * 1024
#: NAT64's well-known prefix, which a Host on an IPv6-only network reaches IPv4 through, the
#: IPv4 address in its last 32 bits.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class EgressProxy:
    """Forwards to the hosts the policy of an episode's phase admits, and refuses the rest."""

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
            await _refuse(writer, 407, "unknown, suspended or missing egress token")
            return

        host, _, port_text = parts[1].rpartition(":")
        if not host:
            host, port_text = parts[1], "443"
        host = host.removeprefix("[").removesuffix("]")  # an IPv6 address

        policy = session.policy
        if not session.may_reach(host):
            await _refuse(writer, 403, _refusal(host, policy))
            return

        blocklist = policy.mode is NetworkMode.BLOCKLIST
        try:
            port = int(port_text)
            if blocklist:
                upstream_reader, upstream = await _connect_admitted(host, port, policy)
            else:
                upstream_reader, upstream = await asyncio.wait_for(
                    asyncio.open_connection(host, port), _CONNECT_TIMEOUT
                )
        except _Refused as refused:
            await _refuse(writer, 403, str(refused))
            return
        except (OSError, ValueError, TimeoutError) as exc:
            await _refuse(writer, 502, f"could not reach {host}: {exc}")
            return

        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()

        # Established: the point is to reach an admitted host, not to read what is said to
        # it, so from here the tunnel is opaque, past a blocklist's look at the server name.
        await _splice(
            reader, writer, upstream_reader, upstream, screen=policy if blocklist else None
        )

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
                return self.sessions.egress(token)
        return None


class _Refused(Exception):
    """A blocklist refuses the host a client asked for, by what it resolves to."""


def _refusal(host: str, policy: NetworkPolicy) -> str:
    """Why ``policy`` refuses ``host``. A blocklist's hosts are not named: a Task may block
    a site because it holds the answer, which naming it would tell the agent."""
    if policy.mode is NetworkMode.ALLOWLIST:
        declared = ", ".join(sorted(policy.allowed_hosts))
        return f"{host} is not in this task's allowlist (declared: {declared})"
    return f"{host} is blocked by this task's network policy"


async def _connect_admitted(
    host: str, port: int, policy: NetworkPolicy
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to ``host`` under a blocklist, at an address it resolves to.

    Every address must be public and admitted, or the host is refused: a name is the host
    its addresses are, and the tunnel leaves from the Host, so a private, loopback or
    link-local one (cloud metadata included) would reach the Host's own networks, which no
    Sandbox reaches through the proxy. The connection goes to the address checked, never to
    a second resolution, and one that turns out to have reached the Host's own address, its
    source and destination alike, is closed.
    """
    loop = asyncio.get_running_loop()
    resolved = await asyncio.wait_for(
        loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), _CONNECT_TIMEOUT
    )
    addresses = list(dict.fromkeys(str(info[4][0]) for info in resolved))
    for address in addresses:
        if reason := _unreachable(address, policy):
            raise _Refused(f"{host} ({address}) {reason}")
    failure: Exception = OSError(f"{host} resolved to no address")
    for address in addresses:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), _CONNECT_TIMEOUT
            )
        except (OSError, TimeoutError) as error:
            failure = error
            continue
        local, peer = writer.get_extra_info("sockname"), writer.get_extra_info("peername")
        if local[0] == peer[0]:
            writer.close()
            raise _Refused(f"{host} ({address}) is the Host itself")
        return reader, writer
    raise failure


def _unreachable(address: str, policy: NetworkPolicy) -> str | None:
    """Why a blocklist's tunnel may not go to ``address``, or ``None``."""
    try:
        ip = ipaddress.ip_address(address.partition("%")[0])
    except ValueError:
        return "is not an address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    elif ip in _NAT64:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if not ip.is_global:
        return "is not a public address"
    if not policy.admits(ip.compressed):
        return "is blocked by this task's network policy"
    return None


async def _opening(reader: asyncio.StreamReader) -> tuple[bytes, tuple[str, ...]]:
    """What a client sends first, and the server names it asks for when that opens a TLS
    handshake: its ClientHello, reassembled from however many records carry it.

    Raises ``ValueError`` for a handshake whose ClientHello cannot be read, which a blocklist
    refuses rather than forwards unjudged.
    """
    raw = await reader.read(65536)
    if raw[:1] != b"\x16":
        return raw, ()
    hello, offset = b"", 0
    while True:
        while len(raw) - offset >= 5:
            if raw[offset] != 0x16:
                raise ValueError("a TLS handshake broken off before its ClientHello")
            size = int.from_bytes(raw[offset + 3 : offset + 5], "big")
            if len(raw) - offset - 5 < size:
                break
            hello += raw[offset + 5 : offset + 5 + size]
            offset += 5 + size
            if len(hello) >= 4 and len(hello) >= (end := 4 + int.from_bytes(hello[1:4], "big")):
                return raw, _server_names(hello[:end])
        if len(raw) > _MAX_HELLO:
            raise ValueError("a ClientHello too long to read")
        more = await asyncio.wait_for(reader.read(65536), _CONNECT_TIMEOUT)
        if not more:
            raise ValueError("a TLS handshake that ended before its ClientHello")
        raw += more


class _Fields:
    """A cursor over TLS's length-prefixed fields that raises at the end of its data."""

    def __init__(self, data: bytes) -> None:
        self.data, self.at = data, 0

    @property
    def done(self) -> bool:
        return self.at >= len(self.data)

    def take(self, size: int) -> bytes:
        if self.at + size > len(self.data):
            raise ValueError("a truncated ClientHello")
        self.at += size
        return self.data[self.at - size : self.at]

    def vector(self, width: int) -> bytes:
        return self.take(int.from_bytes(self.take(width), "big"))


def _server_names(hello: bytes) -> tuple[str, ...]:
    """The host names a whole ClientHello handshake message asks for (RFC 6066), none
    without a ``server_name`` extension. Raises ``ValueError`` for one that cannot be read."""
    if hello[:1] != b"\x01":
        raise ValueError("a TLS handshake that does not open with a ClientHello")
    fields = _Fields(hello[4:])
    fields.take(2 + 32)  # version and random
    fields.vector(1)  # session id
    fields.vector(2)  # cipher suites
    fields.vector(1)  # compression methods
    if fields.done:
        return ()
    extensions = _Fields(fields.vector(2))
    names: list[str] = []
    while not extensions.done:
        kind, body = int.from_bytes(extensions.take(2), "big"), extensions.vector(2)
        if kind != 0:
            continue
        entries = _Fields(_Fields(body).vector(2))
        while not entries.done:
            name_type, name = entries.take(1)[0], entries.vector(2)
            if name_type == 0:
                names.append(name.decode("ascii"))
    return tuple(names)


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
    *,
    screen: NetworkPolicy | None = None,
) -> None:
    """Copy bytes both ways until either side hangs up.

    With ``screen``, the client's first bytes go upstream only once that policy admits every
    server name their TLS handshake asks for; a refusal closes both ends. The server's bytes
    flow meanwhile, so a protocol whose server speaks first is not held up.
    """

    async def pump(
        src: asyncio.StreamReader, dst: asyncio.StreamWriter, screen: NetworkPolicy | None
    ) -> None:
        try:
            if screen is not None:
                opening, names = await _opening(src)
                if not all(screen.admits(name) for name in names):
                    return
                dst.write(opening)
                await dst.drain()
            while chunk := await src.read(65536):
                dst.write(chunk)
                await dst.drain()
        finally:
            dst.close()

    await asyncio.gather(
        pump(client_reader, upstream_writer, screen),
        pump(upstream_reader, client_writer, None),
        return_exceptions=True,
    )
