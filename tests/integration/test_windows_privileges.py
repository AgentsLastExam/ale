"""The same elevated framework contract in shared and separate Windows verification."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from ale.core.taskspec import ImageKind
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.qemu import QemuProvider
from ale.run.tasksets.manifest import load_task_folder
from tests.support import provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]


@pytest.mark.parametrize("mode", ["shared", "separate"])
async def test_windows_framework_copies_protected_artifacts_and_verifies_as_admin(
    tmp_path: Path, mode: str
) -> None:
    image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to an ALE Windows base disk")
    task = tmp_path / "task"
    for stage in ("setup", "oracle", "verify"):
        (task / stage).mkdir(parents=True)
    manifest = {
        "spec_type": "core/v1",
        "name": "windows-privileges",
        "os": "windows",
        "image": {"kind": "vm", "ref": "local/windows-privileges"},
        "resources": {"cpus": 2, "memory_mb": 8192},
        "artifacts": [r"C:\ale-copy\*.sgf", r"C:\ale-copy\directory.json", r"C:\missing"],
        "timeouts": {"setup": 120, "agent": 60, "verify": 120},
    }
    if mode == "separate":
        manifest["verify"] = {
            "environment_mode": mode,
            "resources": {"cpus": 2, "memory_mb": 8192, "storage_mb": None, "gpus": 0},
        }
    (task / "task.yaml").write_text(yaml.safe_dump(manifest))
    (task / "instruction.md").write_text("Leave the prepared outputs for verification.\n")
    (task / "setup/run.ps1").write_text(
        r"""$ErrorActionPreference = 'Stop'
python.exe -c "import ctypes; assert ctypes.windll.shell32.IsUserAnAdmin()"
if ($LASTEXITCODE) { exit $LASTEXITCODE }
New-Item C:\ale-copy\directory.json, C:\ale-private -ItemType Directory -Force | Out-Null
Set-Content C:\ale-private\value 'protected' -NoNewline
& icacls.exe C:\ale-private /inheritance:r /grant:r `
    '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' | Out-Null
if ($LASTEXITCODE) { exit $LASTEXITCODE }
New-Item C:\ale-copy\game.sgf -ItemType SymbolicLink -Target C:\ale-private\value | Out-Null
New-Item C:\ale-copy\directory.json\loop -ItemType SymbolicLink `
    -Target C:\ale-copy\directory.json | Out-Null
"""
    )
    (task / "oracle/run.ps1").write_text(
        'python.exe -c "import ctypes; assert not ctypes.windll.shell32.IsUserAnAdmin()"\n'
        "if ($LASTEXITCODE) { exit $LASTEXITCODE }\n"
        "python.exe .\\check.py\nexit $LASTEXITCODE\n"
    )
    (task / "oracle/check.py").write_text(
        "from pathlib import Path\n"
        "try: Path(r'C:\\ale-private\\value').read_text()\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('agent could read Administrator-only output')\n"
    )
    (task / "verify/run.ps1").write_text("python.exe .\\verify.py\nexit $LASTEXITCODE\n")
    (task / "verify/verify.py").write_text(
        "import ctypes\n"
        "from pathlib import Path\n"
        "from ale_verify import write\n"
        "assert ctypes.windll.shell32.IsUserAnAdmin()\n"
        "v = {}\n"
        "content = Path('C:\\\\ale-copy\\\\game.sgf').read_text()\n"
        "v['protected'] = float(content == 'protected')\n"
        "v['directory'] = float(Path('C:\\\\ale-copy\\\\directory.json').is_dir())\n"
        "v['missing'] = float(not Path('C:\\\\missing').exists())\n"
        "v['overall'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    result = await run_episode(
        load_task_folder(task),
        StandardEnvironment(OracleHarness()),
        provider_registry(
            QemuProvider(image=image, overlay_dir=tmp_path / "qemu"), kind=ImageKind.VM
        ),
        run_dir=tmp_path / "runs",
        collect_artifacts=True,
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"overall": 1.0}
    assert (result.run_dir / "artifacts/0000/game.sgf").read_text() == "protected"
