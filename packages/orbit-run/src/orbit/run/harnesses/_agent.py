"""The agent's side of a Sandbox, as every CLI Harness reaches it."""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

from orbit.core.errors import AgentError
from orbit.core.sandbox import Identity, Sandbox
from orbit.core.taskspec import OperatingSystem
from orbit.run import guestd

#: The agent's home and search path on Windows, as the framework's Python reads them.
_HOME = "from pathlib import Path; print(Path.home().as_posix())"
_PATH = "import os; print(os.environ['PATH'])"


async def agent_home(sandbox: Sandbox) -> str:
    if sandbox.request.os is OperatingSystem.WINDOWS:
        result = await sandbox.exec(
            [agent_python(OperatingSystem.WINDOWS), "-I", "-c", _HOME],
            identity=Identity.AGENT,
        )
        if not result.ok or not result.stdout.strip():
            raise AgentError("could not resolve the Windows agent home")
        return result.stdout.strip()
    result = await sandbox.exec(["sh", "-c", 'printf %s "$HOME"'], identity=Identity.AGENT)
    return result.stdout.strip() or "/home/user"


def agent_python(operating_system: OperatingSystem) -> str:
    """The Python the agent runs Orbit's helper scripts on: the framework's, which every
    account may run, so the image needs none."""
    return guestd.AGENT_PYTHON[operating_system.value]


def agent_path(home: str) -> PurePosixPath:
    return PurePosixPath(PureWindowsPath(home).as_posix() if is_windows(home) else home)


def is_windows(home: str) -> bool:
    return PureWindowsPath(home).is_absolute()


def bash_command(home: str, command: str, *, login: bool = True) -> list[str]:
    if is_windows(home):
        return [
            "C:/Program Files/Git/bin/bash.exe",
            "-c",
            'export PATH="/usr/bin:$PATH"; ' + command,
        ]
    return ["bash", "-lc", command] if login else ["sh", "-c", command]


async def agent_env(sandbox: Sandbox, home: str) -> dict[str, str]:
    """The agent's own ``~/.local/bin``, where its CLI's launcher is, ahead of the Sandbox's
    inherited search path.

    Inherited on every system, so a tool the image put on its ``PATH`` stays reachable.
    """
    home = str(agent_path(home))
    bin_dir = f"{home}/.local/bin"
    if is_windows(home):
        result = await sandbox.exec(
            [agent_python(OperatingSystem.WINDOWS), "-I", "-c", _PATH],
            identity=Identity.AGENT,
        )
        if not result.ok or not result.stdout.strip():
            raise AgentError("could not resolve the Windows agent PATH")
        return {
            "PATH": f"{bin_dir};{result.stdout.strip()}",
            "HOME": home,
            "CLAUDE_CODE_GIT_BASH_PATH": "C:/Program Files/Git/bin/bash.exe",
        }
    result = await sandbox.exec(["sh", "-c", 'printf %s "$PATH"'], identity=Identity.AGENT)
    if not result.ok or not result.stdout.strip():
        raise AgentError("could not resolve the agent PATH")
    return {"PATH": f"{bin_dir}:{result.stdout.strip()}"}


def reported_version(output: str, version: str) -> str | None:
    """``version`` when the program's ``--version`` output names it as a whole token.

    A whole token, so ``2.1.22`` never matches ``2.1.227`` while a build revision such as
    ``1.4.13-R6996.1`` can be pinned; a library banner elsewhere in the output is ignored.
    """
    named = re.search(rf"(?<![\w.+-])v?{re.escape(version)}(?![\w.+-])", output)
    return version if named else None


def version_mismatch(binary: str, version: str, output: str) -> str:
    lines = output.strip().splitlines()
    return (
        f"{binary} --version does not name {version}: {lines[0][:160] if lines else 'no output'!r}"
    )
