from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.environment import Phase
from ale.core.lock import TaskSource
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.provenance import (
    ProvenanceInputs,
    agent_provenance,
    gateway_provenance,
)
from ale.run.providers import ProviderRegistry
from ale.run.tasksets import load_tasks
from tests.support import sibling_checkout

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]

TASK = sibling_checkout("ale-tasks-base") / "tasks/demo/docker_in_vm"


@pytest.mark.asyncio
async def test_task_owned_docker_runs_a_nested_offline_container(tmp_path: Path) -> None:
    settings = RunConfig()
    phases: list[Phase] = []
    result = await run_episode(
        load_tasks(TASK)[0],
        StandardEnvironment(OracleHarness()),
        ProviderRegistry(settings),
        run_dir=tmp_path,
        phase_callback=phases.append,
        provenance=ProvenanceInputs(
            source=TaskSource(
                kind="registry",
                repo="https://github.com/agents-last-exam/ale-tasks-base.git",
                commit="a" * 40,
                path="tasks/demo/docker_in_vm",
            ),
            agent=agent_provenance(OracleHarness(), ""),
            gateway=gateway_provenance(settings),
            config_hash=settings.config_hash,
        ),
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"nested_container": 1.0}
    assert (
        result.run_dir / "artifacts/nested-container.txt"
    ).read_text() == "nested-container-ok\n"
    assert phases == [
        Phase.PROVISION,
        Phase.SETUP,
        Phase.AGENT,
        Phase.VERIFY,
        Phase.TEARDOWN,
    ]
    assert result.lock is not None
    assert result.lock.framework.schema_version == 2
    assert result.lock.resources is not None
    assert result.lock.resources.effective.storage_mb is not None
    assert result.lock.resources.effective.storage_mb >= 8192
    image = result.lock.image
    assert image.declaration.kind == "vm"
    assert image.source == "local"
    assert image.provider == "qemu"
    assert image.observed_identity == image.prepared_identity
    assert image.oci_identity and image.materializer_identity
    assert "content_digest" not in image.model_dump()
