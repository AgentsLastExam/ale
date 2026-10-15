"""ale-guestd: the in-sandbox execution service.

Images carry no copy of their own: the engine installs these files into every sandbox it
creates or VM disk it prepares, so the guest always runs this checkout's code. It is
shipped as plain source, so it must stay standard-library only and must never import
engine code: guest interpreters are not ours to manage.
"""

from pathlib import Path

#: The files a sandbox's guest service consists of, and where the engine reads them.
SOURCE = Path(__file__).resolve().parent
FILES = ("main.py", "protocol.py", "gui.py")
