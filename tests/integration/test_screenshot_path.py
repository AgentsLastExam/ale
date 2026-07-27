"""The fast screen capture is actually the one being used.

This exists because the fast path failed silently for as long as it existed. Capture
tries Xlib and Pillow in-process first and falls back to shelling out, and the fallback
is a correct screenshot — just twenty times slower. So when a symlinked interpreter lost
its own site-packages, nothing broke, nothing warned, and every episode paid the cost.

A test that only asserts "a screenshot came back" would have passed throughout.
"""

from __future__ import annotations

import asyncio

import pytest

from ale.core.sandbox import SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources
from ale.run.providers.docker import DockerProvider

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_gui]

GUI_IMAGE = "ghcr.io/agentslastexam/sandbox-base-gui:latest"

#: Runs inside the sandbox: import what the fast path needs and capture through it,
#: reporting which route actually ran rather than only whether an image appeared.
PROBE = """
import sys
sys.path.insert(0, "/opt/ale/guestd")
import gui

try:
    png = gui._capture_in_process()
except Exception as exc:
    print("FALLBACK", type(exc).__name__, exc)
else:
    print("INPROCESS", len(png), png.startswith(bytes([137, 80, 78, 71])))
"""


@pytest.mark.asyncio
async def test_the_in_process_path_is_the_one_taken() -> None:
    """The image installs Pillow and python-xlib, so nothing should shell out."""
    provider = DockerProvider()
    request = SandboxRequest(
        episode_id="shotpath",
        image_ref=GUI_IMAGE,
        resources=Resources(cpus=2, memory_mb=2048),
        network=NetworkPolicy(),
    )

    async with await provider.create(request) as sandbox:
        result = await sandbox.exec(["python3", "-c", PROBE], timeout_sec=120)

    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("INPROCESS"), (
        f"capture fell back to an external tool: {result.stdout.strip()} {result.stderr.strip()}"
    )
    assert "True" in result.stdout, "the fast path returned something that is not a PNG"


@pytest.mark.asyncio
async def test_a_screenshot_through_the_guest_service_is_a_png() -> None:
    """The route an agent actually uses, end to end."""
    provider = DockerProvider()
    request = SandboxRequest(
        episode_id="shotwire",
        image_ref=GUI_IMAGE,
        resources=Resources(cpus=2, memory_mb=2048),
        network=NetworkPolicy(),
    )

    async with await provider.create(request) as sandbox:
        png = await asyncio.wait_for(sandbox.screenshot(), timeout=60)

    assert png[:4] == b"\x89PNG"
    assert len(png) > 1000, "a screenshot this small is an empty or failed capture"
