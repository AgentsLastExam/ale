from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.config import LoggingPolicy
from ale.core.errors import TrajectoryConversionError
from ale.core.harness import AgentRun
from ale.run.environments.standard import StandardEnvironment
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.recording import EpisodeRecording

pytestmark = pytest.mark.integration


def context(root: Path, policy: LoggingPolicy, run: AgentRun | None):
    recording = EpisodeRecording(root)
    logs = root / "logs/claude-code"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text(
        json.dumps({"type": "result", "session_id": "session", "result": "done"}) + "\n"
    )
    return SimpleNamespace(
        blobs=recording.blobs,
        trajectory=recording,
        transport=recording.transport,
        episode_id=root.name,
        trajectory_id="trajectory",
        run_dir=root,
        session=SimpleNamespace(model="model"),
        agent_version="1",
        spec=SimpleNamespace(instruction="instruction"),
        extras={"agent_run": run, "logging_policy": policy},
    )


def test_minimal_deletes_successful_native_logs(tmp_path: Path) -> None:
    root = tmp_path / "minimal"
    ctx = context(root, LoggingPolicy(), AgentRun(exit_code=0, final_message="done"))
    StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(ctx)  # type: ignore[arg-type]
    assert (root / "trajectory.json").is_file()
    assert not (root / "logs/claude-code").exists()


def test_debug_and_interruption_keep_native_logs(tmp_path: Path) -> None:
    debug = tmp_path / "debug"
    debug_ctx = context(
        debug,
        LoggingPolicy(native_logs="debug"),
        AgentRun(exit_code=0, final_message="done"),
    )
    StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(debug_ctx)  # type: ignore[arg-type]
    assert (debug / "logs/claude-code/transcript.jsonl").is_file()

    interrupted = tmp_path / "interrupted"
    interrupted_ctx = context(interrupted, LoggingPolicy(), None)
    StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(  # type: ignore[arg-type]
        interrupted_ctx
    )
    assert (interrupted / "logs/claude-code/transcript.jsonl").is_file()
    trajectory = json.loads((interrupted / "trajectory.json").read_text())
    assert trajectory["extra"]["ale"]["incomplete"] is True


def test_conversion_failure_keeps_native_logs(tmp_path: Path) -> None:
    root = tmp_path / "failed"
    ctx = context(root, LoggingPolicy(), AgentRun(exit_code=0, final_message="done"))
    transcript = root / "logs/claude-code/transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "orphan",
                            "content": "unmatched",
                        }
                    ]
                },
            }
        )
        + "\n"
    )

    with pytest.raises(TrajectoryConversionError):
        StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(ctx)  # type: ignore[arg-type]

    assert transcript.is_file()


def test_minimal_retention_is_smaller_than_debug(tmp_path: Path) -> None:
    minimal = tmp_path / "minimal-size"
    debug = tmp_path / "debug-size"
    StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(  # type: ignore[arg-type]
        context(minimal, LoggingPolicy(), AgentRun(exit_code=0, final_message="done"))
    )
    StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(  # type: ignore[arg-type]
        context(
            debug,
            LoggingPolicy(native_logs="debug"),
            AgentRun(exit_code=0, final_message="done"),
        )
    )

    minimal_size = sum(path.stat().st_size for path in minimal.rglob("*") if path.is_file())
    debug_size = sum(path.stat().st_size for path in debug.rglob("*") if path.is_file())
    assert debug_size > minimal_size
