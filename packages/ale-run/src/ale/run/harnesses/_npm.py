"""Small shared helpers for pinned npm-distributed harnesses."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath, PureWindowsPath

from ale.core.errors import AgentError
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import OperatingSystem


async def agent_home(sandbox: Sandbox) -> str:
    if sandbox.request.os is OperatingSystem.WINDOWS:
        result = await sandbox.exec(
            ["python", "-c", "from pathlib import Path; print(Path.home().as_posix())"],
            identity=Identity.AGENT,
        )
        if not result.ok or not result.stdout.strip():
            raise AgentError("could not resolve the Windows agent home")
        return result.stdout.strip()
    result = await sandbox.exec(["sh", "-c", 'printf %s "$HOME"'], identity=Identity.AGENT)
    return result.stdout.strip() or "/home/user"


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


def npm_command(home: str, *args: str) -> list[str]:
    argv = ["npm", *args]
    return bash_command(home, shlex.join(argv), login=False) if is_windows(home) else argv


async def npm_env(sandbox: Sandbox, home: str) -> dict[str, str]:
    """The agent's own npm prefix ahead of the Sandbox's inherited search path.

    Inherited on both systems, so a tool the image put on its ``PATH`` stays reachable.
    """
    home = str(agent_path(home))
    prefix = f"{home}/.local"
    if is_windows(home):
        result = await sandbox.exec(
            ["python", "-c", "import os; print(os.environ['PATH'])"],
            identity=Identity.AGENT,
        )
        if not result.ok or not result.stdout.strip():
            raise AgentError("could not resolve the Windows agent PATH")
        return {
            "PATH": f"{prefix};{result.stdout.strip()}",
            "HOME": home,
            "npm_config_cache": f"{prefix}/.npm-cache",
            "CLAUDE_CODE_GIT_BASH_PATH": "C:/Program Files/Git/bin/bash.exe",
        }
    result = await sandbox.exec(["sh", "-c", 'printf %s "$PATH"'], identity=Identity.AGENT)
    if not result.ok or not result.stdout.strip():
        raise AgentError("could not resolve the agent PATH")
    return {
        "PATH": f"{prefix}/bin:{result.stdout.strip()}",
        "npm_config_cache": f"{prefix}/.npm-cache",
    }


async def ensure_npm_cli(
    sandbox: Sandbox,
    *,
    package: str,
    binary: str,
    version: str,
    extra_env: dict[str, str] | None = None,
) -> str:
    home = await agent_home(sandbox)
    env = {**await npm_env(sandbox, home), **(extra_env or {})}
    probe = await sandbox.exec(
        bash_command(
            home, f"command -v {binary} >/dev/null 2>&1 && {binary} --version", login=False
        ),
        env=env,
        timeout_sec=60,
        identity=Identity.AGENT,
    )
    output = f"{probe.stdout}\n{probe.stderr}"
    installed = reported_version(output, version) if probe.ok else None
    if installed is None:
        # As the agent, into its own prefix: an install into root's prefix is one the
        # unprivileged account may not be able to run. `--force` overwrites a same-version
        # residue cleanly.
        result = await sandbox.exec(
            npm_command(
                home,
                "install",
                "-g",
                "--force",
                "--prefix",
                f"{home}/.local",
                f"{package}@{version}",
            ),
            env=env,
            timeout_sec=1200,
            identity=Identity.AGENT,
        )
        if not result.ok:
            detail = (result.stderr or result.stdout)[-800:]
            raise AgentError(f"could not install {package}@{version}: {detail}")
        probe = await sandbox.exec(
            bash_command(home, f"{binary} --version", login=False),
            env=env,
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        output = f"{probe.stdout}\n{probe.stderr}"
        installed = reported_version(output, version) if probe.ok else None
    if installed is None:
        raise AgentError(version_mismatch(binary, version, output))
    return installed


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
