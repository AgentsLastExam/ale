"""Provenance and resume: the two things that make a verdict usable afterwards.

A number nobody can attribute is worse than no number, and a run that cannot be picked
up after an interruption is a run nobody dares start. Both are Constitution III, and
both are only real end-to-end — hence the container.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.harness import (
    AgentRun,
    EffectiveAgentResources,
    HarnessSession,
    ResolvedSkill,
)
from ale.core.lock import TaskSource
from ale.core.sandbox import Identity, Sandbox
from ale.core.trajectory import AtifTrajectory
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.ledger import Ledger, episode_identity
from ale.run.provenance import (
    ProvenanceInputs,
    agent_provenance,
    framework_provenance,
    gateway_provenance,
)
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import ManifestTaskset

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


def inputs_for(harness: object, *, kind: str = "registry") -> ProvenanceInputs:
    source = (
        TaskSource(kind="registry", repo="https://example.com/tasks.git", commit="a" * 40, path="t")
        if kind == "registry"
        else TaskSource(kind="local", path="/tmp/t")
    )
    settings = RunConfig()
    return ProvenanceInputs(
        source=source,
        agent=agent_provenance(harness, "test-model"),
        gateway=gateway_provenance(settings),
        config_hash=settings.config_hash,
    )


async def run_with_provenance(task_root: Path, run_dir: Path, inputs: ProvenanceInputs):  # type: ignore[no-untyped-def]
    task = next(iter(ManifestTaskset(task_root).load()))
    return await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        DockerProvider(),
        run_dir=run_dir,
        provenance=inputs,
    )


@pytest.mark.asyncio
async def test_every_provenance_field_is_populated(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """An auditor must be able to enumerate every input from the record alone."""
    task_root = write_repo(tmp_path / "repo")
    result = await run_with_provenance(task_root, tmp_path / "runs", inputs_for(OracleHarness()))

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    lock = result.lock
    assert lock is not None

    # The digest is the point: a tag alone would let two different images look comparable.
    assert lock.image.digest.startswith("sha256:")
    assert lock.image.digest != lock.image.ref
    assert lock.task.spec_hash.startswith("sha256:")
    assert lock.config_hash.startswith("sha256:")
    assert lock.agent.family == "autonomous"
    assert lock.framework.version
    assert lock.gateway.dialect == "anthropic"

    on_disk = json.loads((result.run_dir / "lock.json").read_text())
    assert on_disk["task"]["spec_hash"] == lock.task.spec_hash


@pytest.mark.asyncio
async def test_a_local_task_is_refused_for_reporting(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """Authoring from a path is fine; publishing a number from one is not."""
    task_root = write_repo(tmp_path / "repo")
    result = await run_with_provenance(
        task_root, tmp_path / "runs", inputs_for(OracleHarness(), kind="local")
    )

    assert result.lock is not None
    problems = result.lock.missing_for_report()
    assert any("local path" in problem for problem in problems)


@pytest.mark.asyncio
async def test_an_unresolved_framework_commit_blocks_reporting(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """`commit: unknown` passes every schema check and helps nobody reproduce anything."""
    task_root = write_repo(tmp_path / "repo")
    inputs = inputs_for(OracleHarness())
    inputs.framework = framework_provenance().model_copy(update={"commit": "unknown"})

    result = await run_with_provenance(task_root, tmp_path / "runs", inputs)

    assert result.lock is not None
    assert any("commit" in problem for problem in result.lock.missing_for_report())


@pytest.mark.asyncio
async def test_resume_skips_completed_work_and_loses_nothing(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """Episodes are matched by what they are, not by when they ran."""
    task_root = write_repo(tmp_path / "repo")
    run_dir = tmp_path / "runs" / "fixed"
    inputs = inputs_for(OracleHarness())
    task = next(iter(ManifestTaskset(task_root).load()))
    identity = episode_identity(
        task.spec,
        agent=f"{inputs.agent.harness}@{inputs.agent.version}",
        seed=0,
        config_hash=inputs.config_hash,
    )

    ledger = Ledger(run_dir)
    ledger.open_run("fixed", inputs.config_hash)
    try:
        for _ in range(2):
            result = await run_with_provenance(task_root, run_dir, inputs)
            ledger.start_episode(
                episode_id=result.episode_id, run_id="fixed", identity=identity, spec=task.spec
            )
            ledger.finish_episode(result.episode_id, result.record)

        done = [row for row in ledger.episodes("fixed") if row.succeeded]
        assert len(done) == 2

        # Identity is stable across runs, so a resume of the same invocation sees both.
        assert all(row.identity == identity for row in done)
        # A different seed is different work and must not be skipped.
        other = episode_identity(
            task.spec,
            agent=f"{inputs.agent.harness}@{inputs.agent.version}",
            seed=99,
            config_hash=inputs.config_hash,
        )
        assert other != identity
    finally:
        ledger.close()


class EvidenceHarness(ClaudeCodeHarness):
    async def install(self, sandbox: Sandbox) -> str:
        return self.version()

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        transcript = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "evidence"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "session_id": "evidence-session",
                        "result": "done",
                    }
                ),
            ]
        )
        await sandbox.write_file(
            f"{session.home}/transcript.jsonl",
            (transcript + "\n").encode(),
            identity=Identity.AGENT,
        )
        return AgentRun(exit_code=0, final_message="done")


@pytest.mark.asyncio
async def test_evidence_is_parsed_when_artifacts_are_disabled(
    tmp_path: Path,
    write_repo: Callable[..., Path],
) -> None:
    task_root = write_repo(tmp_path / "repo")
    task = next(iter(ManifestTaskset(task_root).load()))
    result = await run_episode(
        task,
        StandardEnvironment(EvidenceHarness()),
        DockerProvider(),
        run_dir=tmp_path / "runs",
        collect_artifacts=False,
    )

    assert result.verdict.status is Status.COMPLETED
    assert not (result.run_dir / "logs/claude-code/transcript.jsonl").exists()
    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    assert any(
        step.source == "agent" and step.message in {"evidence", "done"} for step in trajectory.steps
    )


@pytest.mark.asyncio
async def test_unpinned_resource_is_recorded_and_blocks_reporting(
    tmp_path: Path,
    write_repo: Callable[..., Path],
) -> None:
    task_root = write_repo(tmp_path / "repo")
    resource_path = tmp_path / "skill"
    resource_path.mkdir()
    (resource_path / "SKILL.md").write_text("local")
    resources = EffectiveAgentResources(
        skills=(
            ResolvedSkill(
                name="skill",
                path=resource_path,
                source_layers=("run",),
                declared_sources=(str(resource_path),),
                digest="sha256:" + "b" * 64,
                reportable=False,
                reportability_reason="run local resource cannot be re-fetched",
            ),
        ),
        digest="sha256:" + "c" * 64,
    )
    inputs = inputs_for(OracleHarness())
    inputs.agent = agent_provenance(
        OracleHarness(),
        "test-model",
        resources=resources,
    )

    result = await run_with_provenance(task_root, tmp_path / "runs", inputs)

    assert result.lock is not None
    assert result.lock.agent.resources_digest == resources.digest
    assert result.lock.agent.resources[0].digest == "sha256:" + "b" * 64
    assert any("not reportable" in problem for problem in result.lock.missing_for_report())
