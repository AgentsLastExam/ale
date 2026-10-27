"""Bounded macOS desktop readiness under the task account's real privileges."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ale.core.errors import ProviderCapabilityError, ProviderStartError
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


async def await_desktop(
    client: GuestClient,
    *,
    agent_user: str,
    python_path: str,
    driver_path: str,
    socket_path: str,
    timeout_sec: float = 120,
) -> None:
    """Wait for capture without accepting a root-only or different-user desktop.

    The probe uses the exact gui.py source staged into the built-in desktop MCP.
    Supplying it as Python source avoids giving the task access to root-only guestd
    files or leaving probe files in its workspace. Driver settings are inherited
    from guestd and checked against the framework's configured paths.
    """
    source = Path(gui.__file__).read_text() + "\n" + _PROBE
    argv = [python_path, "-I", "-S", "-c", source, agent_user, driver_path, socket_path]
    last = "no successful agent desktop capture"
    try:
        async with asyncio.timeout(timeout_sec):
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
