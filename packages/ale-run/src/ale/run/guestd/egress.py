"""A Windows agent account's system proxy while an ``allowlist`` Sandbox is sealed.

A program that takes its proxy from the account's settings instead of the proxy variables,
a browser above all, cannot carry the Host proxy's per-episode credentials: a browser sends
credentials only in answer to a challenge, and then asks whoever sits at the screen. So
guestd listens on a loopback port, adds the episode's credentials to the request that
opens each tunnel, and forwards it to the Host proxy; the account's system proxy names that
port. The Host proxy still decides every connection.

Standard library only, and no engine imports: this file is copied into guests.
"""

from __future__ import annotations

import base64
import contextlib
import os
import socket
import threading
from urllib.parse import unquote, urlsplit

__all__ = ["Relay", "apply", "restore"]

_HEAD_LIMIT = 16 * 1024
#: As the Host proxy allows for a request head, and for reaching the other end.
_TIMEOUT = 15.0

_lock = threading.Lock()
_relay: Relay | None = None
#: The account's own settings, read before the first ``apply`` and written back by ``restore``.
_saved: tuple[int, str | None, str | None] | None = None


class Relay:
    """A loopback port that forwards to an authenticating proxy with its credentials."""

    def __init__(self, proxy: str) -> None:
        url = urlsplit(proxy)
        if not url.hostname or not url.port:
            raise ValueError("the proxy URL names no host and port")
        self.upstream = (url.hostname, url.port)
        credentials = f"{unquote(url.username or '')}:{unquote(url.password or '')}"
        self.authorization = b"Proxy-Authorization: Basic " + base64.b64encode(credentials.encode())
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.settimeout(0.5)
        self.address = f"127.0.0.1:{self._server.getsockname()[1]}"
        self._closed = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        """Stop accepting; tunnels already open run until either end closes them."""
        self._closed.set()
        self._server.close()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._server.accept()
            except OSError:
                # A client that went away before it was accepted stops nothing.
                if self._closed.is_set():
                    return
                continue
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        with client, contextlib.suppress(OSError):
            client.settimeout(_TIMEOUT)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk or len(head) + len(chunk) > _HEAD_LIMIT:
                    return
                head += chunk
            head, _, rest = head.partition(b"\r\n\r\n")
            request, *fields = head.split(b"\r\n")
            fields = [f for f in fields if not f.lower().startswith(b"proxy-authorization:")]
            try:
                upstream = socket.create_connection(self.upstream, timeout=_TIMEOUT)
            except OSError:
                client.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            with upstream:
                client.settimeout(None)
                upstream.settimeout(None)
                upstream.sendall(
                    b"\r\n".join([request, self.authorization, *fields]) + b"\r\n\r\n" + rest
                )
                back = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
                back.start()
                _pump(client, upstream)
                back.join()


def _pump(source: socket.socket, sink: socket.socket) -> None:
    try:
        while chunk := source.recv(65536):
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            sink.shutdown(socket.SHUT_WR)


def apply(proxy: str, bypass: list[str]) -> str:
    """Relay to ``proxy``, a URL carrying its credentials, and make the relay the account's
    system proxy, bypassed for ``bypass``; return the relay's address.

    A later call replaces the relay, as a run that continues in a retained Sandbox does with
    its own proxy, and keeps the settings the first call saved.
    """
    global _relay, _saved
    with _lock:
        relay = Relay(proxy)
        if _relay is not None:
            _relay.close()
        _relay = relay
        if _saved is None:
            _saved = _read_settings()
        _write_settings(_PROXY_TYPE_DIRECT | _PROXY_TYPE_PROXY, relay.address, ";".join(bypass))
        return relay.address


def restore() -> None:
    """Stop the relay and give the account its own settings back; nothing to do if unset."""
    global _relay, _saved
    with _lock:
        if _relay is not None:
            _relay.close()
            _relay = None
        if _saved is not None:
            _write_settings(*_saved)
            _saved = None


# --- Windows: the default connection's WinINet settings, which the account's WinINet
# clients and Chrome-family browsers read. --------------------------------------------

_PROXY_TYPE_DIRECT = 0x1
_PROXY_TYPE_PROXY = 0x2
_PER_CONN_FLAGS, _PER_CONN_PROXY_SERVER, _PER_CONN_PROXY_BYPASS = 1, 2, 3
#: The flags as the account set them. The plain flags lose automatic detection once it has
#: failed, as it does offline, and written back they would turn it off for good.
_PER_CONN_FLAGS_UI = 10
_OPTION_REFRESH = 37
_OPTION_SETTINGS_CHANGED = 39
_OPTION_PER_CONNECTION_OPTION = 75

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _Value(ctypes.Union):
        _fields_ = (
            ("number", wintypes.DWORD),
            ("text", ctypes.c_void_p),
            ("time", wintypes.FILETIME),
        )

    class _Option(ctypes.Structure):
        _fields_ = [("option", wintypes.DWORD), ("value", _Value)]

    class _Options(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("connection", wintypes.LPWSTR),
            ("count", wintypes.DWORD),
            ("error", wintypes.DWORD),
            ("options", ctypes.POINTER(_Option)),
        ]

    _wininet = ctypes.WinDLL("wininet", use_last_error=True)
    _wininet.InternetQueryOptionW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _wininet.InternetSetOptionW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.GlobalFree.argtypes = [ctypes.c_void_p]
    _kernel.GlobalFree.restype = ctypes.c_void_p

    def _check(succeeded: int) -> None:
        if not succeeded:
            raise ctypes.WinError(ctypes.get_last_error())

    def _options(flags: int) -> tuple[ctypes.Array[_Option], _Options]:
        options = (_Option * 3)()
        names = (flags, _PER_CONN_PROXY_SERVER, _PER_CONN_PROXY_BYPASS)
        for option, name in zip(options, names, strict=True):
            option.option = name
        return options, _Options(ctypes.sizeof(_Options), None, 3, 0, options)

    def _read_settings() -> tuple[int, str | None, str | None]:
        options, request = _options(_PER_CONN_FLAGS_UI)
        size = wintypes.DWORD(ctypes.sizeof(request))
        _check(
            _wininet.InternetQueryOptionW(
                None, _OPTION_PER_CONNECTION_OPTION, ctypes.byref(request), ctypes.byref(size)
            )
        )
        texts: list[str | None] = []
        for option in options[1:]:
            address = option.value.text
            texts.append(ctypes.wstring_at(address) if address else None)
            if address:
                _kernel.GlobalFree(address)
        return options[0].value.number, texts[0], texts[1]

    def _write_settings(flags: int, server: str | None, bypass: str | None) -> None:
        options, request = _options(_PER_CONN_FLAGS)
        options[0].value.number = flags
        buffers = [
            None if text is None else ctypes.create_unicode_buffer(text)
            for text in (server, bypass)
        ]
        for option, buffer in zip(options[1:], buffers, strict=True):
            option.value.text = None if buffer is None else ctypes.addressof(buffer)
        _check(
            _wininet.InternetSetOptionW(
                None, _OPTION_PER_CONNECTION_OPTION, ctypes.byref(request), ctypes.sizeof(request)
            )
        )
        # Programs already running read the change: WinINet's on these notices, Chrome's
        # because it watches the settings in the registry.
        _check(_wininet.InternetSetOptionW(None, _OPTION_SETTINGS_CHANGED, None, 0))
        _check(_wininet.InternetSetOptionW(None, _OPTION_REFRESH, None, 0))

else:

    def _read_settings() -> tuple[int, str | None, str | None]:
        raise OSError("guestd sets a system proxy on Windows only")

    def _write_settings(flags: int, server: str | None, bypass: str | None) -> None:
        raise OSError("guestd sets a system proxy on Windows only")
