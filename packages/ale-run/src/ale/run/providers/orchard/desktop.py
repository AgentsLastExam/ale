"""Start Cua Driver in the agent's desktop session and wait until it captures that desktop."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.run.framework import MACOS_DRIVER, MACOS_DRIVER_APP, MACOS_DRIVER_SOCKET, MACOS_PYTHON
from ale.run.guestd import gui
from ale.run.transport import GuestClient

_RETRY_INTERVAL = 1.0
_PROBE = """
try:
    probe_macos_desktop(*sys.argv[1:])
except DesktopIdentityError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(78)
except GuiUnavailable as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(75)
print("ready")
"""


def driver_argv(agent_user: str, agent_uid: int) -> list[str]:
    """Open the Driver in the account's login session, as root.

    LaunchServices starts the app under its own identity, which the privacy grants name,
    and with the serve options guestd gives Cua Driver on Linux.
    """
    return [
        "/bin/launchctl",
        "asuser",
        str(agent_uid),
        "/usr/bin/sudo",
        "-u",
        agent_user,
        "/usr/bin/open",
        "-g",
        "-a",
        MACOS_DRIVER_APP,
        "--args",
        "serve",
        "--socket",
        MACOS_DRIVER_SOCKET,
        "--permission-mode",
        "unrestricted",
        "--dangerously-bypass-approvals",
    ]


async def await_desktop(
    client: GuestClient,
    *,
    agent_user: str,
    agent_uid: int,
    timeout_sec: float = 120,
) -> None:
    """Start the Driver once the account is signed in, then wait for it to capture.

    The probe uses the exact gui.py source staged into the built-in desktop MCP.
    Supplying it as Python source avoids giving the task access to root-only guestd
    files or leaving probe files in its workspace. Driver settings are inherited
    from guestd and checked against the framework's own paths.
    """
    source = Path(gui.__file__).read_text() + "\n" + _PROBE
    argv = [MACOS_PYTHON, "-I", "-S", "-c", source, agent_user, MACOS_DRIVER, MACOS_DRIVER_SOCKET]
    last = "no successful agent desktop capture"
    started = False
    try:
        async with asyncio.timeout(timeout_sec):
            while not started:
                code, stdout, _, _ = await client.exec(
                    ["/usr/bin/stat", "-f", "%Su", "/dev/console"], timeout_sec=15
                )
                if code == 0 and stdout.strip() == agent_user:
                    code, _, stderr, timed_out = await client.exec(
                        driver_argv(agent_user, agent_uid), timeout_sec=30
                    )
                    if code or timed_out:
                        raise ProviderStartError(f"could not start Cua Driver: {stderr.strip()}")
                    started = True
                else:
                    last = f"{agent_user} is not signed in to the desktop yet"
                    await asyncio.sleep(_RETRY_INTERVAL)
            while True:
                code, stdout, stderr, timed_out = await client.exec(
                    argv, run_as=agent_user, timeout_sec=min(60, timeout_sec)
                )
                last = (stderr.strip() or stdout.strip() or "desktop probe timed out")[:1000]
                if code == 78:
                    raise ProviderCapabilityError(f"macOS desktop identity check failed: {last}")
                if code == 0 and not timed_out:
                    if stdout.strip() != "ready":
                        raise ProviderStartError("macOS desktop probe returned an invalid result")
                    return
                if code != 75 and not timed_out:
                    raise ProviderStartError(f"could not probe the agent's macOS desktop: {last}")
                await asyncio.sleep(_RETRY_INTERVAL)
    except TimeoutError as exc:
        raise ProviderStartError(
            f"the agent's macOS desktop did not become ready within {timeout_sec:g}s; "
            f"last attempt: {last}"
        ) from exc
