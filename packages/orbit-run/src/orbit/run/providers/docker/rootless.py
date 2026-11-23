"""The Host's ports at the bridges of a rootless Docker daemon.

A Sandbox reaches its Gateway and egress proxy at the Host's address on its network, the
bridge's. A rootless daemon's bridges are in a network namespace of rootlesskit's: one it
detaches for them (``--detach-netns``, ``dockerd-rootless.sh``'s default) or the daemon's
own, kept from the Host's loopback (``--disable-host-loopback``), where nothing the Host
serves listens. So for each Host port its Sandboxes reach, the Provider listens at that port
there, on every address, and this process relays each connection to the port on the Host's
loopback.

A fresh Python joins the daemon's user and network namespaces, which only a process of one
thread may do, makes the listener, and hands it back over a Unix socket. The listener is
then this process's own, so it goes when the last Sandbox that reaches the port ends, or
with this process, and leaves nothing in the daemon's namespace.
"""

from __future__ import annotations

import asyncio
import functools
import os
import socket
import struct
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from orbit.core.errors import ProviderStartError

__all__ = ["HostPorts", "Namespaces", "namespaces"]

#: The variable rootlesskit gives the daemon, naming the directory of its state.
_STATE_DIR = b"ROOTLESSKIT_STATE_DIR="

#: Run by a fresh Python: joins the user and then the network namespace that ``argv`` names,
#: listens there on every address at the port, and sends the listener back over the Unix
#: socket whose descriptor is ``argv[1]``.
_LISTEN = """
import os, socket, sys
back, user, net, port = sys.argv[1:]
for path, kind in ((user, os.CLONE_NEWUSER), (net, os.CLONE_NEWNET)):
    fd = os.open(path, os.O_RDONLY)
    os.setns(fd, kind)
    os.close(fd)
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("0.0.0.0", int(port)))
listener.listen(socket.SOMAXCONN)
socket.send_fds(socket.socket(fileno=int(back)), [b"."], [listener.fileno()])
"""


@dataclass(frozen=True)
class Namespaces:
    """The user and network namespaces a rootless daemon's bridges are in, by path."""

    user: str
    net: str


def namespaces(endpoint: str) -> Namespaces | None:
    """Where the bridges of the rootless daemon at ``endpoint``, a ``unix://`` URL, are; None
    when that is this process's network namespace, as when it runs inside it.

    The daemon is the process listening there (``SO_PEERCRED``). Its bridges are in
    ``$ROOTLESSKIT_STATE_DIR/netns``, seen from its mount namespace, when rootlesskit detached
    them, and in its own network namespace otherwise, as ``dockerd-rootless.sh`` finds them.
    """
    try:
        with socket.socket(socket.AF_UNIX) as daemon:
            daemon.connect(urlsplit(endpoint).path)
            credentials = daemon.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
        pid = struct.unpack("3i", credentials)[0]
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        state = next(
            (entry[len(_STATE_DIR) :] for entry in environ if entry.startswith(_STATE_DIR)), b""
        ).decode()
        detached = f"/proc/{pid}/root{state}/netns"
        net = detached if state and os.path.exists(detached) else f"/proc/{pid}/ns/net"
        bridges, ours = os.stat(net), os.stat("/proc/self/ns/net")
    except OSError as error:
        raise ProviderStartError(
            f"could not find the network namespace of the rootless Docker daemon at "
            f"{endpoint}: {error}"
        ) from None
    if (bridges.st_dev, bridges.st_ino) == (ours.st_dev, ours.st_ino):
        return None
    return Namespaces(user=f"/proc/{pid}/ns/user", net=net)


class HostPorts:
    """The Host's ports a Provider's Sandboxes hold, each relayed from the daemon's bridges
    from its first hold until its last holder lets go.

    ``find`` says where the bridges are each time a port needs a listener, since a daemon
    that restarted is another process. Once they are found in this process's network
    namespace, nothing is relayed.
    """

    def __init__(self, find: Callable[[], Awaitable[Namespaces | None]]) -> None:
        self._find = find
        self._here = False
        self._servers: dict[int, asyncio.Server] = {}
        self._holders: dict[int, set[str]] = {}
        self._lock = asyncio.Lock()

    async def hold(self, holder: str, ports: Iterable[int]) -> None:
        """Relay each of ``ports`` for ``holder``; on failure, the caller releases it."""
        async with self._lock:
            for port in ports:
                if self._here:
                    return
                if port not in self._servers:
                    namespaces = await self._find()
                    if namespaces is None:
                        self._here = True
                        return
                    listener = await _listen(namespaces, port)
                    self._servers[port] = await asyncio.get_running_loop().create_server(
                        functools.partial(_Accepted, port), sock=listener
                    )
                self._holders.setdefault(port, set()).add(holder)

    def release(self, holder: str) -> None:
        """Let go of every port ``holder`` holds; one nobody holds stops listening."""
        for port, holders in list(self._holders.items()):
            holders.discard(holder)
            if not holders:
                del self._holders[port]
                self._servers.pop(port).close()


async def _listen(namespaces: Namespaces, port: int) -> socket.socket:
    """A listener at ``port`` in the daemon's network namespace, made there by a fresh
    Python and handed back to this process."""
    ours, theirs = socket.socketpair()
    with ours:
        with theirs:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", _LISTEN,
                str(theirs.fileno()), namespaces.user, namespaces.net, str(port),
                pass_fds=(theirs.fileno(),),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )  # fmt: skip
        _, stderr = await process.communicate()
        if process.returncode != 0:
            reason = stderr.decode(errors="replace").strip().splitlines()[-1:]
            raise ProviderStartError(
                f"could not listen at port {port} in the rootless Docker daemon's network "
                f"namespace: {''.join(reason) or f'exit status {process.returncode}'}"
            )
        ours.setblocking(False)  # the listener was sent before the process exited
        _, fds, _, _ = socket.recv_fds(ours, 1, 1)
    return socket.socket(fileno=fds[0])


class _End(asyncio.Protocol):
    """One end of a relayed connection, which sends its peer what it receives.

    The upstream end is made with its peer and links both as it connects, before it reads.
    While one end cannot write more, its peer reads nothing; an end that ends passes it on,
    and both close once both have ended or either is lost.
    """

    def __init__(self, peer: _End | None = None) -> None:
        self.peer = peer
        self.transport: asyncio.Transport
        self.ended = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        self.transport = transport
        if self.peer is not None:
            self.peer.peer = self

    def data_received(self, data: bytes) -> None:
        assert self.peer is not None
        self.peer.transport.write(data)

    def eof_received(self) -> bool:
        assert self.peer is not None
        self.ended = True
        if self.peer.ended:
            self.transport.close()
            self.peer.transport.close()
        else:
            self.peer.transport.write_eof()
        return True

    def connection_lost(self, exc: Exception | None) -> None:
        if self.peer is not None:
            self.peer.transport.close()

    def pause_writing(self) -> None:
        assert self.peer is not None
        self.peer.transport.pause_reading()

    def resume_writing(self) -> None:
        assert self.peer is not None
        self.peer.transport.resume_reading()


class _Accepted(_End):
    """The end a Sandbox connected, which reads nothing until its peer has connected to the
    port on the Host's loopback, and is reset when that connection is refused."""

    def __init__(self, port: int) -> None:
        super().__init__()
        self._port = port
        self._connecting: asyncio.Task[None] | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        self.transport.pause_reading()
        self._connecting = asyncio.get_running_loop().create_task(self._connect())

    async def _connect(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            upstream, _ = await loop.create_connection(
                functools.partial(_End, self), "127.0.0.1", self._port
            )
        except OSError:
            # Reset, as a connection the port refused would be, rather than ended.
            linger = struct.pack("ii", 1, 0)
            self.transport.get_extra_info("socket").setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, linger
            )
            self.transport.abort()
            return
        if self.transport.is_closing():
            upstream.close()
        else:
            self.transport.resume_reading()
