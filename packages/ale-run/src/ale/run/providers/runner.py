"""What QEMU and Firecracker share: each boots its guest in a runner container.

The runner publishes guestd's port on the Host's loopback, behind a token the Provider
issues and keeps beside the guest's disk for whoever reattaches. The guest reaches the
Host at ``HOST_IP``, the runner's address on the guest's network, where the runner
forwards the Gateway and the proxy.
"""

from __future__ import annotations

import os
import secrets
import socket
import zlib
from pathlib import Path
from urllib.parse import quote, urlsplit

from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import SandboxRequest
from ale.run import guestd
from ale.run.transport import GuestClient, TcpTransport

__all__ = [
    "GUEST_TOKEN_FILE",
    "HOST_IP",
    "connect_guest",
    "free_port",
    "is_blank",
    "issue_guest_token",
    "proxy_env",
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


async def connect_guest(
    port: int, token: str, *, timeout_sec: float, interval_sec: float = 1.0
) -> GuestClient:
    """guestd at the runner's published ``port``, once the guest has booted and answers."""
    transport = TcpTransport("127.0.0.1", port, token=token)
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


def proxy_env(request: SandboxRequest) -> dict[str, str]:
    """The variables that send an agent's commands through the proxy the runner forwards."""
    if not request.proxy_url:
        return {}
    proxy = f"http://{HOST_IP}:{url_port(request.proxy_url)}"
    if request.proxy_token:
        proxy = proxy.replace("://", f"://{quote(request.proxy_token, safe='')}:@", 1)
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": f"{HOST_IP},localhost,127.0.0.1",
    }
