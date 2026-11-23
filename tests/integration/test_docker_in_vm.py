from __future__ import annotations

import json
from pathlib import Path

import pytest

from orbit.core.config import RunConfig
from orbit.core.episode import Phase
from orbit.core.lock import TaskSource
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.harnesses.builtin import OracleHarness
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.provenance import (
    ProvenanceInputs,
    agent_provenance,
    gateway_provenance,
)
from orbit.run.providers import ProviderRegistry
from orbit.run.tasksets.manifest import load_tasks
from tests.support import sibling_checkout

TASK = sibling_checkout("ale-tasks-base") / "tasks/demo/docker_in_vm"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.needs_docker,
    pytest.mark.needs_kvm,
    pytest.mark.skipif(not TASK.is_dir(), reason=f"no ale-tasks-base checkout at {TASK}"),
]


@pytest.mark.asyncio
async def test_task_owned_docker_runs_a_nested_offline_container(tmp_path: Path) -> None:
    settings = RunConfig()
    phases: list[Phase] = []
    result = await run_episode(
        load_tasks(TASK)[0],
        EpisodeProtocol(OracleHarness()),
        ProviderRegistry(settings),
        run_dir=tmp_path,
        phase_callback=phases.append,
        provenance=ProvenanceInputs(
            source=TaskSource(
                kind="git",
                repo="https://github.com/agents-last-exam/ale-tasks-base.git",
                commit="a" * 40,
                path="tasks/demo/docker_in_vm",
            ),
            agent=agent_provenance(OracleHarness(), ""),
            gateway=gateway_provenance(settings),
            config_hash=settings.config_hash,
        ),
        collect_artifacts=True,
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    (entry,) = json.loads((result.run_dir / "artifacts/snapshot.json").read_text())["entries"]
    assert entry["source"] == "/home/user/output/nested-container.txt"
    stored = result.run_dir / "artifacts" / entry["stored_path"]
    assert stored.read_text() == "nested-container-ok\n"
    assert phases == [
        Phase.PROVISION,
        Phase.SETUP,
        Phase.AGENT,
        Phase.VERIFY,
        Phase.TEARDOWN,
    ]
    assert result.lock is not None
    assert result.lock.framework.schema_version == 3
    assert result.lock.resources is not None
    assert result.lock.resources.effective.storage_mb is not None
    assert result.lock.resources.effective.storage_mb >= 8192
    image = result.lock.image
    assert image.declaration.kind == "vm"
    assert image.source == "local"
    assert image.provider == "qemu"
    assert image.observed_identity == image.prepared_identity
    assert image.builder_identity and image.base_materials
    assert "content_digest" not in image.model_dump()
