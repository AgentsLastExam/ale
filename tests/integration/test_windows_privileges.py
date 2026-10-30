"""The same elevated framework contract in shared and separate Windows verification."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ale.core.taskspec import ImageKind
from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers.qemu import QemuProvider
from ale.run.tasksets.manifest import load_task_folder
from tests.support import provider_registry, write_task

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]


@pytest.mark.parametrize("mode", ["shared", "separate"])
async def test_windows_framework_copies_protected_artifacts_and_verifies_as_admin(
    tmp_path: Path, mode: str
) -> None:
    image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to an ALE Windows base disk")
    task = write_task(
        tmp_path / "task",
        name="agentslastexam/windows-privileges",
        config={
            "artifacts": [r"C:\ale-copy\*.sgf", r"C:\ale-copy\directory.json", r"C:\missing"],
            "environment": {
                "os": "windows",
                "vm_image": "local/windows-privileges",
                "cpus": 2,
                "memory_mb": 8192,
            },
            "agent": {"timeout_sec": 60, "workdir": r"C:\Users\user"},
            "verifier": {"environment_mode": mode},
        },
        dockerfile=None,
        instruction="Leave the prepared outputs for verification.\n",
        test=None,
        solve=None,
    )
    for stage in ("environment/setup", "solution", "tests"):
        (task / stage).mkdir(parents=True, exist_ok=True)
    (task / "environment/setup/setup.ps1").write_text(
        r"""$ErrorActionPreference = 'Stop'
python.exe -c "import ctypes; assert ctypes.windll.shell32.IsUserAnAdmin()"
if ($LASTEXITCODE) { exit $LASTEXITCODE }
New-Item C:\ale-copy\directory.json -ItemType Directory -Force | Out-Null
Set-Content C:\ale-copy\game.sgf 'protected' -NoNewline
& icacls.exe C:\ale-copy\game.sgf /inheritance:r /grant:r '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' |
    Out-Null
if ($LASTEXITCODE) { exit $LASTEXITCODE }
New-Item C:\ale-copy\directory.json\loop -ItemType SymbolicLink `
    -Target C:\ale-copy\directory.json | Out-Null
"""
    )
    (task / "solution/solve.ps1").write_text(
        'python.exe -c "import ctypes; assert not ctypes.windll.shell32.IsUserAnAdmin()"\n'
        "if ($LASTEXITCODE) { exit $LASTEXITCODE }\n"
        "python.exe .\\check.py\nexit $LASTEXITCODE\n"
    )
    (task / "solution/check.py").write_text(
        "from pathlib import Path\n"
        "try: Path(r'C:\\ale-copy\\game.sgf').read_text()\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('agent could read Administrator-only output')\n"
    )
    (task / "tests/test.ps1").write_text("python.exe .\\verify.py\nexit $LASTEXITCODE\n")
    (task / "tests/verify.py").write_text(
        "import ctypes\n"
        "from pathlib import Path\n"
        "from ale_verify import write\n"
        "assert ctypes.windll.shell32.IsUserAnAdmin()\n"
        "v = {}\n"
        "content = Path('C:\\\\ale-copy\\\\game.sgf').read_text()\n"
        "v['protected'] = float(content == 'protected')\n"
        "v['directory'] = float(Path('C:\\\\ale-copy\\\\directory.json').is_dir())\n"
        "v['missing'] = float(not Path('C:\\\\missing').exists())\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    result = await run_episode(
        load_task_folder(task),
        EpisodeProtocol(OracleHarness()),
        provider_registry(
            QemuProvider(image=image, overlay_dir=tmp_path / "qemu"), kind=ImageKind.VM
        ),
        run_dir=tmp_path / "runs",
        collect_artifacts=True,
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    assert (result.run_dir / "artifacts/0000/game.sgf").read_text() == "protected"
