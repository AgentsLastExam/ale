"""One Windows disk starts its desktop at every resolution a Run asks for."""

from __future__ import annotations

import os
import struct
import uuid
from pathlib import Path

import pytest

from ale.core.sandbox import DesktopResolution, ImageRef, SandboxRequest
from ale.core.taskspec import NetworkPolicy, OperatingSystem, Resources
from ale.run.providers.qemu import QemuProvider

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]


@pytest.mark.asyncio
async def test_one_windows_disk_serves_every_desktop_resolution(tmp_path: Path) -> None:
    disk = os.environ.get("ALE_TEST_WINDOWS_IMAGE")
    if not disk:
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to an ALE Windows base disk")
    provider = QemuProvider(image=Path(disk), overlay_dir=tmp_path / "overlays")
    image = await provider.prepare_image(ImageRef(kind="vm", reference=disk))
    for resolution in (DesktopResolution(), DesktopResolution(width=1366, height=768)):
        request = SandboxRequest(
            episode_id=f"resolution-{uuid.uuid4().hex[:8]}",
            os=OperatingSystem.WINDOWS,
            user=os.environ.get("ALE_TEST_WINDOWS_USER", "user"),
            prepared_image=image,
            resources=Resources(cpus=2, memory_mb=8192),
            network=NetworkPolicy(mode="open"),
            desktop_resolution=resolution,
        )
        async with await provider.create(request) as sandbox:
            assert sandbox.allocation.desktop_resolution == resolution
            png = await sandbox.screenshot()
            assert struct.unpack(">II", png[16:24]) == (resolution.width, resolution.height)
