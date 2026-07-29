"""Several episodes on one host at the same time.

Concurrency is where shared state shows itself. Everything an episode touches is supposed
to be its own — its sandbox, its run directory, its ledger row, the port its guest service
is reached on — and the way to find out is to run four at once and require every one of
them to be right, not merely to finish.

A sequential suite would pass with a name collision, a reused port or a directory written
by two episodes at once. This is the test those would fail.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.harness import HarnessSession
from ale.core.sandbox import SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import ManifestTaskset

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

#: Our own base image, which satisfies the sandbox contract; see tests/integration/conftest.
IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"

EPISODES = 4


@pytest.mark.asyncio
async def test_four_episodes_at_once_each_score_and_stay_separate(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(tmp_path / "repo")
    task = next(iter(ManifestTaskset(task_root).load()))
    provider = DockerProvider()

    async def one(index: int):  # type: ignore[no-untyped-def]
        return await run_episode(
            task,
            StandardEnvironment(OracleHarness()),
            provider,
            run_dir=tmp_path / "runs",
        )

    results = await asyncio.gather(*(one(i) for i in range(EPISODES)))

    for result in results:
        assert result.verdict.status is Status.COMPLETED, result.verdict.failure
        assert result.verdict.rewards == {"reward": 1.0}

    # Distinct identities and distinct directories: two episodes writing to one path would
    # still score, and the second would be reporting the first one's work.
    assert len({result.episode_id for result in results}) == EPISODES
    assert len({result.run_dir for result in results}) == EPISODES
    for result in results:
        # Each wrote its own trace; a shared directory would leave three of these missing
        # or hold one file with four episodes interleaved in it.
        assert (result.run_dir / "trajectory.json").is_file()
        assert (result.run_dir / "trace.execution.jsonl").is_file()


@pytest.mark.asyncio
async def test_each_sandbox_gets_the_resources_its_task_declared() -> None:
    """Four at once must not mean four sharing one task's allowance.

    Read back from the sandbox rather than from the request that created it: a limit the
    engine passed and the runtime ignored would otherwise look identical to one that took.
    """
    provider = DockerProvider()
    request = SandboxRequest(
        episode_id="limits",
        image_ref=IMAGE,
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
    )

    async def one(index: int) -> str:
        sandbox = await provider.create(request.model_copy(update={"episode_id": f"lim{index}"}))
        try:
            result = await sandbox.exec(["cat", "/sys/fs/cgroup/memory.max"], timeout_sec=30)
            return result.stdout.strip()
        finally:
            await sandbox.destroy()

    seen = await asyncio.gather(*(one(i) for i in range(EPISODES)))
    assert all(value == str(512 * 1024 * 1024) for value in seen), seen


def test_one_claude_definition_has_no_episode_mutable_paths_or_versions() -> None:
    harness = ClaudeCodeHarness()
    base = HarnessSession(
        episode_id="base",
        gateway_url="http://gateway",
        token="token",
        model="model",
    )
    sessions = [
        harness._env(
            base.model_copy(update={"episode_id": str(index), "home": f"/home/agent-{index}"})
        )
        for index in range(20)
    ]
    assert len({env["CLAUDE_CONFIG_DIR"] for env in sessions}) == 20
    assert len({env["PATH"] for env in sessions}) == 20
    assert not hasattr(harness, "_resolved_version")
    assert not hasattr(harness, "_prefix")
    assert not hasattr(harness, "_path")
