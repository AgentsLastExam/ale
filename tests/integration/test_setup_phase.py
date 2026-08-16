from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.scaffold import scaffold_task
from ale.run.tasksets import load_tasks
from tests.support import provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


async def execute(task_path: Path, run_dir: Path):  # type: ignore[no-untyped-def]
    return await run_episode(
        load_tasks(task_path)[0],
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=run_dir,
    )


@pytest.mark.asyncio
async def test_setup_is_optional_and_artifact_paths_are_prepared(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "plain")
    assert not (task / "setup").exists()
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure


@pytest.mark.asyncio
async def test_setup_runs_relative_entry_as_root(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "root-setup")
    setup = task / "setup"
    setup.mkdir()
    entry = setup / "run.sh"
    entry.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'test "$PWD" = "$ALE_STAGE_DIR"\n'
        "id -u > /home/user/output/setup-uid\n"
        "chown user:user /home/user/output/setup-uid\n"
    )
    entry.chmod(0o755)
    check = task / "verify" / "verify.py"
    check.write_text(
        "from ale_verify import Verification, checks\n"
        "v = Verification()\n"
        "v.check('root', checks.text_equals('/home/user/output/setup-uid', '0\\n'))\n"
        "v.write()\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.rewards == {"root": 1.0}


@pytest.mark.asyncio
async def test_nonzero_setup_stops_before_agent(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "broken")
    setup = task / "setup"
    setup.mkdir()
    entry = setup / "run.sh"
    entry.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 23\n")
    entry.chmod(0o755)
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.rewards is None
    assert "setup failed with exit code 23" in result.verdict.failure.message


@pytest.mark.asyncio
async def test_setup_timeout_is_explicit(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "timeout")
    manifest = task / "task.yaml"
    manifest.write_text(manifest.read_text().replace("setup: 60", "setup: 0.1"))
    setup = task / "setup"
    setup.mkdir()
    entry = setup / "run.sh"
    entry.write_text("#!/usr/bin/env bash\nsleep 10\n")
    entry.chmod(0o755)
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.TIMEOUT
    assert result.verdict.rewards is None
    assert "timeout" in result.verdict.failure.message


@pytest.mark.asyncio
async def test_setup_assets_are_uploaded_with_the_complete_stage(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "ale-tasks-demo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    task = scaffold_task(repository / "tasks/setup-assets")
    setup = task / "setup"
    setup.mkdir()
    (setup / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'test "$(id -u)" = 0\n'
        'test "$(cat assets/value.txt)" = dynamic\n'
        "printf ready > /home/user/output/setup-ready\n"
        "chown user:user /home/user/output/setup-ready\n"
    )
    (setup / "run.sh").chmod(0o755)
    source = task / "setup/assets/value.txt"
    source.parent.mkdir(parents=True)
    source.write_text("dynamic")
    (task / "verify" / "verify.py").write_text(
        "from ale_verify import Verification, checks\n"
        "v = Verification()\n"
        "v.check('setup', checks.text_equals('/home/user/output/setup-ready', 'ready'))\n"
        "v.write()\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"setup": 1.0}
