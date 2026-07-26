"""One task folder, one container, one verdict.

This is the vertical slice: a real task repository on disk, a real container, the task's
own setup and verify stages, and a scored result. It runs the oracle harness, so it needs
no model and no gateway — everything else is the production path.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ale.core.trace import read_records
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import NopHarness, OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import ManifestTaskset

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

IMAGE = "docker.io/library/python:3.12-slim"


def write_repo(root: Path, *, with_oracle: bool = True, verify_body: str | None = None) -> Path:
    """Lay out a minimal task repository: manifest, instruction, setup, verify, oracle."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "domain.yaml").write_text("name: demo\nrequires_core: '>=0.1,<0.2'\n")
    task = root / "tasks" / "hello"
    (task / "setup").mkdir(parents=True)
    (task / "verify").mkdir()

    (task / "task.yaml").write_text(
        textwrap.dedent(f"""
        image: {IMAGE}
        resources: {{ cpus: 1, memory_mb: 512 }}
        timeouts: {{ setup: 120, agent: 120, verify: 120 }}
        artifacts: [{{ path: /ale/output }}]
        params: {{ greeting: hello }}
        validate: {{ min_reward: 1.0 }}
        """).strip()
    )
    (task / "instruction.md").write_text(
        "Write ${greeting} followed by the word in /ale/input/word.txt "
        "into /ale/output/result.txt\n"
    )
    (task / "setup" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /ale/input /ale/output\n"
        "printf 'world' > /ale/input/word.txt\n"
    )
    (task / "verify" / "run.sh").write_text(
        verify_body
        or textwrap.dedent("""
            #!/usr/bin/env bash
            set -euo pipefail
            expected="hello world"
            actual="$(cat /ale/output/result.txt 2>/dev/null || true)"
            if [ "$actual" = "$expected" ]; then
                printf '{"rewards": {"reward": 1.0}}' > "$ALE_VERDICT_PATH"
            else
                printf '{"rewards": {"reward": 0.0}}' > "$ALE_VERDICT_PATH"
            fi
        """).strip()
    )
    if with_oracle:
        (task / "oracle").mkdir()
        (task / "oracle" / "run.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            "printf 'hello %s' \"$(cat /ale/input/word.txt)\" > /ale/output/result.txt\n"
        )
    return task


async def run_one(task_root: Path, run_dir: Path, harness: object):  # type: ignore[no-untyped-def]
    task = next(iter(ManifestTaskset(task_root).load()))
    return await run_episode(
        task,
        StandardEnvironment(harness),  # type: ignore[arg-type]
        DockerProvider(),
        run_dir=run_dir,
    )


@pytest.mark.asyncio
async def test_oracle_solves_the_task(tmp_path: Path) -> None:
    """The admission gate: a task whose own solution scores full marks."""
    task_root = write_repo(tmp_path / "repo")
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.primary_reward == 1.0


@pytest.mark.asyncio
async def test_idle_agent_scores_zero_but_completes(tmp_path: Path) -> None:
    """Doing nothing is a legitimate zero, not an error."""
    task_root = write_repo(tmp_path / "repo")
    result = await run_one(task_root, tmp_path / "runs", NopHarness())

    assert result.verdict.status is Status.COMPLETED
    assert result.verdict.primary_reward == 0.0


@pytest.mark.asyncio
async def test_broken_verifier_is_a_task_error_not_a_zero(tmp_path: Path) -> None:
    """A verifier that writes nothing is a defect in the task, and must say so."""
    task_root = write_repo(
        tmp_path / "repo",
        verify_body="#!/usr/bin/env bash\nexit 0\n",  # exits cleanly, writes no rewards
    )
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())

    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.primary_reward is None


@pytest.mark.asyncio
async def test_agent_never_sees_verification_material(tmp_path: Path) -> None:
    """The reason answers stay on the host until scoring."""
    task_root = write_repo(tmp_path / "repo")
    (task_root / "verify" / "answer.txt").write_text("hello world")

    probe = task_root / "setup" / "run.sh"
    probe.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /ale/input /ale/output\n"
        "printf 'world' > /ale/input/word.txt\n"
        "for p in /ale/verify /ale/oracle /ale/reference; do\n"
        '  if [ -e "$p" ]; then echo "LEAK: $p" >&2; exit 17; fi\n'
        "done\n"
    )
    result = await run_one(task_root, tmp_path / "runs", NopHarness())

    # Setup runs before the agent; if scoring material were staged, it would exit 17.
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure


@pytest.mark.asyncio
async def test_traces_and_artifacts_are_written(tmp_path: Path) -> None:
    task_root = write_repo(tmp_path / "repo")
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())

    semantic = result.run_dir / "trace.semantic.jsonl"
    assert semantic.is_file()
    kinds = {record["kind"] for record in read_records(semantic)}
    assert {"instruction", "exec", "verifier"} <= kinds
    assert (result.run_dir / "artifacts" / "output" / "result.txt").is_file()
