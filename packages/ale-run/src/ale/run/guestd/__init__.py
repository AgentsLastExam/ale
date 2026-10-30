"""ale-guestd: the in-sandbox execution service.

Images carry no copy of their own: the engine installs these files into every sandbox it
creates or VM disk it prepares, so the guest always runs this checkout's code. It is
shipped as plain source, so it must stay standard-library only and must never import
engine code: guest interpreters are not ours to manage.
"""

from pathlib import Path

#: The files a sandbox's guest service consists of, and where the engine reads them.
SOURCE = Path(__file__).resolve().parent
FILES = ("main.py", "protocol.py", "gui.py", "windows.py", "service.py", "tunnel.py")
#: The port a VM's guest service listens on, forwarded to an ephemeral Host port.
PORT = 7411
#: The Host's address on a VM's network. It is the only address a VM's firewall lets reach the
#: guest service.
HOST_ADDRESS = "172.30.0.1"
#: The framework's own Python, which guestd runs on, per guest operating system. Only the
#: framework's identity can run it: root on Linux, the system and Administrators on Windows.
PYTHON = {
    "linux": "/opt/ale/python/bin/python3",
    "windows": r"C:\ProgramData\ALE\python\python.exe",
}
