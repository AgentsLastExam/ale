"""What QEMU and Firecracker share: each boots its guest in a runner container.

The runner publishes guestd's port on the Host's loopback, behind a token the Provider
issues and keeps beside the guest's disk for whoever reattaches. The guest reaches the
Host at ``HOST_IP``, the runner's address on the guest's network, where the runner
forwards the Gateway and the proxy. The runner's log is the guest's serial console, where
a Linux guest's kernel writes each process its OOM killer ends.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
import zlib
from pathlib import Path
from urllib.parse import urlsplit

from orbit.core.errors import (
    GuestUnreachableError,
    MemoryExhaustedError,
    ProviderCapabilityError,
    ProviderStartError,
)
from orbit.run import guestd
from orbit.run.process import run
from orbit.run.transport import GuestClient, TcpTransport

__all__ = [
    "GUEST_TOKEN_FILE",
    "HOST_IP",
    "Console",
    "connect_guest",
    "free_port",
    "is_blank",
    "issue_guest_token",
    "read_guest_token",
    "url_port",
]

#: The Host's address on the guest's network, which the runner owns.
HOST_IP = guestd.HOST_ADDRESS
#: The sandbox's guest-service token, kept beside its disk for reattachment.
GUEST_TOKEN_FILE = "guestd-token"


def issue_guest_token(storage: Path) -> str:
    """A fresh token the booted guest's first connection claims; readable only by this account."""
    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(storage / GUEST_TOKEN_FILE, flags, 0o600), "w") as handle:
        handle.write(token)
    return token


def read_guest_token(storage: Path) -> str:
    try:
        return (storage / GUEST_TOKEN_FILE).read_text().strip()
    except FileNotFoundError as exc:
        raise ProviderCapabilityError(
            f"retained sandbox {storage} has no guest service token"
        ) from exc


#: What a Linux kernel writes to its console when its OOM killer ends a process, for the
#: machine's memory, which in a VM is the Task's, or for a cgroup's limit in it, and when it
#: has none left to end.
_OOM_KILL = re.compile(
    r"(?:Memory cgroup o|O)ut of memory[^:]*: Killed process \d+ \([^)]*\)"
    r"|System is deadlocked on memory"
)


class Console:
    """A runner's log, the guest's serial console since it booted."""

    def __init__(self, container: str, memory_mb: int) -> None:
        self.container = container
        #: The memory its Task declares.
        self.memory_mb = memory_mb

    async def oom_killed(self) -> str | None:
        """What the guest's kernel said of the OOM kills it made, or None."""
        code, stdout, stderr = await run("docker", "logs", self.container, timeout=60)
        if code != 0:
            return None
        found = [
            match.group(0)
            for line in (stdout + stderr).splitlines()
            if (match := _OOM_KILL.search(line))
        ]
        if not found:
            return None
        return (
            f"its console shows {len(found)} messages of its kernel's OOM killer, the last "
            f"{found[-1]!r}"
        )


class _RunnerTransport(TcpTransport):
    """guestd through the runner's port, whose loss the guest's console may explain."""

    def __init__(self, port: int, token: str, console: Console | None) -> None:
        super().__init__("127.0.0.1", port, token=token)
        self._console = console
        self._answered = False
        self._closing = False
        #: What the end of its connection was found to mean, which a later call raises too.
        self._explained: GuestUnreachableError | None = None

    async def recv(self) -> str:
        line = await super().recv()
        self._answered = True
        return line

    async def close(self) -> None:
        # Orbit closes a connection before it destroys, keeps or snapshots the Sandbox; one
        # that never answered is still connecting.
        self._closing = self._answered
        await super().close()

    async def explain(self, error: GuestUnreachableError) -> GuestUnreachableError:
        if self._console is None or self._closing or not self._answered:
            return error
        if self._explained is None:
            self._explained = error
            try:
                evidence = await self._console.oom_killed()
            except ProviderStartError:
                evidence = None  # the runner's log did not answer either
            if evidence is not None:
                self._explained = MemoryExhaustedError(
                    f"guestd in the VM was lost after an OOM kill in it (its Task declares "
                    f"{self._console.memory_mb} MiB): {evidence} ({error})"
                )
                self._explained.__cause__ = error
        return self._explained


async def connect_guest(
    port: int,
    token: str,
    *,
    timeout_sec: float,
    interval_sec: float = 1.0,
    console: Console | None = None,
) -> GuestClient:
    """guestd at the runner's published ``port``, once the guest has booted and answers; a
    lost connection is explained by ``console``, if any."""
    transport = _RunnerTransport(port, token, console)
    client = GuestClient(transport)
    try:
        await transport.start(timeout_sec=timeout_sec, interval_sec=interval_sec)
    except BaseException:
        await client.close()
        raise
    return client


def free_port() -> int:
    """Ask the OS for a port, then hand it to the runner.

    There is a race between closing this socket and the runner binding it, which is why
    the port is ephemeral per episode rather than fixed: a collision costs one retry of
    one episode instead of making concurrent runs impossible.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def is_blank(png: bytes) -> bool:
    """Whether an image is a single flat colour: a desktop that is up but has drawn nothing.

    Decoded here rather than in the guest, and without an imaging library. Asking the
    guest meant running Python in a process that has no display — the guest service finds
    one when *it* needs one, which does nothing for a command run beside it — so the check
    reported "nothing drawn" forever against a desktop that had painted twenty seconds in.
    The bytes are already on this side; nothing else was needed.

    A flat image compresses to almost nothing: every scanline is one filter byte followed
    by the same pixel repeated, so the decompressed stream holds only a couple of distinct
    values. Anything real holds hundreds.
    """
    data = bytearray()
    offset = 8  # past the signature
    while offset + 8 <= len(png):
        length = int.from_bytes(png[offset : offset + 4], "big")
        kind = png[offset + 4 : offset + 8]
        if kind == b"IDAT":
            data += png[offset + 8 : offset + 8 + length]
        elif kind == b"IEND":
            break
        offset += 12 + length
    if not data:
        return False  # unreadable is not "blank"; let the caller keep what it was given
    try:
        return len(set(zlib.decompress(bytes(data)))) <= 2
    except zlib.error:
        return False


def url_port(url: str | None) -> str:
    """The port a URL names, or empty when a run has no such service."""
    port = urlsplit(url).port if url else None
    return str(port) if port else ""
