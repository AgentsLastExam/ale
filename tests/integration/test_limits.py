"""Ceilings and deadlines end episodes; they do not hang them.

The failure this guards against is not "the limit was ignored" — it is an episode that
burns its whole budget and then stalls, holding a container and telling nobody why. So
each case asserts a *typed status*, promptly, with the partial evidence preserved.
"""

from __future__ import annotations

import asyncio
import textwrap
import time
from collections.abc import Callable
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

#: Well under any phase deadline these tests set, so a pass cannot come from slowness.
GRACE_SEC = 60


async def run_one(task_root: Path, run_dir: Path, harness: object):  # type: ignore[no-untyped-def]
    task = next(iter(ManifestTaskset(task_root).load()))
    return await run_episode(
        task,
        StandardEnvironment(harness),  # type: ignore[arg-type]
        DockerProvider(),
        run_dir=run_dir,
    )


def set_timeout(task_root: Path, phase: str, seconds: int) -> None:
    manifest = task_root / "task.yaml"
    text = manifest.read_text()
    line = next(line for line in text.splitlines() if line.startswith("timeouts:"))
    manifest.write_text(text.replace(line, line.replace(f"{phase}: 120", f"{phase}: {seconds}")))


@pytest.mark.asyncio
async def test_a_hanging_setup_times_out(tmp_path: Path, write_repo: Callable[..., Path]) -> None:
    """A task that never finishes preparing is a task defect, reported as a timeout."""
    task_root = write_repo(tmp_path / "repo")
    (task_root / "setup" / "run.sh").write_text("#!/usr/bin/env bash\nsleep 600\n")
    set_timeout(task_root, "setup", 5)

    started = time.monotonic()
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())
    elapsed = time.monotonic() - started

    assert result.verdict.status is Status.TIMEOUT
    assert result.verdict.primary_reward is None  # a timeout is not a zero
    assert elapsed < GRACE_SEC, "the deadline fired, but the episode did not end promptly"


@pytest.mark.asyncio
async def test_a_hanging_agent_times_out_and_keeps_its_trace(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """Partial evidence survives: an episode that ran out of time still explains itself."""
    task_root = write_repo(tmp_path / "repo")
    (task_root / "oracle" / "run.sh").write_text("#!/usr/bin/env bash\nsleep 600\n")
    set_timeout(task_root, "agent", 5)

    started = time.monotonic()
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())
    elapsed = time.monotonic() - started

    assert result.verdict.status is Status.TIMEOUT
    assert elapsed < GRACE_SEC

    records = list(read_records(result.run_dir / "trace.semantic.jsonl"))
    kinds = {record["kind"] for record in records}
    assert "instruction" in kinds, "the trace of a timed-out episode was lost"

    # The phase that ran out is the one whose duration matters most.
    (timing,) = [record for record in records if record["kind"] == "timing"]
    phases = {span["name"] for span in timing["phases"]}
    assert "agent" in phases


@pytest.mark.asyncio
async def test_a_hanging_verifier_times_out(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(tmp_path / "repo")
    (task_root / "verify" / "run.sh").write_text("#!/usr/bin/env bash\nsleep 600\n")
    set_timeout(task_root, "verify", 5)

    started = time.monotonic()
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())

    assert result.verdict.status is Status.TIMEOUT
    assert time.monotonic() - started < GRACE_SEC


@pytest.mark.asyncio
async def test_a_timed_out_episode_leaves_no_container_behind(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """Teardown is shielded, so the deadline reclaims the sandbox rather than leaking it."""
    task_root = write_repo(tmp_path / "repo")
    (task_root / "setup" / "run.sh").write_text("#!/usr/bin/env bash\nsleep 600\n")
    set_timeout(task_root, "setup", 5)

    result = await run_one(task_root, tmp_path / "runs", OracleHarness())
    assert result.verdict.status is Status.TIMEOUT

    proc = await asyncio.create_subprocess_exec(
        "docker",
        "ps",
        "--filter",
        f"label=ale.episode={result.episode_id}",
        "--format",
        "{{.Names}}",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    assert not stdout.decode().strip(), "a container outlived its episode"


@pytest.mark.asyncio
async def test_a_crashing_setup_is_a_task_error_not_a_zero(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """Distinguishing a broken task from a hard one is the point of the taxonomy."""
    task_root = write_repo(tmp_path / "repo")
    (task_root / "setup" / "run.sh").write_text(
        textwrap.dedent("""
            #!/usr/bin/env bash
            set -euo pipefail
            echo "the data this task needs is not where it expected" >&2
            exit 3
        """).strip()
    )

    result = await run_one(task_root, tmp_path / "runs", NopHarness())

    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.primary_reward is None
