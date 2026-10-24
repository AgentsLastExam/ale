"""Periodic trajectories must not perform finalization or discard useful evidence."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from ale.core.config import LoggingPolicy, load_run_config
from ale.core.harness import AgentRun, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import ExecResult
from ale.core.trajectory import AtifAgent, AtifStep, AtifTrajectory
from ale.run.environments import standard
from ale.run.environments.standard import StandardEnvironment
from ale.run.harnesses.base import CliCommand, CliHarness, NativeResult
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.harnesses.codex_cli import CodexCliHarness
from ale.run.harnesses.grok_build import GrokBuildHarness
from ale.run.harnesses.openclaw_cli import OpenClawCliHarness
from ale.run.recording import EpisodeRecording

pytestmark = pytest.mark.unit


class _Settings(BaseModel):
    pass


class EvidenceHarness(CliHarness):
    name = "evidence-test"
    native_logs = ()
    settings = _Settings()

    def version(self) -> str:
        return "1"

    async def prepare_command(self, instruction_path, sandbox, session, *, native_session_id):
        return CliCommand(("unused",), {})

    def read_result(self, result: ExecResult, session: HarnessSession) -> NativeResult:
        return NativeResult(None, result.stdout)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        content = (context.logs_dir / "stdout.log").read_text()
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
    (logs / "stdout.log").write_text("first observed response")
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


def test_live_conversion_retains_raw_input_and_defers_final_side_effects(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    env = StandardEnvironment(EvidenceHarness())
    final_calls = []
    persist = env._persist_trajectory
    monkeypatch.setattr(env, "_persist_trajectory", lambda *args: final_calls.append(args))
    env._parse_harness_trajectory(ctx, final=False)
    assert _read(ctx)["extra"]["ale"]["incomplete_reason"] == "running"
    assert not ctx.trajectory_written
    assert (tmp_path / "logs/evidence-test/stdout.log").exists()
    assert final_calls == []

    ctx.agent_run = AgentRun(exit_code=0, final_message="done")
    monkeypatch.setattr(env, "_persist_trajectory", persist)
    env._parse_harness_trajectory(ctx)
    assert ctx.trajectory_written
    assert "incomplete" not in _read(ctx).get("extra", {}).get("ale", {})
    assert not (tmp_path / "logs/evidence-test").exists()


def test_transient_and_final_parser_failures_preserve_last_valid_history(tmp_path):
    ctx = _context(tmp_path)
    env = StandardEnvironment(EvidenceHarness())
    env._parse_harness_trajectory(ctx, final=False)
    prior = (tmp_path / "trajectory.json").read_bytes()
    (tmp_path / "logs/evidence-test/stdout.log").write_text("broken")
    env._parse_harness_trajectory(ctx, final=False)
    assert (tmp_path / "trajectory.json").read_bytes() == prior
    ctx.agent_run = AgentRun(exit_code=0)
    env._parse_harness_trajectory(ctx)
    result = _read(ctx)
    assert result["steps"][0]["message"] == "first observed response"
    assert result["extra"]["ale"]["incomplete_reason"] == "conversion_failed"
    assert (tmp_path / "logs/evidence-test/stdout.log").exists()


@pytest.mark.asyncio
async def test_final_collection_and_conversion_precede_failing_cleanup(tmp_path):
    ctx = _context(tmp_path)
    ctx.agent_run = AgentRun(exit_code=0)
    order = []

    class CleanupFailure(EvidenceHarness):
        async def cleanup(self, sandbox, session):
            assert (tmp_path / "trajectory.json").exists()
            order.append("cleanup")
            raise RuntimeError("cleanup failed")

    class Collector:
        incomplete = False

        async def sync(self, *, final=False):
            assert final
            order.append("collect")
            return True

    sandbox = SimpleNamespace(gateway_url=None, sandbox_id="sandbox")
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await StandardEnvironment(CleanupFailure())._capture_solver_evidence(
            ctx, sandbox, collector=Collector()
        )
    assert order == ["collect", "cleanup"]
    assert ctx.solver_evidence_captured
    assert ctx.trajectory_written


@pytest.mark.asyncio
async def test_periodic_read_timeout_retries_and_worker_can_stop(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    calls = 0
    retried = asyncio.Event()
    monkeypatch.setattr(standard, "SYNC_INTERVAL_SEC", 0.001)
    monkeypatch.setattr(standard, "SYNC_TIMEOUT_SEC", 0.01)

    class Collector:
        async def sync(self):
            nonlocal calls
            calls += 1
            if calls > 1:
                retried.set()
            await asyncio.Event().wait()

    worker = asyncio.create_task(
        StandardEnvironment(EvidenceHarness())._poll_cli_evidence(ctx, Collector())
    )
    try:
        await asyncio.wait_for(retried.wait(), timeout=1)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    assert calls >= 2


@pytest.mark.asyncio
async def test_cancelled_parser_worker_cannot_overwrite_final_trajectory(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(standard, "SYNC_INTERVAL_SEC", 0.001)

    class SlowParser(EvidenceHarness):
        def parse_trajectory(self, context):
            trajectory = super().parse_trajectory(context)
            if context.incomplete_reason == "running":
                entered.set()
                assert release.wait(timeout=5)
                finished.set()
            return trajectory

    class Collector:
        async def sync(self):
            return True

    env = StandardEnvironment(SlowParser())
    worker = asyncio.create_task(env._poll_cli_evidence(ctx, Collector()))
    try:
        # Waiting here also proves parsing does not occupy the episode event loop.
        assert await asyncio.to_thread(entered.wait, 1)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, timeout=1)
        ctx.agent_run = AgentRun(exit_code=0)
        env._parse_harness_trajectory(ctx)
        final_bytes = (tmp_path / "trajectory.json").read_bytes()
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await asyncio.sleep(0)
        assert (tmp_path / "trajectory.json").read_bytes() == final_bytes
        assert not _read(ctx).get("extra", {}).get("ale", {}).get("incomplete")
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker


def test_experiment_native_collection_default_and_override(tmp_path):
    assert not load_run_config().logging.collect_native_logs
    config = tmp_path / "run.toml"
    config.write_text("[logging]\ncollect_native_logs=true\nnative_logs='debug'\n")
    assert load_run_config(run_path=config).logging.collect_native_logs
    assert not load_run_config(
        run_path=config, overrides=["logging.collect_native_logs=false"]
    ).logging.collect_native_logs


@pytest.mark.parametrize(
    ("harness", "native_file", "root_event"),
    [
        (
            ClaudeCodeHarness(),
            ".claude-config/projects/project/session.jsonl",
            {"type": "assistant", "sessionId": "session", "message": {"content": "done"}},
        ),
        (
            CodexCliHarness(),
            ".codex-ale/sessions/session.jsonl",
            {"type": "session_meta", "payload": {"id": "session"}},
        ),
        (
            GrokBuildHarness(),
            ".grok-ale/sessions/session/chat_history.jsonl",
            {"type": "assistant", "content": "done"},
        ),
        (
            OpenClawCliHarness(),
            ".openclaw-ale/agents/main/sessions/session.jsonl",
            {
                "type": "message",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            },
        ),
    ],
)
def test_minimal_retention_keeps_native_sessions_outside_parser_coverage(
    tmp_path, harness, native_file, root_event
):
    ctx = _context(tmp_path)
    logs = tmp_path / "logs" / harness.name
    (tmp_path / "logs/evidence-test").rename(logs)
    (logs / "stdout.log").write_text("")
    (logs / "invocations.jsonl").write_text(
        json.dumps({"stdout_offset": 0, "expected_session_id": "session"}) + "\n"
    )
    root = logs / "native" / native_file
    root.parent.mkdir(parents=True)
    root.write_text(json.dumps(root_event) + "\n")
    child = (
        logs
        / "native"
        / native_file.replace("session/", "child/").replace("session.jsonl", "child.jsonl")
    )
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_text('{"message":"unconverted child evidence"}\n')
    ctx.agent_run = AgentRun(exit_code=0, final_message="done")
    ctx.logging_policy = LoggingPolicy(collect_native_logs=True, native_logs="minimal")

    StandardEnvironment(harness)._parse_harness_trajectory(ctx)

    trajectory = _read(ctx)
    assert ctx.trajectory_written
    assert "unconverted child evidence" not in json.dumps(trajectory)
    assert trajectory["extra"]["ale"]["parse_issues"] == [
        {
            "reason": "unconverted_native_files",
            "count": 1,
            "sources": [child.relative_to(logs).as_posix()],
        }
    ]
    assert child.read_text() == '{"message":"unconverted child evidence"}\n'
