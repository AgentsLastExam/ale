"""Real Docker coverage for ALE's supported Harbor single-step protocol."""

from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.harbor import HarborEnvironment, HarborProviderRegistry
from ale.run.harnesses.builtin import NopHarness, OracleHarness
from ale.run.tasksets import load_tasks

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


def _write_task(root: Path, *, separate: bool) -> Path:
    task = root / "task"
    (task / "environment").mkdir(parents=True)
    (task / "solution").mkdir()
    (task / "tests").mkdir()
    (task / "environment" / "Dockerfile").write_text(
        "FROM alpine:3.22\n"
        "RUN apk add --no-cache bash\n"
        "WORKDIR /workspace\n"
        "RUN printf expected > input.txt\n"
    )
    (task / "instruction.md").write_text("Write the expected result.\n")
    (task / "solution" / "solve.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\ncat input.txt > result.txt\n"
    )
    (task / "tests" / "test.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "reward=0\n"
        'if test "$(cat result.txt 2>/dev/null || true)" = expected; then reward=1; fi\n'
        "printf '%s\\n' \"$reward\" > /logs/verifier/reward.txt\n"
    )
    mode = "separate" if separate else "shared"
    artifacts = 'artifacts = ["/workspace/result.txt"]\n' if separate else ""
    (task / "task.toml").write_text(
        f'version = "1.0"\n{artifacts}\n'
        f'[verifier]\nenvironment_mode = "{mode}"\ntimeout_sec = 60\n\n'
        "[agent]\ntimeout_sec = 60\n\n"
        "[environment]\ncpus = 1\nmemory_mb = 256\n"
        'network_mode = "no-network"\n'
    )
    return task


async def _run(task_root: Path, run_dir: Path, harness: object):  # type: ignore[no-untyped-def]
    task = load_tasks(task_root)[0]
    return await run_episode(
        task,
        HarborEnvironment(harness),  # type: ignore[arg-type]
        HarborProviderRegistry(RunConfig()),
        run_dir=run_dir,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("separate", [False, True], ids=["shared", "separate"])
async def test_harbor_oracle_and_verifier_use_image_workdir(tmp_path: Path, separate: bool) -> None:
    task_root = _write_task(tmp_path, separate=separate)

    untouched = await _run(task_root, tmp_path / "nop", NopHarness())
    oracle = await _run(task_root, tmp_path / "oracle", OracleHarness())

    assert untouched.verdict.status is Status.COMPLETED, untouched.verdict.failure
    assert untouched.verdict.rewards == {"reward": 0.0}
    assert oracle.verdict.status is Status.COMPLETED, oracle.verdict.failure
    assert oracle.verdict.rewards == {"reward": 1.0}
