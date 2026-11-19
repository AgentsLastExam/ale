"""Harbor's task.toml fields, end to end in Docker Sandboxes."""

from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import load_tasks
from tests.support import CONTAINER_BASE, provider_registry, write_task

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

WORK = "/home/user/work"
DOCKERFILE = f"FROM {CONTAINER_BASE}\nRUN install -d -o user -g user {WORK}\n"
#: A Harbor verifier: absolute paths, the working directory it was given, and reward.txt.
HARBOR_TEST = f"""#!/usr/bin/env bash
set -eu
if [ "$(id -un)" = user ] && [ "$PWD" = {WORK} ] && [ "$(cat out.txt)" = done ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo "as $(id -un) in $PWD" >&2
  echo 0 > /logs/verifier/reward.txt
fi
"""


async def _execute(task: Path, run_dir: Path):  # type: ignore[no-untyped-def]
    return await run_episode(
        load_tasks(task)[0],
        EpisodeProtocol(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=run_dir,
        collect_artifacts=True,
    )


def _harbor_task(root: Path, healthcheck: str) -> Path:
    task = write_task(
        root,
        config={
            "environment": {
                "workdir": WORK,
                "healthcheck": {"command": healthcheck, "interval_sec": 0, "retries": 2},
            },
            "verifier": {"user": "user"},
            "artifacts": [{"source": f"{WORK}/out.txt", "destination": "kept/out.txt"}],
        },
        dockerfile=DOCKERFILE,
        test=HARBOR_TEST,
        solve=f"#!/usr/bin/env bash\nset -eu\necho done > {WORK}/out.txt\n",
    )
    setup = task / "environment/setup/setup.sh"
    setup.parent.mkdir(parents=True)
    setup.write_text(f"#!/usr/bin/env bash\nset -eu\ntouch {WORK}/ready\n")
    return task


@pytest.mark.asyncio
async def test_a_harbor_task_runs_with_its_workdir_user_healthcheck_and_reward_txt(
    tmp_path: Path,
) -> None:
    task = _harbor_task(tmp_path / "harbor", healthcheck=f"test -f {WORK}/ready")
    result = await _execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    assert (result.run_dir / "artifacts/kept/out.txt").read_text() == "done\n"


@pytest.mark.asyncio
async def test_a_service_that_never_becomes_healthy_is_the_task_s_error(tmp_path: Path) -> None:
    task = _harbor_task(tmp_path / "unhealthy", healthcheck=f"test -f {WORK}/never")
    result = await _execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.failure is not None
    assert "healthcheck failed after 2 consecutive retries" in str(result.verdict.failure)
