"""The desktop resolution a Run sets for every Sandbox, and the check that it holds.

The resolution is not part of any image: each Provider applies the request's as the
Sandbox starts, then asks the desktop what it got, so a desktop that ignored it fails
the Sandbox rather than leaving an agent a screen its RunLock does not describe.
"""

from __future__ import annotations

from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import DesktopResolution
from ale.run.transport import GuestClient

__all__ = ["CONTAINER_VARIABLE", "require_resolution"]

#: Where a container desktop takes its size from. The name is the one VNC desktop images
#: share, the ALE container base among them, but any X server can start at it.
CONTAINER_VARIABLE = "VNC_RESOLUTION"


async def require_resolution(
    client: GuestClient, requested: DesktopResolution, *, container: bool = False
) -> DesktopResolution:
    """The desktop's resolution, once it has shown that it is the one requested.

    A container's desktop is its image's own X server, so only the image can fix one of
    another size, and the refusal says how.
    """
    width, height = await client.screen_size()
    if (width, height) != (requested.width, requested.height):
        fix = (
            f"the image's X server must start at the size {CONTAINER_VARIABLE} names, "
            f"as in Xvfb :1 -screen 0 ${{{CONTAINER_VARIABLE}}}x24; "
            if container
            else ""
        )
        raise ProviderCapabilityError(
            f"the desktop is {width}x{height}, not the requested {requested}; {fix}"
            "see the Providers README on desktop resolution"
        )
    return requested
