from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import ManifestTaskset

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"


def repo_with_flat_kit(root: Path) -> Path:
    task = root / "tasks" / "usekit"
    for sub in ("setup", "verify", "oracle"):
        (task / sub).mkdir(parents=True)
    (root / "domain.yaml").write_text("name: demo\nrequires_core: '>=0.1,<0.2'\n")
    kit = root / "kits" / "grader_protocol"
    kit.mkdir(parents=True)
    (kit / "__init__.py").write_text(
        "from ale_verify import CheckResult\n"
        "def score(v): return CheckResult(float(v == 'hello'))\n"
    )
    (task / "task.yaml").write_text(
        textwrap.dedent(f"""
        image: {IMAGE}
        resources: {{ cpus: 1, memory_mb: 512 }}
        timeouts: {{ setup: 60, agent: 60, verify: 60 }}
        artifacts: [/home/user/output]
        verify:
          kits: [grader_protocol]
        """).strip()
    )
    (task / "instruction.md").write_text("Write hello to /home/user/output/r.txt\n")
    (task / "setup" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /home/user/output\n"
    )
    (task / "oracle" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf hello > /home/user/output/r.txt\n"
    )
    (task / "verify" / "run.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nexec python3 "$(dirname "$0")/check.py"\n'
    )
    (task / "verify" / "check.py").write_text(
        "from ale_verify import Verification\n"
        "from grader_protocol import score\n"
        "v = Verification()\n"
        "v.check('reward', score(open('/home/user/output/r.txt').read().strip()))\n"
        "v.write()\n"
    )
    for entry in task.glob("*/run.sh"):
        entry.chmod(0o755)
    return task


@pytest.mark.asyncio
async def test_flat_kit_is_importable_without_pythonpath_or_lock(tmp_path: Path) -> None:
    task_root = repo_with_flat_kit(tmp_path / "repo")
    task = next(iter(ManifestTaskset(task_root).load()))
    result = await run_episode(
        task, StandardEnvironment(OracleHarness()), DockerProvider(), run_dir=tmp_path / "runs"
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    assert not (task_root.parents[1] / "kits.lock.yaml").exists()
