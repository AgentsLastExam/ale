"""Launch an agent command with the interactive account's limited Windows token.

Executed by the elevated guest service as an isolated Python child. Keeping the launcher
alive lets guestd stream output and terminate the entire command tree on timeout. Only
the command, cwd and environment cross stdin; the service's token is never serialized.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
from ctypes import wintypes as w


class StartupInfo(ctypes.Structure):
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


class ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("process", w.HANDLE),
        ("thread", w.HANDLE),
        ("pid", w.DWORD),
        ("tid", w.DWORD),
    ]


def main() -> int:
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    desktop = ctypes.WinDLL("user32", use_last_error=True)
    desktop.GetShellWindow.restype = w.HWND
    desktop.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    security.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    security.GetTokenInformation.argtypes = [
        w.HANDLE,
        ctypes.c_int,
        w.LPVOID,
        w.DWORD,
        ctypes.POINTER(w.DWORD),
    ]
    security.DuplicateTokenEx.argtypes = [
        w.HANDLE,
        w.DWORD,
        w.LPVOID,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(w.HANDLE),
    ]
    security.CreateProcessWithTokenW.argtypes = [
        w.HANDLE,
        w.DWORD,
        w.LPCWSTR,
        w.LPWSTR,
        w.DWORD,
        w.LPVOID,
        w.LPCWSTR,
        ctypes.POINTER(StartupInfo),
        ctypes.POINTER(ProcessInfo),
    ]

    def check(ok: int) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    request = json.load(sys.stdin)
    token, primary = w.HANDLE(), w.HANDLE()
    shell_process = None
    process = ProcessInfo()
    size = w.DWORD()
    try:
        shell_pid = w.DWORD()
        check(desktop.GetWindowThreadProcessId(desktop.GetShellWindow(), ctypes.byref(shell_pid)))
        shell_process = kernel.OpenProcess(0x1000, False, shell_pid)
        check(shell_process)
        check(security.OpenProcessToken(shell_process, 0x000A, ctypes.byref(token)))
        elevated = w.DWORD()
        check(
            security.GetTokenInformation(
                token,
                20,
                ctypes.byref(elevated),
                ctypes.sizeof(elevated),
                ctypes.byref(size),
            )
        )
        if elevated.value:
            raise PermissionError("the desktop agent token is elevated; enable UAC in the image")
        # Ask for the rights permitted by the desktop token's DACL (MAXIMUM_ALLOWED).
        check(security.DuplicateTokenEx(token, 0x02000000, None, 2, 1, ctypes.byref(primary)))

        env = request["env"]
        if request.get("shell"):
            shell = os.path.join(os.environ["SYSTEMROOT"], "System32", "cmd.exe")
            command = f'{subprocess.list2cmdline([shell])} /d /s /c "{request["shell"]}"'
        else:
            command = subprocess.list2cmdline(request["argv"])
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(env.items())) + "\0"
        )
        startup = StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.desktop = "winsta0\\default"
        startup.flags = 0x00000100  # STARTF_USESTDHANDLES
        if request.get("shell") or _console_program(request["argv"][0], env):
            # It gets a console of its own, since this call cannot share guestd's; hidden, as
            # guestd's is, so nothing appears on the agent's desktop. A window program is
            # not hidden: the setting would apply to its first window.
            startup.flags |= 0x00000001  # STARTF_USESHOWWINDOW
            startup.show = 0  # SW_HIDE
        startup.stdout = msvcrt.get_osfhandle(sys.stdout.fileno())
        startup.stderr = msvcrt.get_osfhandle(sys.stderr.fileno())
        with open(os.devnull, "rb") as null:
            startup.stdin = msvcrt.get_osfhandle(null.fileno())
            for handle in (startup.stdin, startup.stdout, startup.stderr):
                os.set_handle_inheritable(handle, True)
            check(
                security.CreateProcessWithTokenW(
                    primary,
                    0,
                    None,
                    ctypes.create_unicode_buffer(command),
                    0x00000400 | 0x00000200,  # Unicode environment, new process group.
                    environment,
                    request.get("cwd"),
                    ctypes.byref(startup),
                    ctypes.byref(process),
                )
            )
        if kernel.WaitForSingleObject(process.process, 0xFFFFFFFF) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        code = w.DWORD()
        check(kernel.GetExitCodeProcess(process.process, ctypes.byref(code)))
        return code.value
    finally:
        for handle in (process.thread, process.process, primary, token, shell_process):
            if handle:
                kernel.CloseHandle(handle)


def _console_program(name: str, env: dict[str, str]) -> bool:
    """Whether ``name`` runs in a console; a script or an unknown program is taken to."""
    found = shutil.which(name, path=env.get("PATH") or env.get("Path"))
    if not found or not found.lower().endswith(".exe"):
        return True
    try:
        with open(found, "rb") as program:
            program.seek(0x3C)
            header = int.from_bytes(program.read(4), "little")
            program.seek(header + 0x5C)  # PE optional header's Subsystem
            return int.from_bytes(program.read(2), "little") != 2  # 2: a window program
    except (OSError, ValueError):
        return True


if __name__ == "__main__":
    raise SystemExit(main())
