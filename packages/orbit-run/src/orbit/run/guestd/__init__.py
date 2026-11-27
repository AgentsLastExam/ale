"""orbit-guestd: the in-sandbox execution service.

Images carry no copy of their own: the engine installs these files into every sandbox it
creates or VM disk it prepares, so the guest always runs this checkout's code. It is
shipped as plain source, so it must stay standard-library only and must never import
engine code: guest interpreters are not ours to manage.
"""

from pathlib import Path, PureWindowsPath

#: The files a sandbox's guest service consists of, and where the engine reads them.
SOURCE = Path(__file__).resolve().parent
FILES = (
    "main.py",
    "protocol.py",
    "gui.py",
    "windows.py",
    "service.py",
    "tunnel.py",
    "egress.py",
    "nodes.py",
)
#: The port a QEMU VM's guest service listens on, forwarded to an ephemeral Host port.
PORT = 7411
#: The Host's address on a QEMU VM's network. It is the only address a QEMU VM's firewall
#: lets reach the guest service.
HOST_ADDRESS = "172.30.0.1"
#: The framework's own Python, which guestd runs on, per guest operating system. Every
#: account may run it and only the framework may change it, because the agent's desktop
#: tool, program installs and Harness helpers run on it too: on Linux and macOS it sits
#: beside the framework's private ``/opt/orbit``, and on Windows it is the one directory of
#: the framework's ``ProgramData`` that every account may read and run.
PYTHON = {
    "linux": "/opt/orbit-python/bin/python3",
    "windows": r"C:\ProgramData\Orbit\python\python.exe",
    "macos": "/opt/orbit-python/bin/python3",
}
#: The same Python as the agent's commands and its CLIs' configuration name it: Windows' with
#: forward slashes, which Git Bash, those files and Windows itself all take as written.
AGENT_PYTHON = {**PYTHON, "windows": PureWindowsPath(PYTHON["windows"]).as_posix()}
