"""The ALE guest service on Windows: runs guestd and Cua Driver in the agent's session.

The framework is installed into a disk offline, and a logon task can only be registered by
a running Windows, so this service starts the framework instead. Windows starts it at boot
as LocalSystem, installed as ``python.exe -I -S service.py <agent account>``. It waits for
the agent to sign in, then starts guestd and Cua Driver on the agent's desktop with the
account's elevated token, and starts them again when they exit. Agent commands are still
launched unelevated by guestd itself (``windows.py``).

An offline write leaves the files it makes readable and writable by everyone, so the
service first limits the framework's files to SYSTEM and Administrators.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
from ctypes import wintypes as w
from pathlib import Path

NAME = "ALEGuestd"
ROOT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ALE"
PORT = 7411
#: SMBIOS serial the Host sets when the Task grants the agent Administrator access.
SUDO_SERIAL = "ale-sudo"
#: Where Cua Driver serves guestd's calls; ``gui.py`` connects to the same pipe.
CUA_DRIVER_PIPE = r"\\.\pipe\ale-cua-driver"
_CUA_DRIVER_ARGS = (
    f"serve --socket {CUA_DRIVER_PIPE} --permission-mode unrestricted "
    "--dangerously-bypass-approvals"
)
_SYSTEM, _ADMINISTRATORS = "*S-1-5-18", "*S-1-5-32-544"

_STOP, _SHUTDOWN = 0x1, 0x4
_RUNNING, _STOPPED = 4, 1
_SERVICE_WIN32_OWN_PROCESS = 0x10
_CREATE_UNICODE_ENVIRONMENT, _CREATE_NEW_CONSOLE = 0x400, 0x10
_STARTF_USESHOWWINDOW, _SW_HIDE = 0x1, 0
_MAXIMUM_ALLOWED = 0x02000000
_TOKEN_ELEVATION_TYPE, _TOKEN_LINKED_TOKEN = 18, 19
_ELEVATION_LIMITED = 3
_WAIT_FAILED = 0xFFFFFFFF

_SERVICE_MAIN = ctypes.WINFUNCTYPE(None, w.DWORD, ctypes.POINTER(w.LPWSTR))
_HANDLER = ctypes.WINFUNCTYPE(w.DWORD, w.DWORD, w.DWORD, w.LPVOID, w.LPVOID)


class _TableEntry(ctypes.Structure):
    _fields_ = [("name", w.LPWSTR), ("proc", _SERVICE_MAIN)]


class _Status(ctypes.Structure):
    _fields_ = [
        (field, w.DWORD)
        for field in ("type", "state", "accepted", "exit", "specific", "checkpoint", "hint")
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("reserved", w.LPWSTR),
        ("desktop", w.LPWSTR),
        ("title", w.LPWSTR),
        ("x", w.DWORD),
        ("y", w.DWORD),
        ("x_size", w.DWORD),
        ("y_size", w.DWORD),
        ("x_chars", w.DWORD),
        ("y_chars", w.DWORD),
        ("fill", w.DWORD),
        ("flags", w.DWORD),
        ("show", w.WORD),
        ("reserved_size", w.WORD),
        ("reserved_bytes", w.LPBYTE),
        ("stdin", w.HANDLE),
        ("stdout", w.HANDLE),
        ("stderr", w.HANDLE),
    ]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


class Service:
    """One run of the service: its Windows entry points and the processes it keeps up."""

    def __init__(self, user: str, runner_address: str) -> None:
        self.user = user
        self.runner_address = runner_address
        self.stopping = threading.Event()
        self.status = w.HANDLE()
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        self.sessions = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self.profiles = ctypes.WinDLL("userenv", use_last_error=True)
        self._declare()

    def _declare(self) -> None:
        self.kernel.WTSGetActiveConsoleSessionId.restype = w.DWORD
        self.kernel.WaitForMultipleObjects.argtypes = [
            w.DWORD,
            ctypes.POINTER(w.HANDLE),
            w.BOOL,
            w.DWORD,
        ]
        self.kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.security.RegisterServiceCtrlHandlerExW.restype = w.HANDLE
        self.security.RegisterServiceCtrlHandlerExW.argtypes = [w.LPCWSTR, _HANDLER, w.LPVOID]
        self.security.SetServiceStatus.argtypes = [w.HANDLE, ctypes.POINTER(_Status)]
        self.security.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(_TableEntry)]
        self.security.GetTokenInformation.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            w.LPVOID,
            w.DWORD,
            ctypes.POINTER(w.DWORD),
        ]
        self.security.DuplicateTokenEx.argtypes = [
            w.HANDLE,
            w.DWORD,
            w.LPVOID,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(w.HANDLE),
        ]
        self.security.CreateProcessAsUserW.argtypes = [
            w.HANDLE,
            w.LPCWSTR,
            w.LPWSTR,
            w.LPVOID,
            w.LPVOID,
            w.BOOL,
            w.DWORD,
            w.LPVOID,
            w.LPCWSTR,
            ctypes.POINTER(_StartupInfo),
            ctypes.POINTER(_ProcessInfo),
        ]
        self.sessions.WTSQueryUserToken.argtypes = [w.ULONG, ctypes.POINTER(w.HANDLE)]
        self.profiles.CreateEnvironmentBlock.argtypes = [
            ctypes.POINTER(w.LPVOID),
            w.HANDLE,
            w.BOOL,
        ]
        self.profiles.DestroyEnvironmentBlock.argtypes = [w.LPVOID]

    # --- Windows entry points ---

    def run(self) -> None:
        """Hand this process to the service control manager until the service stops."""

        @_SERVICE_MAIN
        def service_main(argc, argv):  # type: ignore[no-untyped-def]
            self._serve()

        table = (_TableEntry * 2)(
            _TableEntry(NAME, service_main), _TableEntry(None, _SERVICE_MAIN())
        )
        if not self.security.StartServiceCtrlDispatcherW(table):
            raise ctypes.WinError(ctypes.get_last_error())

    def _serve(self) -> None:
        @_HANDLER
        def control(code, event, data, context):  # type: ignore[no-untyped-def]
            if code in (_STOP, _SHUTDOWN):
                self.stopping.set()
            return 0

        self.status.value = self.security.RegisterServiceCtrlHandlerExW(NAME, control, None)
        self._report(_RUNNING)
        failure = 0
        try:
            self._keep_up()
        except Exception as error:
            self._log(f"stopped: {error}")
            failure = 1
        self._report(_STOPPED, failure)

    def _report(self, state: int, failure: int = 0) -> None:
        accepted = _STOP | _SHUTDOWN if state == _RUNNING else 0
        status = _Status(_SERVICE_WIN32_OWN_PROCESS, state, accepted, failure, 0, 0, 0)
        self.security.SetServiceStatus(self.status, ctypes.byref(status))

    # --- what the service does ---

    def _keep_up(self) -> None:
        self._lock_down()
        self._open_port()
        sudo = "1" if self._serial() == SUDO_SERIAL else "0"
        guestd = f'"{sys.executable}" -I -S "{ROOT / "guestd" / "main.py"}" --tcp 0.0.0.0:{PORT}'
        driver = f'"{ROOT / "cua-driver" / "cua-driver.exe"}" {_CUA_DRIVER_ARGS}'
        while not self.stopping.is_set():
            token = self._agent_token()
            if token is None:
                return
            running: dict[str, w.HANDLE] = {}
            try:
                environment = self._environment(token, sudo)
                while not self.stopping.is_set():
                    for command in (guestd, driver):
                        if command not in running:
                            running[command] = self._start(token, command, environment)
                    if self._reap(running):
                        self.stopping.wait(1)
            except OSError as error:
                # The session ended, or a program would not start; sign-in is awaited again.
                self._log(f"restarting: {error}")
            finally:
                for handle in running.values():
                    self.kernel.TerminateProcess(handle, 1)
                    self.kernel.CloseHandle(handle)
                self.kernel.CloseHandle(token)
            self.stopping.wait(1)

    def _reap(self, running: dict[str, w.HANDLE]) -> bool:
        """Wait up to a second for a program to exit; forget it if one did."""
        commands = list(running)
        handles = (w.HANDLE * len(commands))(*running.values())
        waited = self.kernel.WaitForMultipleObjects(len(commands), handles, False, 1000)
        if waited == _WAIT_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
        if waited >= len(commands):  # timed out
            return False
        self.kernel.CloseHandle(running.pop(commands[waited]))
        return True

    def _lock_down(self) -> None:
        """Limit the framework's files to SYSTEM and Administrators, as /opt/ale is on Linux."""
        root, owner = str(ROOT), _ADMINISTRATORS
        grant = f"{_SYSTEM}:(OI)(CI)(F)", f"{owner}:(OI)(CI)(F)"
        self._run("icacls", root, "/setowner", owner)
        self._run("icacls", root, "/reset")
        self._run("icacls", root, "/inheritance:r", "/grant:r", *grant)
        for name in ("guestd", "cua-driver", "framework"):
            # Files written offline carry no access list; they now inherit the directory's.
            self._run("icacls", str(ROOT / name), "/setowner", owner, "/T")
            self._run("icacls", str(ROOT / name), "/reset", "/T")

    def _open_port(self) -> None:
        """Let the Host's runner, and nothing else, reach guestd."""
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule", "name=ALE guest service"],
            capture_output=True,
            check=False,
        )
        self._run(
            "netsh", "advfirewall", "firewall", "add", "rule", "name=ALE guest service", "dir=in",
            "action=allow", "protocol=TCP", f"localport={PORT}",
            f"remoteip={self.runner_address}",
        )  # fmt: skip

    def _serial(self) -> str:
        shown = self._run(
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "(Get-CimInstance Win32_ComputerSystemProduct).IdentifyingNumber",
        )  # fmt: skip
        return shown.strip()

    def _run(self, *argv: str) -> str:
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise OSError(f"{argv[0]} failed ({done.returncode}): {done.stderr or done.stdout}")
        return done.stdout

    def _agent_token(self) -> w.HANDLE | None:
        """The agent's full token, once the account is signed in; None if the service stops."""
        while not self.stopping.is_set():
            session = self.kernel.WTSGetActiveConsoleSessionId()
            limited = w.HANDLE()
            if session != 0xFFFFFFFF and self.sessions.WTSQueryUserToken(
                session, ctypes.byref(limited)
            ):
                try:
                    return self._linked(limited)
                finally:
                    self.kernel.CloseHandle(limited)
            self.stopping.wait(0.5)
        return None

    def _linked(self, limited: w.HANDLE) -> w.HANDLE:
        """The elevated token UAC keeps beside a signed-in administrator's limited one."""
        kind, size = w.DWORD(), w.DWORD()
        self._check(
            self.security.GetTokenInformation(
                limited, _TOKEN_ELEVATION_TYPE, ctypes.byref(kind), 4, ctypes.byref(size)
            )
        )
        if kind.value != _ELEVATION_LIMITED:
            raise OSError(
                f"the agent account has no elevated token (type {kind.value}); enable UAC"
            )
        full, primary = w.HANDLE(), w.HANDLE()
        self._check(
            self.security.GetTokenInformation(
                limited,
                _TOKEN_LINKED_TOKEN,
                ctypes.byref(full),
                ctypes.sizeof(full),
                ctypes.byref(size),
            )
        )
        try:
            self._check(
                self.security.DuplicateTokenEx(
                    full, _MAXIMUM_ALLOWED, None, 2, 1, ctypes.byref(primary)
                )
            )
        finally:
            self.kernel.CloseHandle(full)
        return primary

    def _environment(self, token: w.HANDLE, sudo: str) -> ctypes.Array[ctypes.c_wchar]:
        """The agent's own environment, plus what guestd reads about the account."""
        block = w.LPVOID()
        self._check(self.profiles.CreateEnvironmentBlock(ctypes.byref(block), token, False))
        entries: list[str] = []
        try:
            address = block.value or 0
            while entry := ctypes.wstring_at(address):
                entries.append(entry)
                address += (len(entry) + 1) * 2
        finally:
            self.profiles.DestroyEnvironmentBlock(block)
        values: dict[str, str] = {}
        for entry in entries:
            key, _, value = entry.partition("=")
            if key:
                values[key] = value
        values.update(
            ALE_AGENT_USER=self.user,
            ALE_AGENT_HOME=values.get("USERPROFILE", ""),
            ALE_AGENT_SUDO=sudo,
        )
        return ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(values.items())) + "\0"
        )

    def _start(self, token: w.HANDLE, command: str, environment: object) -> w.HANDLE:
        """Start ``command`` hidden on the agent's desktop; its console is hidden too."""
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.desktop = "winsta0\\default"
        startup.flags = _STARTF_USESHOWWINDOW
        startup.show = _SW_HIDE
        process = _ProcessInfo()
        self._check(
            self.security.CreateProcessAsUserW(
                token,
                None,
                ctypes.create_unicode_buffer(command),
                None,
                None,
                False,
                _CREATE_UNICODE_ENVIRONMENT | _CREATE_NEW_CONSOLE,
                environment,
                str(ROOT),
                ctypes.byref(startup),
                ctypes.byref(process),
            )
        )
        self.kernel.CloseHandle(process.thread)
        return process.process

    def _check(self, ok: int) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def _log(self, message: str) -> None:
        with (ROOT / "service.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{message}\n")


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: service.py <agent account> <runner address>\n")
        return 2
    Service(sys.argv[1], sys.argv[2]).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
