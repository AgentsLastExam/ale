"""A converter defect never turns a finished agent run into a failed episode."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orbit.core.config import LoggingPolicy
from orbit.core.harness import AgentRun, AutonomousHarness, TrajectoryParseContext
from orbit.core.trajectory import AtifTrajectory
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.recording import EpisodeRecording

pytestmark = pytest.mark.unit


class BrokenParserHarness(AutonomousHarness):
    name = "broken-parser"
    logs = ("native.jsonl",)

    def version(self) -> str:
        return "1.0.0"

    async def launch(self, instruction, sandbox, session, *, timeout_sec):  # type: ignore[no-untyped-def]
        raise AssertionError("not launched in this test")

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        raise KeyError("tool_use_id secret-episode-token")


def test_conversion_failure_keeps_a_minimal_trajectory_and_the_native_logs(
    tmp_path: Path,
) -> None:
    recording = EpisodeRecording(tmp_path)
    native = tmp_path / "logs" / "broken-parser" / "native.jsonl"
    native.parent.mkdir(parents=True)
    native.write_text('{"truncated":\n')
    ctx = SimpleNamespace(
        blobs=recording.blobs,
        trajectory=recording,
        transport=None,
        agent_run=AgentRun(exit_code=0, final_message="Report written."),
        episode_id="episode",
        trajectory_id="trajectory-1",
        spec=SimpleNamespace(instruction="Write the report.", timeouts=SimpleNamespace(agent=1800)),
        resource_allocation=None,
        run_dir=tmp_path,
        session=SimpleNamespace(model="model", token="secret-episode-token"),
        secrets=("secret-episode-token",),
        agent_version="1.0.0",
        logging_policy=LoggingPolicy(native_logs="minimal"),
        trajectory_written=False,
    )

    EpisodeProtocol(BrokenParserHarness())._parse_harness_trajectory(ctx)  # type: ignore[arg-type]

    assert ctx.trajectory_written
    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    # The first prompt as it was sent, with the time stated below the instruction.
    assert [step["message"] for step in trajectory["steps"]] == [
        "Write the report.\n\n---\n\nYour time limit is 30 minutes. If it runs out, your work "
        "is scored as it is then.\n",
        "Report written.",
    ]
    orbit = trajectory["extra"]["orbit"]
    assert orbit["incomplete"] is True
    assert orbit["incomplete_reason"] == "conversion_failed"
    (issue,) = orbit["parse_issues"]
    assert issue["reason"] == "conversion_failed"
    assert issue["detail"].startswith("KeyError:")
    assert "secret-episode-token" not in issue["detail"]
    assert native.exists()
