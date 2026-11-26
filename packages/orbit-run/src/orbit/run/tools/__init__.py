"""Framework-owned agent tools staged only when explicitly requested."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath

from orbit.core.harness import ResolvedMcpServer
from orbit.core.sandbox import Identity, Sandbox
from orbit.core.taskspec import OperatingSystem, StdioMcpServer
from orbit.run import guestd
from orbit.run.framework import MACOS_DRIVER, MACOS_DRIVER_SOCKET
from orbit.run.tools.cua_desktop_mcp import SCREENSHOT_MAX_DIMENSION

__all__ = ["CUA_DESKTOP_NAME", "resolved_cua_desktop", "stage_cua_desktop"]

CUA_DESKTOP_NAME = "cua-desktop"
_SOURCES = (
    Path(__file__).resolve().parent / "cua_desktop_mcp.py",
    Path(__file__).resolve().parents[1] / "guestd" / "gui.py",
)


def resolved_cua_desktop(
    operating_system: OperatingSystem = OperatingSystem.LINUX,
    *,
    screenshot_max_dimension: int | None = None,
) -> ResolvedMcpServer:
    """Return the built-in through the same canonical contract as external servers."""
    # The framework's Python, which every account may run, so the image needs none.
    command = guestd.AGENT_PYTHON[operating_system.value]
    # The server's settings travel in its environment: the values are not secret, and a CLI
    # that filters its MCP children's environment still passes what their configuration
    # names. On macOS the Provider starts the Driver, so the server is told where it is
    # rather than starting its own.
    environment = (
        {"ORBIT_CUA_DRIVER": MACOS_DRIVER, "ORBIT_CUA_DRIVER_SOCKET": MACOS_DRIVER_SOCKET}
        if operating_system is OperatingSystem.MACOS
        else {}
    )
    if screenshot_max_dimension is not None:
        environment[SCREENSHOT_MAX_DIMENSION] = str(screenshot_max_dimension)
    hasher = hashlib.sha256()
    hasher.update(command.encode() + b"\0")
    hasher.update(json.dumps(environment, sort_keys=True).encode() + b"\0")
    for source in _SOURCES:
        hasher.update(source.name.encode())
        hasher.update(b"\0")
        hasher.update(source.read_bytes())
        hasher.update(b"\0")
    return ResolvedMcpServer(
        name=CUA_DESKTOP_NAME,
        server=StdioMcpServer(
            name=CUA_DESKTOP_NAME,
            transport="stdio",
            command=command,
            args=("{home}/.orbit-cua-desktop/cua_desktop_mcp.py",),
            environment=environment,
        ),
        source_layers=("run",),
        declared_sources=(CUA_DESKTOP_NAME,),
        digest=f"sha256:{hasher.hexdigest()}",
        source_version=None,
        reportable=True,
        staged_files=Path(__file__).resolve().parent,
    )


async def stage_cua_desktop(sandbox: Sandbox, home: str) -> str:
    """Stage the built-in implementation and return its sandbox directory."""
    path_type = PureWindowsPath if sandbox.request.os.value == "windows" else PurePosixPath
    root = path_type(home) / ".orbit-cua-desktop"
    for source in _SOURCES:
        await sandbox.write_file(
            str(root / source.name), source.read_bytes(), identity=Identity.AGENT
        )
    return str(root)
