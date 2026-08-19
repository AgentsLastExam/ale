"""Framework-owned agent tools staged only when explicitly requested."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath

from ale.core.harness import ResolvedMcpServer
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import StdioMcpServer

__all__ = ["CUA_DESKTOP_NAME", "resolved_cua_desktop", "stage_cua_desktop"]

CUA_DESKTOP_NAME = "cua-desktop"
_SOURCES = (
    Path(__file__).resolve().parent / "cua_desktop_mcp.py",
    Path(__file__).resolve().parents[1] / "guestd" / "gui.py",
)


def resolved_cua_desktop() -> ResolvedMcpServer:
    """Return the built-in through the same canonical contract as external servers."""
    hasher = hashlib.sha256()
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
            command="python3",
            args=("{home}/.ale-cua-desktop/cua_desktop_mcp.py",),
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
    root = path_type(home) / ".ale-cua-desktop"
    for source in _SOURCES:
        await sandbox.write_file(
            str(root / source.name), source.read_bytes(), identity=Identity.AGENT
        )
    return str(root)
