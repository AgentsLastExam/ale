"""The standalone Task authoring loop."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import NopHarness, OracleHarness
from ale.run.lint import lint_repository
from ale.run.providers.docker import DockerProvider
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from tests.support import provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


async def run(task_path: Path, run_dir: Path, harness: object):  # type: ignore[no-untyped-def]
    return await run_episode(
        load_tasks(task_path)[0],
        StandardEnvironment(harness),  # type: ignore[arg-type]
        provider_registry(DockerProvider()),
        run_dir=run_dir,
    )


@pytest.mark.asyncio
async def test_scaffold_lints_validates_and_survives_relocation(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "collection" / "first")
    assert lint_repository(task) == []

    original = load_tasks(task)[0]
    moved = tmp_path / "unrelated" / "first"
    moved.parent.mkdir()
    shutil.copytree(task, moved)
    relocated = load_tasks(moved)[0]
    assert relocated.spec == original.spec
    assert relocated.task_digest == original.task_digest

    result = await run(moved, tmp_path / "runs", OracleHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"content": 1.0, "overall": 1.0}
    assert json.loads((result.run_dir / "result.json").read_text())["rewards"] == {
        "content": 1.0,
        "overall": 1.0,
    }


@pytest.mark.asyncio
async def test_idle_agent_is_a_real_zero(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "task")
    result = await run(task, tmp_path / "runs", NopHarness())
    assert result.verdict.status is Status.COMPLETED
    assert result.verdict.rewards == {"content": 0.0, "overall": 0.0}


@pytest.mark.asyncio
async def test_dynamic_setup_runs_before_oracle(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "dynamic")
    setup = task / "setup"
    setup.mkdir()
    entry = setup / "run.sh"
    entry.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'test "$(id -u)" = 0\n'
        "printf ready > /home/user/output/service-ready\n"
        "chown user:user /home/user/output/service-ready\n"
    )
    entry.chmod(0o755)
    check = task / "verify" / "verify.py"
    check.write_text(
        "from ale_verify import Verification, checks\n"
        "v = Verification()\n"
        "v.check('setup', checks.text_equals('/home/user/output/service-ready', 'ready'))\n"
        "v.check('content', checks.text_equals('/home/user/output/result.txt', 'hello\\n'))\n"
        "v.aggregate('overall')\n"
        "v.write()\n"
    )
    result = await run(task, tmp_path / "runs", OracleHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"setup": 1.0, "content": 1.0, "overall": 1.0}
