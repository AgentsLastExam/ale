"""Claude native evidence maps deterministically to the shared ATIF contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale.core.errors import TrajectoryReferenceError
from ale.core.harness import TrajectoryParseContext
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.recording import BlobStore

pytestmark = pytest.mark.unit


def parse(tmp_path: Path, events: list[dict], *, incomplete: bool = False):
    (tmp_path / "transcript.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events)
        + ('{"type":"assistant"\n' if incomplete else "")
    )
    return ClaudeCodeHarness().parse_trajectory(
        TrajectoryParseContext(
            episode_id="episode",
            trajectory_id="trajectory",
            instruction="Complete instruction.",
            logs_dir=tmp_path,
            model="model",
            agent_version="1",
            blobs=BlobStore(tmp_path),
            incomplete=incomplete,
            incomplete_reason="agent_interrupted" if incomplete else None,
        )
    )


def test_system_instruction_native_order_tool_grouping_and_subagent_refs(
    tmp_path: Path,
) -> None:
    trajectory = parse(
        tmp_path,
        [
            {"type": "system", "timestamp": "2026-07-29T00:00:00Z", "message": "system"},
            {
                "type": "user",
                "timestamp": "2026-07-29T00:00:01Z",
                "message": {"content": "Complete instruction."},
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-29T00:00:02Z",
                "message": {
                    "id": "msg-root",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "delegate-1",
                            "name": "Task",
                            "input": {"prompt": "Investigate"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "delegate-1",
                            "content": "delegated",
                        }
                    ]
                },
            },
            {
                "type": "user",
                "isSidechain": True,
                "agentId": "researcher",
                "parentToolUseID": "delegate-1",
                "sessionId": "sub-session",
                "message": {"content": "Investigate"},
            },
            {
                "type": "assistant",
                "isSidechain": True,
                "agentId": "researcher",
                "parentToolUseID": "delegate-1",
                "sessionId": "sub-session",
                "message": {"id": "msg-sub", "content": [{"type": "text", "text": "found"}]},
            },
        ],
    )

    assert [step.source for step in trajectory.steps[:3]] == [
        "system",
        "user",
        "agent",
    ]
    assert trajectory.steps[1].message == "Complete instruction."
    root = trajectory.steps[2]
    assert root.tool_calls[0].tool_call_id == "delegate-1"
    assert root.observation.results[0].content == "delegated"
    (ref,) = root.observation.results[0].subagent_trajectory_ref
    assert ref.trajectory_id == "trajectory-subagent-researcher"
    (subagent,) = trajectory.subagent_trajectories
    assert subagent.trajectory_id == ref.trajectory_id
    assert subagent.session_id == "sub-session"
    assert [step.message for step in subagent.steps] == ["Investigate", "found"]


def test_missing_result_stays_absent_and_partial_prefix_is_valid(tmp_path: Path) -> None:
    trajectory = parse(
        tmp_path,
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "missing",
                            "name": "lookup",
                            "input": {},
                        }
                    ]
                },
            }
        ],
        incomplete=True,
    )
    assert trajectory.steps[1].observation is None
    assert trajectory.extra["ale"]["incomplete"] is True
    assert trajectory.extra["ale"]["parse_issues"][0]["reason"] == "malformed_json"


def test_orphan_result_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TrajectoryReferenceError, match="unknown calls"):
        parse(
            tmp_path,
            [
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "orphan",
                                "content": "x",
                            }
                        ]
                    },
                }
            ],
        )


def test_same_sandbox_resume_remains_one_logical_trajectory(tmp_path: Path) -> None:
    trajectory = parse(
        tmp_path,
        [
            {"type": "result", "session_id": "native-session", "result": "segment one"},
            {"type": "result", "session_id": "native-session", "result": "segment two"},
        ],
    )
    assert trajectory.session_id == "native-session"
    assert trajectory.continued_trajectory_ref is None
    assert [step.message for step in trajectory.steps[1:]] == [
        "segment one",
        "segment two",
    ]
