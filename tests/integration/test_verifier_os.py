"""A Windows solver verified in a Linux container, its own operating system."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers.qemu import QemuProvider
from ale.run.tasksets.manifest import load_task_folder
from tests.support import CONTAINER_BASE, provider_registry, write_task

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]


async def test_a_windows_solvers_relative_artifact_is_verified_on_linux(tmp_path: Path) -> None:
    image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to an ALE Windows base disk")
    task = write_task(
        tmp_path / "task",
        name="agentslastexam/linux-verifier",
        config={
            "artifacts": [r"output\result.txt"],
            "environment": {
                "os": "windows",
                "image": "local/linux-verifier",
                "cpus": 2,
                "memory_mb": 8192,
            },
            "agent": {"timeout_sec": 120, "workdir": "Documents"},
            "verifier": {"environment": {"os": "linux", "cpus": 1, "memory_mb": 512}},
        },
        dockerfile=None,
        instruction="Write the result.\n",
        solve=None,
    )
    (task / "solution").mkdir()
    (task / "solution/solve.ps1").write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$output = Join-Path $env:ALE_HOME 'Documents\\output'\n"
        "New-Item $output -ItemType Directory -Force | Out-Null\n"
        "Set-Content (Join-Path $output 'result.txt') 'from windows' -NoNewline\n"
    )
    # A headless Linux verifier, whose account's home is not the solver's.
    (task / "tests/Dockerfile").write_text(f"FROM {CONTAINER_BASE}\nENV DISPLAY=\n")
    (task / "tests/test.sh").write_text('exec "$ALE_VERIFY_PYTHON" verify.py\n')
    (task / "tests/verify.py").write_text(
        "import os, platform\n"
        "from pathlib import Path\n"
        "from ale_verify import write\n"
        "result = Path(os.environ['ALE_HOME']) / 'Documents/output/result.txt'\n"
        "v = {'linux': float(platform.system() == 'Linux')}\n"
        "v['content'] = float(result.read_text() == 'from windows')\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )

    result = await run_episode(
        load_task_folder(task),
        EpisodeProtocol(OracleHarness()),
        provider_registry(QemuProvider(image=image, overlay_dir=tmp_path / "qemu")),
        run_dir=tmp_path / "runs",
        collect_artifacts=True,
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    assert [outcome.provider for outcome in result.record.sandboxes] == ["qemu", "docker"]
    (entry,) = [
        entry
        for entry in json.loads((result.run_dir / "artifacts/snapshot.json").read_text())["entries"]
        if entry["kind"] == "file"
    ]
    assert entry["source"].lower() == r"c:\users\user\documents\output\result.txt"
    assert entry["below_start"] == "output/result.txt"
