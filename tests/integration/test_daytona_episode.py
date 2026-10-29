"""Whole episodes on Daytona, through the engine rather than the Provider alone.

Setup, the agent's stage, artifact collection and verification each take their own turn
with the sandbox, and each turn depends on something the conformance suite checks one piece
at a time: who runs, what is uploaded, and when the network is open. Creates billable
sandboxes, so it needs ``ALE_DAYTONA_API_KEY``.
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.lock import TaskSource
from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers.daytona import DaytonaProvider
from ale.run.tasksets.manifest import load_tasks
from tests.support import provider_registry

pytestmark = [
    pytest.mark.integration,
    pytest.mark.needs_docker,
    pytest.mark.needs_daytona,
    pytest.mark.skipif(
        not os.environ.get("ALE_DAYTONA_API_KEY"), reason="ALE_DAYTONA_API_KEY is not set"
    ),
]

REACH = (
    "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), 5)\" >/dev/null 2>&1"
)


async def run_one(task_root: Path, run_dir: Path):  # type: ignore[no-untyped-def]
    task = load_tasks(task_root)[0]
    settings = RunConfig()
    return await run_episode(
        task,
        EpisodeProtocol(OracleHarness()),
        provider_registry(DaytonaProvider()),
        run_dir=run_dir,
        collect_artifacts=True,
        provenance=ProvenanceInputs(
            source=TaskSource(kind="git", repo="https://e/x.git", commit="a" * 40, path="t"),
            agent=agent_provenance(OracleHarness(), "none"),
            gateway=gateway_provenance(settings),
            config_hash=settings.config_hash,
        ),
    )


def whole_gib(task_root: Path) -> None:
    """Daytona allocates whole GiB, and refuses a Task that asks for anything else."""
    manifest = task_root / "task.yaml"
    manifest.write_text(manifest.read_text().replace("memory_mb: 512", "memory_mb: 1024"))


def collected(result, name: str) -> str:  # type: ignore[no-untyped-def]
    return (result.run_dir / "artifacts" / "0000" / "output" / name).read_text().strip()


@pytest.mark.asyncio
async def test_the_oracle_solves_the_task_on_daytona(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(tmp_path / "repo")
    whole_gib(task_root)
    result = await run_one(task_root, tmp_path / "runs")

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"overall": 1.0}
    assert result.lock is not None and result.lock.resources is not None
    assert result.lock.resources.effective.provider == "daytona"
    observed = result.lock.image.observed_ref
    assert "@sha256:" in observed and observed.split("/")[0] == "cr.app.daytona.io"


@pytest.mark.asyncio
async def test_the_network_is_open_for_setup_and_closed_for_the_agent(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(tmp_path / "repo")
    whole_gib(task_root)
    (task_root / "setup").mkdir()
    (task_root / "setup" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\nmkdir -p /home/user/output\n"
        f"if {REACH}; then echo reachable > /home/user/output/setup_net\n"
        "else echo blocked > /home/user/output/setup_net; fi\n"
        "chown -R user:user /home/user/output\n"
    )
    (task_root / "oracle" / "run.sh").write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -uo pipefail
        if {REACH}; then echo reachable > /home/user/output/agent_net
        else echo blocked > /home/user/output/agent_net; fi
        word=$(cat /home/user/input/word.txt)
        printf 'hello %s' "$word" > /home/user/output/result.txt
        """)
    )
    result = await run_one(task_root, tmp_path / "runs")

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert collected(result, "setup_net") == "reachable"
    assert collected(result, "agent_net") == "blocked"


@pytest.mark.asyncio
async def test_an_allowlisted_task_reaches_its_host_directly_and_nothing_else(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(
        tmp_path / "repo",
        manifest_suffix="network: {mode: allowlist, allowed_hosts: [example.com]}",
    )
    whole_gib(task_root)
    oracle = task_root / "oracle" / "run.sh"
    oracle.write_text(
        oracle.read_text()
        + textwrap.dedent("""
        for site in example.com github.com; do
            if curl -sf --max-time 10 -o /dev/null "https://$site"; then state=reachable
            else state=blocked; fi
            echo "$state" > "/home/user/output/$site"
        done
        """)
    )
    result = await run_one(task_root, tmp_path / "runs")

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert collected(result, "example.com") == "reachable"
    assert collected(result, "github.com") == "blocked"


@pytest.mark.asyncio
@pytest.mark.needs_gpu
@pytest.mark.skipif(
    os.environ.get("ALE_TEST_DAYTONA_GPU") != "1",
    reason="set ALE_TEST_DAYTONA_GPU=1 to run an episode on a Daytona GPU sandbox",
)
async def test_a_gpu_task_runs_on_the_gpu_its_lock_records(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(tmp_path / "repo")
    manifest = task_root / "task.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "resources: {cpus: 1, memory_mb: 512}", "resources: {cpus: 2, memory_mb: 8192, gpus: 1}"
        )
    )
    oracle = task_root / "oracle" / "run.sh"
    oracle.write_text(
        oracle.read_text()
        + "\nnvidia-smi --query-gpu=uuid --format=csv,noheader > /home/user/output/gpu.txt\n"
    )
    result = await run_one(task_root, tmp_path / "runs")

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"overall": 1.0}
    assert result.lock is not None and result.lock.resources is not None
    gpu = result.lock.resources.effective.gpu
    assert gpu is not None and gpu.requested_count == 1
    (device,) = gpu.observed_devices
    assert "H100" in device.model
    assert collected(result, "gpu.txt") == device.id


@pytest.mark.asyncio
async def test_a_task_that_asks_for_a_fraction_of_a_gib_is_refused_not_rounded(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    result = await run_one(write_repo(tmp_path / "repo"), tmp_path / "runs")  # asks for 512 MB

    assert result.verdict.status is Status.ENV_ERROR
    assert result.verdict.failure is not None
    assert "whole number of GiB" in result.verdict.failure.message
