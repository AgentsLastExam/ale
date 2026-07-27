"""Tools an agent runs with, staged into the sandbox rather than assumed present."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from ale.core.sandbox import Identity, Sandbox

__all__ = ["DESKTOP_SERVER_NAME", "stage_desktop_bridge"]

#: What the agent's client will call this server. Tool names reach the model as
#: ``mcp__<server>__<tool>``, so it is short and says what it drives.
DESKTOP_SERVER_NAME = "desktop"

#: The two files that make up the bridge: the MCP server and the action layer it calls.
#: Copied rather than imported from ``/opt/ale``, which the agent cannot read — and should
#: not, since that is the service driving its own sandbox.
_SOURCES = (
    Path(__file__).resolve().parent / "desktop_mcp.py",
    Path(__file__).resolve().parents[1] / "guestd" / "gui.py",
)


async def stage_desktop_bridge(sandbox: Sandbox, home: str) -> str:
    """Put the desktop bridge where the agent can run it, and return its config path.

    Staged as the agent because the agent's own client is what launches it: a file the
    agent cannot execute is a server that never starts, and the failure surfaces as a
    model that simply never uses the tools.
    """
    root = PurePosixPath(home) / ".ale-desktop"
    await sandbox.exec(["mkdir", "-p", str(root)], identity=Identity.AGENT)
    for source in _SOURCES:
        await sandbox.write_file(root / source.name, source.read_bytes(), identity=Identity.AGENT)

    config = {
        "mcpServers": {
            DESKTOP_SERVER_NAME: {
                "command": "python3",
                "args": [str(root / "desktop_mcp.py")],
            }
        }
    }
    config_path = root / "mcp.json"
    await sandbox.write_file(
        config_path, json.dumps(config, indent=2).encode("utf-8"), identity=Identity.AGENT
    )
    return str(config_path)
