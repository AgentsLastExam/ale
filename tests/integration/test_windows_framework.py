"""A Windows disk that carries no framework gets it from the Provider, locked down."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from ale.core.sandbox import Identity, ImageRef, SandboxRequest
from ale.core.taskspec import NetworkPolicy, OperatingSystem, Resources
from ale.run.framework import WINDOWS_MARKER, windows_framework_identity
from ale.run.providers.qemu import QemuProvider

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]

POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]


@pytest.mark.asyncio
async def test_a_bare_windows_disk_boots_with_the_framework_closed_to_the_agent(
    tmp_path: Path,
) -> None:
    disk = os.environ.get("ALE_TEST_WINDOWS_IMAGE")
    if not disk:
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to an ALE Windows base disk")
    user = os.environ.get("ALE_TEST_WINDOWS_USER", "user")
    provider = QemuProvider(image=Path(disk), overlay_dir=tmp_path / "overlays")
    request = SandboxRequest(
        episode_id=f"framework-{uuid.uuid4().hex[:8]}",
        os=OperatingSystem.WINDOWS,
        user=user,
        sudo=False,
        prepared_image=await provider.prepare_image(ImageRef(kind="vm", reference=disk)),
        resources=Resources(cpus=2, memory_mb=8192),
        network=NetworkPolicy(mode="open"),
    )
    async with await provider.create(request) as sandbox:
        marker = await sandbox.read_file(r"C:\ProgramData\ALE" + "\\framework")
        assert marker.decode().strip() == windows_framework_identity(user=user)
        assert WINDOWS_MARKER.endswith("/framework")
        # The agent cannot read or change the framework it is driven through.
        read = await sandbox.exec(
            [*POWERSHELL, r"Get-Content C:\ProgramData\ALE\guestd\main.py -TotalCount 1"],
            identity=Identity.AGENT,
        )
        assert not read.ok
        write = await sandbox.exec(
            [*POWERSHELL, r"Set-Content C:\ProgramData\ALE\guestd\planted.py 'x'"],
            identity=Identity.AGENT,
        )
        assert not write.ok
        # The service runs as the system; guestd and Cua Driver run in the agent's session.
        report = await sandbox.exec(
            [
                *POWERSHELL,
                "(Get-Service ALEGuestd).Status; "
                "Get-CimInstance Win32_Process "
                "| Where-Object { $_.Name -match '^(python|cua-driver)' } "
                '| ForEach-Object { "session=$($_.SessionId) $($_.CommandLine)" }',
            ],
            identity=Identity.FRAMEWORK,
        )
        assert report.ok, report.stderr
        lines = report.stdout.splitlines()
        assert lines[0].strip() == "Running"
        session = {
            name: line.split()[0]
            for line in lines[1:]
            for name in ("service.py", "main.py", "cua-driver.exe")
            if name in line
        }
        assert session == {
            "service.py": "session=0",
            "main.py": "session=1",
            "cua-driver.exe": "session=1",
        }
