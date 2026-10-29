"""The upload policy on a Windows VM, where it is an ACL reset rather than modes.

Linux backends are held to it by the shared Provider conformance suite.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from ale.core.sandbox import Identity, ImageRef, SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources
from ale.run.providers.qemu import QemuProvider

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]


@pytest.mark.asyncio
async def test_windows_upload_permissions_ignore_host_modes(tmp_path: Path) -> None:
    disk = os.environ.get("ALE_TEST_WINDOWS_IMAGE")
    if not disk:
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to a prepared VM disk")
    provider = QemuProvider(image=Path(disk), overlay_dir=tmp_path / "overlays")
    image = ImageRef(kind="vm", reference="local/upload-permissions")

    source = tmp_path / "source"
    (source / "assets/empty").mkdir(parents=True)
    (source / "assets/data.txt").write_text("input")
    (source / ".hidden").write_text("hidden")
    (source / "setup.ps1").write_text("Write-Output stage-ok")
    request = SandboxRequest(
        episode_id=f"upload-{uuid.uuid4().hex[:8]}",
        os="windows",
        user=os.environ.get("ALE_TEST_WINDOWS_USER", "user"),
        sudo=False,
        prepared_image=await provider.prepare_image(image),
        resources=Resources(cpus=2, memory_mb=8192),
        network=NetworkPolicy(mode="open"),
    )
    async with await provider.create(request) as sandbox:
        target = r"C:\ProgramData\ALE\setup"
        # Re-upload over existing paths, changing both Host modes and destination ownership.
        for identity, mode in [(Identity.AGENT, 0o777), (Identity.FRAMEWORK, 0o600)]:
            for path in source.rglob("*"):
                path.chmod(0o700 if path.is_dir() else mode)
            await sandbox.upload_dir(str(source), target, identity=identity)
            check = await sandbox.exec(
                [
                    "python.exe",
                    "-c",
                    "import pathlib, sys; p=pathlib.Path(sys.argv[1]); "
                    "assert (p/'assets/data.txt').read_text()=='input'; "
                    "assert (p/'.hidden').read_text()=='hidden'; "
                    "assert (p/'assets/empty').is_dir()",
                    target,
                ]
            )
            assert check.ok, check.stderr
            stage = await sandbox.exec(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    target + r"\setup.ps1",
                ],
                identity=identity,
            )
            assert stage.ok and stage.stdout.strip() == "stage-ok", stage.stderr
            # A limited agent can edit agent uploads, but cannot edit framework uploads.
            edit = (
                "import pathlib,sys; (pathlib.Path(sys.argv[1])/'assets/data.txt').write_text('x')"
            )
            write = await sandbox.exec(["python.exe", "-c", edit, target], identity=Identity.AGENT)
            assert write.ok == (identity is Identity.AGENT), write.stderr
            # Privileged setup/verification can always write, including agent-owned uploads.
            write = await sandbox.exec(["python.exe", "-c", edit, target])
            assert write.ok, write.stderr
