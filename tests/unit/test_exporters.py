from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ale.core.result import ResultRecord
from ale.core.trajectory import (
    AtifAgent,
    AtifMetrics,
    AtifObservation,
    AtifObservationResult,
    AtifStep,
    AtifSubagentTrajectoryRef,
    AtifToolCall,
    AtifTrajectory,
)
from ale.core.verdict import Status
from ale.run.exporters import (
    harbor_result,
    harbor_trajectory,
    prime_verifiers_record,
    scalar_reward,
)

pytestmark = pytest.mark.unit


def fixture() -> tuple[AtifTrajectory, ResultRecord]:
    trajectory = AtifTrajectory(
        trajectory_id="trajectory",
        session_id="session",
        agent=AtifAgent(name="claude-code", version="1", model_name="model"),
        steps=[
            AtifStep(step_id=1, source="user", message="solve"),
            AtifStep(
                step_id=2,
                source="agent",
                model_name="model",
                message="checking",
                tool_calls=[
                    AtifToolCall(
                        tool_call_id="call-1",
                        function_name="lookup",
                        arguments={"key": "answer"},
                    )
                ],
                observation=AtifObservation(
                    results=[
                        AtifObservationResult(
                            source_call_id="call-1",
                            content="42",
                        )
                    ]
                ),
                metrics=AtifMetrics(prompt_tokens=10, completion_tokens=2),
            ),
        ],
    )
    now = datetime.now(UTC)
    result = ResultRecord(
        episode_id="episode",
        status=Status.COMPLETED,
        rewards={"correctness": 1.0, "format": 0.5},
        metrics={"elapsed": 1.2},
        started_at=now,
        finished_at=now,
    )
    return trajectory, result


def test_harbor_export_is_direct_atif_and_preserves_all_rewards() -> None:
    trajectory, result = fixture()
    assert harbor_trajectory(trajectory)["schema_version"] == "ATIF-v1.7"
    assert harbor_result(result) == {"rewards": {"correctness": 1.0, "format": 0.5}}


def test_prime_verifiers_record_preserves_messages_tools_calls_and_rewards() -> None:
    trajectory, result = fixture()
    transport = [
        {
            "kind": "call",
            "call_id": "gateway-1",
            "model": "model",
            "timestamp": "2026-07-29T00:00:00Z",
            "input_tokens": 10,
            "output_tokens": 2,
            "cost_usd": 0.01,
            "latency_ms": 100,
            "stop_reason": "tool_use",
            "disposition": "forwarded",
        },
        {
            "kind": "trajectory_link",
            "call_id": "gateway-1",
            "step_id": 2,
        },
    ]
    record = prime_verifiers_record(trajectory, result, transport)
    assert [node["message"]["role"] for node in record["nodes"]] == [
        "user",
        "assistant",
        "tool",
    ]
    assert record["calls"][0]["node"] == 2 - 1
    assert record["rewards"] == result.rewards
    assert record["metrics"] == result.metrics
    assert "reward" not in record


def test_prime_verifiers_record_preserves_subagent_branch_parentage() -> None:
    trajectory, result = fixture()
    subagent = AtifTrajectory(
        trajectory_id="subagent-1",
        agent=AtifAgent(name="subagent", version="1"),
        steps=[
            AtifStep(step_id=1, source="user", message="research"),
            AtifStep(step_id=2, source="agent", message="found it"),
        ],
    )
    assistant = trajectory.steps[1]
    observation = assistant.observation
    assert observation is not None
    branch_result = observation.results[0].model_copy(
        update={"subagent_trajectory_ref": [AtifSubagentTrajectoryRef(trajectory_id="subagent-1")]}
    )
    trajectory = trajectory.model_copy(
        update={
            "steps": [
                trajectory.steps[0],
                assistant.model_copy(
                    update={
                        "observation": observation.model_copy(update={"results": [branch_result]})
                    }
                ),
            ],
            "subagent_trajectories": [subagent],
        }
    )

    record = prime_verifiers_record(trajectory, result)

    assert [node["message"]["role"] for node in record["nodes"]] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    assert record["nodes"][3]["parent"] == 1
    assert record["nodes"][4]["parent"] == 3


def test_scalar_export_requires_an_explicit_policy() -> None:
    _, result = fixture()
    assert scalar_reward(result, lambda rewards: rewards["correctness"]) == 1.0
