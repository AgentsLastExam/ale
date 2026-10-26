"""Logs are copied while the agent runs and converted once, after the final copy."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.config import LoggingPolicy
from ale.core.harness import AgentRun, AutonomousHarness, HarnessSession, TrajectoryParseContext
from ale.core.trajectory import AtifAgent, AtifStep, AtifTrajectory
from ale.run.environments import standard
from ale.run.environments.standard import RetainedVerificationEnvironment, StandardEnvironment
from ale.run.recording import EpisodeRecording

pytestmark = pytest.mark.unit


class EvidenceHarness(AutonomousHarness):
    name = "evidence-test"
    logs = ("evidence.log",)

    def version(self) -> str:
        return "1"

    async def launch(self, instruction, sandbox, session, *, timeout_sec):  # type: ignore[no-untyped-def]
        raise AssertionError("the environment phases under test do not launch")

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        content = (context.logs_dir / "evidence.log").read_text()
        if content == "broken":
            raise ValueError("broken conversion")
        return AtifTrajectory(
            trajectory_id=context.trajectory_id,
            agent=AtifAgent(name=self.name, version="1"),
            steps=[AtifStep(step_id=1, source="agent", message=content)],
        )


def _context(path: Path) -> SimpleNamespace:
    recording = EpisodeRecording(path)
    logs = path / "logs/evidence-test"
    logs.mkdir(parents=True)
    (logs / "evidence.log").write_text("first observed response")
    return SimpleNamespace(
        blobs=recording.blobs,
        trajectory=recording,
        transport=recording.transport,
        episode_id="episode",
        trajectory_id="trajectory",
        run_dir=path,
        session=HarnessSession(
            episode_id="episode", model="model", token="token", gateway_url="http://gateway"
        ),
        agent_version="1",
        spec=SimpleNamespace(instruction="instruction", artifacts=()),
        agent_run=None,
        logging_policy=LoggingPolicy(),
        trajectory_written=False,
        agent_started=True,
        solver_evidence_captured=False,
        artifacts=SimpleNamespace(enabled=False),
        agent_resources=SimpleNamespace(digest="resources"),
        home="/home/agent",
    )


def _read(ctx: SimpleNamespace) -> dict:
    return json.loads((ctx.run_dir / "trajectory.json").read_text())


def test_complete_conversion_drops_native_logs_under_minimal_retention(tmp_path):
    ctx = _context(tmp_path)
    ctx.agent_run = AgentRun(exit_code=0, final_message="done")
    ctx.logging_policy = LoggingPolicy(native_logs="minimal")
    StandardEnvironment(EvidenceHarness())._parse_harness_trajectory(ctx)
    assert ctx.trajectory_written
    assert "incomplete" not in _read(ctx).get("extra", {}).get("ale", {})
    assert not (tmp_path / "logs/evidence-test").exists()


def test_parser_failure_after_an_interrupted_agent_is_still_conversion_failed(tmp_path):
    ctx = _context(tmp_path)
    (tmp_path / "logs/evidence-test/evidence.log").write_text("broken")
    trajectory = StandardEnvironment(EvidenceHarness())._convert_harness_trajectory(ctx)
    assert trajectory.extra["ale"]["incomplete_reason"] == "conversion_failed"


@pytest.mark.asyncio
async def test_final_collection_precedes_conversion(tmp_path):
    ctx = _context(tmp_path)
    ctx.agent_run = AgentRun(exit_code=0)

    class Collector:
        incomplete = False

        async def sync(self, *, final=False):
            assert final
            assert not (tmp_path / "trajectory.json").exists()

    sandbox = SimpleNamespace(gateway_url=None, sandbox_id="sandbox")
    await StandardEnvironment(EvidenceHarness())._capture_solver_evidence(
        ctx, sandbox, logs=Collector()
    )
    assert ctx.solver_evidence_captured
    assert ctx.trajectory_written


@pytest.mark.asyncio
async def test_polling_only_copies_logs_and_retries_after_a_timeout(tmp_path, monkeypatch):
    calls = 0
    retried = asyncio.Event()
    monkeypatch.setattr(standard, "SYNC_INTERVAL_SEC", 0.001)
    monkeypatch.setattr(standard, "SYNC_TIMEOUT_SEC", 0.01)

    class NeverConverted(EvidenceHarness):
        def parse_trajectory(self, context):
            raise AssertionError("only the final capture converts")

    class Collector:
        async def sync(self):
            nonlocal calls
            calls += 1
            if calls > 1:
                retried.set()
            await asyncio.Event().wait()

    worker = asyncio.create_task(StandardEnvironment(NeverConverted())._poll_logs(Collector()))
    try:
        await asyncio.wait_for(retried.wait(), timeout=1)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    assert calls >= 2
    assert not (tmp_path / "trajectory.json").exists()


@pytest.mark.asyncio
async def test_retained_reverification_captures_evidence_without_a_harness(tmp_path):
    ctx = _context(tmp_path)
    ctx.agent_started = False
    environment = RetainedVerificationEnvironment(
        SimpleNamespace(), tmp_path, sandbox_identity=None
    )
    await environment._capture_solver_evidence(ctx, SimpleNamespace())
    assert ctx.solver_evidence_captured
