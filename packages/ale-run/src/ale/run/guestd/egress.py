"""The relay a sealed ``allowlist`` Sandbox reaches the Host's egress proxy through.

The Host proxy knows an episode by its token, and many programs cannot present one: a
browser sends proxy credentials only in answer to a challenge, and then asks whoever sits at
the screen; Chromium ignores those in the proxy variables. So guestd listens on a loopback
port, adds the episode's credentials to the request that opens each tunnel, and forwards it
to the Host proxy. The agent's proxy variables name that port, and on a desktop so does the
agent account's system proxy, which programs started from the desktop take theirs from. The
Host proxy still decides every connection.

Standard library only, and no engine imports: this file is copied into guests.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

__all__ = ["Relay", "apply", "restore"]

_HEAD_LIMIT = 16 * 1024
#: As the Host proxy allows for a request head, and for reaching the other end.
_TIMEOUT = 15.0
#: Seconds of silence before a tunnel's connection to the Host proxy sends keepalive probes.
_IDLE_SEC = 60
#: What ``apply`` changed in the account's system proxy, kept where it outlives a guestd that
#: ends while the Sandbox is sealed: one connected over ``docker exec`` or Orchard's exec ends
#: with its run, and an agent with administrator rights can stop Windows' service. Only the
#: framework reads it.
if os.name == "nt":
    _CHANGED = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "ALE", "system-proxy.json")
elif sys.platform == "darwin":
    _CHANGED = Path("/var/db/ale/system-proxy.json")
else:
    _CHANGED = Path("/run/ale/system-proxy.json")

_lock = threading.Lock()
_relay: Relay | None = None


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
            try:
                threading.Thread(target=self._serve, args=(client,), daemon=True).start()
            except RuntimeError:
                client.close()  # out of threads: this client fails, the next may not

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
                _keep_alive(upstream)
                client.settimeout(None)
                upstream.settimeout(None)
                upstream.sendall(
                    b"\r\n".join([request, self.authorization, *fields]) + b"\r\n\r\n" + rest
                )
                back = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
                back.start()
                _pump(client, upstream)
                back.join()


def _keep_alive(connection: socket.socket) -> None:
    """Probe a quiet tunnel after a minute: a Mac's Softnet forgets a VM's connection that
    carried nothing for five, as a long-poll or an idle WebSocket may not."""
    with contextlib.suppress(OSError):
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        idle = getattr(socket, "TCP_KEEPALIVE", None) or getattr(socket, "TCP_KEEPIDLE", None)
        if idle is not None:
            connection.setsockopt(socket.IPPROTO_TCP, idle, _IDLE_SEC)


def _pump(source: socket.socket, sink: socket.socket) -> None:
    try:
        while chunk := source.recv(65536):
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            sink.shutdown(socket.SHUT_WR)


def apply(proxy: str, bypass: list[str], *, desktop: bool) -> str:
    """Relay to ``proxy``, a URL carrying its credentials, and return the relay's address.

    With ``desktop`` the relay also becomes the agent account's system proxy, bypassed for
    ``bypass``, where the desktop has one. A later call replaces the relay, as a run that
    continues in a retained Sandbox does with its own proxy, and keeps the account's settings
    the first one found.
    """
    global _relay
    with _lock:
        relay = Relay(proxy)
        if _relay is not None:
            _relay.close()
        _relay = relay
        if desktop and _has_settings():
            changed = _load()
            own = changed["own"] if changed else _read_settings()
            _store({"own": own, "relay": None})
            _write_settings(_relay_settings(relay.address, bypass))
            # As the system shows them, which is what ``restore`` compares.
            _store({"own": own, "relay": _read_settings()})
        return relay.address


def restore() -> None:
    """Stop the relay and give the account its own system proxy back.

    Only what still names the relay goes back: a setting the agent changed meanwhile is its
    own work, which verification may be about to look at.
    """
    global _relay
    with _lock:
        if _relay is not None:
            _relay.close()
            _relay = None
        changed = _load()
        if changed is None:
            return
        own, relayed = changed["own"], changed["relay"]
        if relayed is not None:
            own = _unchanged(own, relayed, _read_settings())
        _write_settings(own)
        _CHANGED.unlink(missing_ok=True)


def _unchanged(own: list[Any], relayed: list[Any], current: list[Any]) -> list[Any]:
    """The account's own settings for what still holds what the relay wrote.

    GNOME's and macOS's settings are entries ``[*key, value]``: one gone since, such as a
    network service the agent turned off, is left gone, and one that appeared is left alone.
    Windows' three are matched by position.
    """
    if not _KEYED:
        return [
            mine if now == written else now
            for mine, written, now in zip(own, relayed, current, strict=True)
        ]
    written = {tuple(entry[:-1]): entry[-1] for entry in relayed}
    present = {tuple(entry[:-1]): entry[-1] for entry in current}
    return [
        entry
        for entry in own
        if (key := tuple(entry[:-1])) in present and present[key] == written.get(key)
    ]


def _load() -> dict[str, Any] | None:
    try:
        return json.loads(_CHANGED.read_text())
    except FileNotFoundError:
        return None


def _store(changed: dict[str, Any]) -> None:
    _CHANGED.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _CHANGED.write_text(json.dumps(changed))


# --- macOS: the web and secure web proxies and bypass domains of every enabled network
# service, which Safari, Chrome and other CFNetwork clients read, set as root with
# networksetup for the whole system. Defined everywhere so the unit suite runs it. ---------

_NETWORKSETUP = "/usr/sbin/networksetup"
#: A service's proxies, each read with ``-get<kind>`` and named with ``-set<kind>``.
_PROXIES = ("webproxy", "securewebproxy")
_BYPASS = "proxybypassdomains"


def _networksetup(*args: str) -> str:
    result = subprocess.run(
        [_NETWORKSETUP, *args], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise OSError(f"networksetup {args[0]} failed: {detail}")
    return result.stdout


def _network_services() -> list[str]:
    """The enabled network services; a first line explains the asterisk of a disabled one."""
    lines = _networksetup("-listallnetworkservices").splitlines()
    if lines and "asterisk" in lines[0]:
        lines = lines[1:]
    return [line for line in lines if line.strip() and not line.startswith("*")]


def _read_network_proxies() -> list[list[Any]]:
    """Per service: its bypass domains, then each proxy as ``[enabled, server, port]``."""
    settings: list[list[Any]] = []
    for service in _network_services():
        lines = _networksetup(f"-get{_BYPASS}", service).splitlines()
        domains = [line.strip() for line in lines if line.strip()]
        if domains and domains[0].startswith("There aren't any"):
            domains = []
        settings.append([service, _BYPASS, domains])
        for kind in _PROXIES:
            fields = {}
            for line in _networksetup(f"-get{kind}", service).splitlines():
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
            port = fields.get("Port", "")
            proxy = [fields.get("Enabled") == "Yes", fields.get("Server", ""), int(port or 0)]
            settings.append([service, kind, proxy])
    return settings


def _relay_network_proxies(address: str, bypass: list[str]) -> list[list[Any]]:
    host, _, port = address.rpartition(":")
    settings: list[list[Any]] = []
    for service in _network_services():
        settings.append([service, _BYPASS, list(bypass)])
        settings.extend([service, kind, [True, host, int(port)]] for kind in _PROXIES)
    return settings


def _write_network_proxies(settings: list[list[Any]]) -> None:
    """Each service's bypass domains first, so a proxy is on only once they are set."""
    for service, kind, value in settings:
        if kind == _BYPASS:
            _networksetup(f"-set{_BYPASS}", service, *(value or ["Empty"]))
            continue
        enabled, server, port = value
        if server:
            _networksetup(f"-set{kind}", service, server, str(port))  # which turns it on
        if not enabled:
            _networksetup(f"-set{kind}state", service, "off")


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

    _KEYED = False

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

    def _read_settings() -> list[Any]:
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
        return [options[0].value.number, texts[0], texts[1]]

    def _has_settings() -> bool:
        return True

    def _relay_settings(address: str, bypass: list[str]) -> list[Any]:
        return [_PROXY_TYPE_DIRECT | _PROXY_TYPE_PROXY, address, ";".join(bypass)]

    def _write_settings(settings: list[Any]) -> None:
        flags, server, bypass = settings
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

elif sys.platform == "darwin":
    _KEYED = True

    def _has_settings() -> bool:
        return True

    _read_settings = _read_network_proxies
    _relay_settings = _relay_network_proxies
    _write_settings = _write_network_proxies

else:
    # --- Linux: the agent's GNOME settings, which Chromium-family browsers, Firefox and
    # GNOME's own programs read, and watch, in a GNOME session. A desktop without them keeps
    # the proxy variables alone. ----------------------------------------------------------

    _KEYED = True
    _SCHEMA = "org.gnome.system.proxy"
    #: The keys a relay changes, the mode last so that the proxy is on only once named.
    _GNOME = (
        (_SCHEMA, "ignore-hosts"),
        (f"{_SCHEMA}.http", "host"),
        (f"{_SCHEMA}.http", "port"),
        (f"{_SCHEMA}.https", "host"),
        (f"{_SCHEMA}.https", "port"),
        (_SCHEMA, "mode"),
    )

    def _session() -> tuple[dict[str, Any], dict[str, str]]:
        """How to run a program as the agent account, in its desktop session's settings."""
        import gui

        account, env = gui.session()
        if os.geteuid() != 0:
            return {}, env
        groups = os.getgrouplist(account.pw_name, account.pw_gid)
        return {"user": account.pw_uid, "group": account.pw_gid, "extra_groups": groups}, env

    def _gsettings(session: tuple[dict[str, Any], dict[str, str]], *args: str) -> str:
        identity, env = session
        result = subprocess.run(
            ["gsettings", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            **identity,
        )
        if result.returncode != 0:
            raise OSError(f"gsettings {' '.join(args[:3])} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def _has_settings() -> bool:
        if shutil.which("gsettings") is None:
            return False
        return _SCHEMA in _gsettings(_session(), "list-schemas").split()

    def _read_settings() -> list[list[str]]:
        session = _session()
        return [[schema, key, _gsettings(session, "get", schema, key)] for schema, key in _GNOME]

    def _relay_settings(address: str, bypass: list[str]) -> list[list[str]]:
        host, _, port = address.rpartition(":")
        values = {
            "ignore-hosts": json.dumps(bypass),
            "host": json.dumps(host),
            "port": port,
            "mode": "'manual'",
        }
        return [[schema, key, values[key]] for schema, key in _GNOME]

    def _write_settings(settings: list[list[str]]) -> None:
        session = _session()
        for schema, key, value in settings:
            _gsettings(session, "set", schema, key, value)
