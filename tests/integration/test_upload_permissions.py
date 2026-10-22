"""The same upload policy on a real container, Linux VM and Windows VM."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from ale.core.sandbox import Identity, ImageRef, SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources
from ale.run.providers.docker import DockerProvider
from ale.run.providers.qemu import QemuProvider
from tests.support import CONTAINER_BASE

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend",
    [
        "docker",
        pytest.param("linux-vm", marks=pytest.mark.needs_kvm),
        pytest.param("windows-vm", marks=pytest.mark.needs_kvm),
    ],
)
async def test_upload_permissions_ignore_host_modes(backend: str, tmp_path: Path) -> None:
    windows = backend == "windows-vm"
    if backend == "docker":
        provider = DockerProvider()
        image = ImageRef(kind="container", reference=CONTAINER_BASE)
    else:
        variable = "ALE_TEST_WINDOWS_IMAGE" if windows else "ALE_QEMU_IMAGE"
        disk = os.environ.get(variable)
        if not disk:
            pytest.skip(f"set {variable} to a prepared VM disk")
        provider = QemuProvider(image=Path(disk), overlay_dir=tmp_path / "overlays")
        image = ImageRef(kind="vm", reference="local/upload-permissions")

    source = tmp_path / "source"
    (source / "assets/empty").mkdir(parents=True)
    (source / "assets/data.txt").write_text("input")
    (source / ".hidden").write_text("hidden")
    entry = source / ("run.ps1" if windows else "run.sh")
    entry.write_text("Write-Output stage-ok" if windows else "printf stage-ok")
    user = os.environ.get("ALE_TEST_WINDOWS_USER", "user") if windows else "user"
    request = SandboxRequest(
        episode_id=f"upload-{uuid.uuid4().hex[:8]}",
        os="windows" if windows else "linux",
        user=user,
        sudo=False,
        prepared_image=await provider.prepare_image(image),
        resources=Resources(cpus=2, memory_mb=8192 if windows else 2048),
        network=NetworkPolicy(mode="open"),
    )
    async with await provider.create(request) as sandbox:
        target = r"C:\ProgramData\ALE\setup" if windows else "/tmp/ale-upload"
        python = "python.exe" if windows else "python3"
        # Re-upload over existing paths, changing both Host modes and destination ownership.
        for identity, mode in [(Identity.AGENT, 0o777), (Identity.FRAMEWORK, 0o600)]:
            for path in source.rglob("*"):
                path.chmod(0o700 if path.is_dir() else mode)
            await sandbox.upload_dir(str(source), target, identity=identity)
            check = await sandbox.exec(
                [
                    python,
                    "-c",
                    "import pathlib, sys; p=pathlib.Path(sys.argv[1]); "
                    "assert (p/'assets/data.txt').read_text()=='input'; "
                    "assert (p/'.hidden').read_text()=='hidden'; "
                    "assert (p/'assets/empty').is_dir()",
                    target,
                ]
            )
            assert check.ok, check.stderr
            if not windows:
                check = await sandbox.exec(
                    [
                        python,
                        "-c",
                        "import pathlib,pwd,stat,sys; p=pathlib.Path(sys.argv[1]); "
                        "u=pwd.getpwnam(sys.argv[2]); "
                        "assert all(stat.S_IMODE(x.stat().st_mode)=="
                        "(0o755 if x.is_dir() else 0o644) "
                        "and (x.stat().st_uid,x.stat().st_gid)==(u.pw_uid,u.pw_gid) "
                        "for x in [p,*p.rglob('*')])",
                        target,
                        user if identity is Identity.AGENT else "root",
                    ]
                )
                assert check.ok, check.stderr
            argv = (
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    target + r"\run.ps1",
                ]
                if windows
                else ["bash", target + "/run.sh"]
            )
            stage = await sandbox.exec(argv, identity=identity)
            assert stage.ok and stage.stdout.strip() == "stage-ok", stage.stderr
            # A limited agent can edit agent uploads, but cannot edit framework uploads.
            write = await sandbox.exec(
                [
                    python,
                    "-c",
                    "import pathlib,sys; "
                    "(pathlib.Path(sys.argv[1])/'assets/data.txt').write_text('edit')",
                    target,
                ],
                identity=Identity.AGENT,
            )
            assert write.ok == (identity is Identity.AGENT), write.stderr
            # Privileged setup/verification can always write, including agent-owned uploads.
            write = await sandbox.exec(
                [
                    python,
                    "-c",
                    "import pathlib,sys; "
                    "(pathlib.Path(sys.argv[1])/'assets/data.txt').write_text('root')",
                    target,
                ]
            )
            assert write.ok, write.stderr
