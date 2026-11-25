"""One synthetic episode proves canonical ownership without a model or container."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orbit.core.capability import CapabilityProfile, CpuHold, SandboxNeeds
from orbit.core.episode import EpisodeContext
from orbit.core.lock import (
    AgentProvenance,
    FrameworkProvenance,
    GatewayProvenance,
    TaskSource,
)
from orbit.core.result import PhaseTiming, ResultRecord
from orbit.core.sandbox import (
    ExecResult,
    ImageKind,
    ImageRef,
    PreparedTaskImage,
    Provider,
    ResolvedImage,
    ResourceAllocation,
    SandboxRequest,
)
from orbit.core.task import Task
from orbit.core.taskspec import TaskSpec
from orbit.core.trace import (
    PhaseFinished,
    PhaseStarted,
    TrajectoryLink,
    TransportCall,
    read_jsonl,
)
from orbit.core.trajectory import (
    AtifAgent,
    AtifContentPart,
    AtifImageSource,
    AtifObservation,
    AtifObservationResult,
    AtifStep,
    AtifToolCall,
    AtifTrajectory,
)
from orbit.core.verdict import Verdict
from orbit.run.episode import run_episode
from orbit.run.ledger import Ledger, episode_identity
from orbit.run.provenance import ProvenanceInputs
from orbit.run.recording import CommandRecorder
from tests.support import provider_registry

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "a" * 64


class DemoTask(Task):
    async def score(self, ctx: EpisodeContext) -> dict[str, float]:
        return {"correctness": 1.0, "format": 1.0}


class NoSandboxProvider(Provider):
    name = "none"

    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        return ()

    async def preflight(self, needs: Sequence[SandboxNeeds] = ()) -> None:
        return None

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        assert isinstance(image, PreparedTaskImage)
        return image

    async def create(self, request: SandboxRequest):  # type: ignore[no-untyped-def]
        raise AssertionError("this protocol does not provision")


class LoggedProtocol:
    """Stands in for the episode protocol: records evidence through the context only."""

    name = "test/logged"

    async def run(self, task: Task, ctx: EpisodeContext) -> Verdict:
        assert ctx.execution is not None
        assert ctx.transport is not None
        assert ctx.trajectory is not None
        assert ctx.blobs is not None
        ctx.image_digest = DIGEST
        ctx.resolved_image = ResolvedImage(
            kind=ImageKind.CONTAINER,
            prepared_identity=DIGEST,
            observed_identity=DIGEST,
            observed_ref="orbit-task:fixture",
        )
        ctx.resource_allocation = ResourceAllocation(
            cpus=task.spec.resources.cpus,
            cpu_hold=CpuHold.SHARED,
            memory_mb=task.spec.resources.memory_mb,
            storage_mb=None,
            sudo=False,
            network_mode=task.spec.network.mode,
            provider="none",
            leaf="none",
        )
        ctx.agent_version = "1"
        now = datetime.now(UTC)
        ctx.execution.append(
            PhaseStarted(
                episode_id=ctx.episode_id,
                phase="setup",
                component=self.name,
            )
        )
        recorder = CommandRecorder(
            execution=ctx.execution,
            blobs=ctx.blobs,  # type: ignore[arg-type]
            episode_id=ctx.episode_id,
            phase="setup",
            component="task-stage",
            execution_id="setup-1",
            actor="task",
            argv=["bash", "/opt/orbit/setup/run.sh"],
        )
        await recorder.write("stdout", b"prepared\n")
        recorder.finish(ExecResult(exit_code=0, duration_ms=3))
        ctx.execution.append(
            PhaseFinished(
                episode_id=ctx.episode_id,
                phase="setup",
                component=self.name,
                outcome="succeeded",
                duration_ms=3,
            )
        )
        ctx.phases.append(
            PhaseTiming(
                phase="setup",
                started_at=now,
                finished_at=now,
                duration_ms=3,
                outcome="succeeded",
            )
        )

        image = ctx.blobs.put(b"\x89PNG\r\n\x1a\n", media_type="image/png")
        call_id = "gateway-call-1"
        ctx.transport.append(
            TransportCall(
                episode_id=ctx.episode_id,
                call_id=call_id,
                model="model",
                request_digest=DIGEST,
                response_digest=DIGEST,
                provider_response_id="msg-1",
                input_tokens=5,
                output_tokens=2,
                disposition="forwarded",
            )
        )
        trajectory = AtifTrajectory(
            trajectory_id=ctx.trajectory_id,
            session_id=ctx.episode_id,
            agent=AtifAgent(name="fake-autonomous", version="1", model_name="model"),
            steps=[
                AtifStep(step_id=1, source="user", message=task.spec.instruction),
                AtifStep(
                    step_id=2,
                    source="agent",
                    message="used the task tool",
                    tool_calls=[
                        AtifToolCall(
                            tool_call_id="tool-1",
                            function_name="mcp__task-proof__inspect",
                            arguments={"path": "/workspace/input"},
                        )
                    ],
                    observation=AtifObservation(
                        results=[
                            AtifObservationResult(
                                source_call_id="tool-1",
                                content=[
                                    AtifContentPart(type="text", text="visible"),
                                    AtifContentPart(
                                        type="image",
                                        source=AtifImageSource(
                                            media_type="image/png",
                                            path=image.path,
                                        ),
                                    ),
                                ],
                            )
                        ]
                    ),
                    extra={"orbit": {"transport_call_ids": [call_id]}},
                ),
            ],
        )
        ctx.trajectory.write_trajectory(trajectory)
        ctx.transport.append(
            TrajectoryLink(
                episode_id=ctx.episode_id,
                call_id=call_id,
                trajectory_id=ctx.trajectory_id,
                step_id=2,
            )
        )
        return Verdict.completed(await task.score(ctx))


def provenance() -> ProvenanceInputs:
    return ProvenanceInputs(
        source=TaskSource(kind="local", path="/tasks/demo"),
        agent=AgentProvenance(
            harness="fake-autonomous",
            family="autonomous",
            version="1",
            integrity="test:1",
            model="model",
        ),
        gateway=GatewayProvenance(dialect="anthropic"),
        framework=FrameworkProvenance(version="test", commit="deadbeef"),
        config_hash=DIGEST,
    )


@pytest.mark.asyncio
async def test_complete_episode_has_one_authoritative_home_per_fact(
    tmp_path: Path,
) -> None:
    task = DemoTask(
        TaskSpec(
            name="agentslastexam/demo-logging",
            instruction="Use the task tool and inspect the image.",
            image={"kind": "container"},
        ),
        folder=None,
        task_digest=DIGEST,
        image_source_digest=DIGEST,
    )
    task.prepared_image = PreparedTaskImage(
        kind="container",
        source="solver-local",
        input_identity=DIGEST,
        image_source_identity=DIGEST,
        runtime_ref="orbit-task:fixture",
        prepared_identity=DIGEST,
    )
    # As a Run that selected and checked its Providers hands the Task over.
    task.provider = "docker:runc"
    providers = provider_registry()
    providers._instances[task.provider] = NoSandboxProvider()
    episode = await run_episode(
        task,
        LoggedProtocol(),  # type: ignore[arg-type]
        providers,
        run_dir=tmp_path,
        provenance=provenance(),
        episode_id="episode",
        trajectory_id="trajectory",
    )

    assert episode.record.status.value == "completed"
    expected = {
        "trajectory.json",
        "trace.transport.jsonl",
        "trace.execution.jsonl",
        "result.json",
        "lock.json",
        "blobs",
    }
    assert expected <= {path.name for path in episode.run_dir.iterdir()}
    assert not (episode.run_dir / "events.jsonl").exists()
    assert not (episode.run_dir / "trace.semantic.jsonl").exists()
    assert not list(episode.run_dir.glob("agent-output-*"))

    trajectory = json.loads((episode.run_dir / "trajectory.json").read_text())
    result = ResultRecord.model_validate_json((episode.run_dir / "result.json").read_text())
    transport = read_jsonl(episode.run_dir / "trace.transport.jsonl").records
    execution = read_jsonl(episode.run_dir / "trace.execution.jsonl").records
    image_path = trajectory["steps"][1]["observation"]["results"][0]["content"][1]["source"]["path"]
    assert (episode.run_dir / image_path).is_file()
    assert result.rewards == {"correctness": 1.0, "format": 1.0}
    assert all("rewards" not in record for record in transport + execution)
    assert all("messages" not in record for record in transport)
    assert "base64" not in json.dumps(trajectory).lower()

    ledger = Ledger(tmp_path)
    ledger.open_run("run", DIGEST)
    ledger.start_episode(
        episode_id=episode.episode_id,
        run_id="run",
        identity=episode_identity(
            task.spec,
            task_digest=task.task_digest,
            image_digest=DIGEST,
            agent="fake-autonomous@1",
            seed=0,
            reuse_hash=DIGEST,
        ),
        spec=task.spec,
    )
    ledger.finish_episode(episode.episode_id, episode.record)
    ledger.close()
    with sqlite3.connect(tmp_path / "ledger.db") as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(episodes)").fetchall()}
    assert {
        "lock_json",
        "result_json",
        "trajectory_json",
        "transport_json",
        "execution_json",
        "native_log",
        "artifact",
        "blob",
    }.isdisjoint(columns)
