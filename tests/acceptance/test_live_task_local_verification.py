from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from tests.support import provider_registry

pytestmark = [pytest.mark.needs_docker]


@pytest.mark.asyncio
async def test_relocated_task_local_verifier_does_not_enter_solver_image(
    tmp_path: Path,
) -> None:
    source = scaffold_task(tmp_path / "source")
    secret = "verify-only-marker"
    (source / "verify" / "helper.py").write_text(f"EXPECTED = {secret!r}\n")
    (source / "verify" / "verify.py").write_text(
        "from pathlib import Path\n"
        "from ale_verify import CheckResult, Verification\n"
        "from helper import EXPECTED\n"
        "v = Verification()\n"
        "v.check('withheld', CheckResult(float(EXPECTED == 'verify-only-marker')))\n"
        "v.check('not_baked', CheckResult(float(not Path('/verify/helper.py').exists())))\n"
        "v.write()\n"
    )
    relocated = tmp_path / "elsewhere" / "task"
    relocated.parent.mkdir()
    shutil.copytree(source, relocated)
    result = await run_episode(
        load_tasks(relocated)[0],
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"withheld": 1.0, "not_baked": 1.0}


@pytest.mark.asyncio
async def test_external_reference_is_published_only_for_live_verification(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "ale-tasks-live"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    task_path = scaffold_task(repository / "tasks/reference")
    reference = task_path / "verify/assets/expected.txt"
    reference.parent.mkdir(parents=True)
    reference.write_text("expected")
    (task_path / "verify" / "verify.py").write_text(
        "from ale_verify import Verification, checks\n"
        "v = Verification()\n"
        "v.check('reference', checks.text_equals('assets/expected.txt', 'expected'))\n"
        "v.write()\n"
    )
    result = await run_episode(
        load_tasks(task_path)[0],
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reference": 1.0}
