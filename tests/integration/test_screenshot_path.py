"""The sandbox desktop path is backed by Cua Driver end to end."""

from __future__ import annotations

import asyncio

import pytest

from ale.core.sandbox import SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources
from ale.run.providers.docker import DockerProvider
from tests.support import prepare_reference

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_gui]

GUI_IMAGE = "ghcr.io/agentslastexam/container-ubuntu22-base:latest"

PROBE = """
import sys
sys.path.insert(0, "/opt/ale/guestd")
import gui
png = gui.capture_screen()
print(gui._binary(), len(png), png.startswith(bytes([137, 80, 78, 71])))
"""


@pytest.mark.asyncio
async def test_cua_driver_is_the_only_screenshot_backend() -> None:
    provider = DockerProvider()
    prepared = await prepare_reference(provider, GUI_IMAGE)
    request = SandboxRequest(
        episode_id="shotpath",
        prepared_image=prepared,
        resources=Resources(cpus=2, memory_mb=2048),
        network=NetworkPolicy(),
    )

    async with await provider.create(request) as sandbox:
        result = await sandbox.exec(["python3", "-c", PROBE], timeout_sec=120)

    assert result.exit_code == 0, result.stderr
    assert "cua-driver" in result.stdout
    assert result.stdout.rstrip().endswith("True"), result.stdout


@pytest.mark.asyncio
async def test_a_screenshot_through_the_guest_service_is_a_png() -> None:
    """The route an agent actually uses, end to end."""
    provider = DockerProvider()
    prepared = await prepare_reference(provider, GUI_IMAGE)
    request = SandboxRequest(
        episode_id="shotwire",
        prepared_image=prepared,
        resources=Resources(cpus=2, memory_mb=2048),
        network=NetworkPolicy(),
    )

    async with await provider.create(request) as sandbox:
        png = await asyncio.wait_for(sandbox.screenshot(), timeout=60)

    assert png[:4] == b"\x89PNG"
    assert len(png) > 1000, "a screenshot this small is an empty or failed capture"
