"""Canonical Harbor ATIF v1.7 trajectory contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ale.core.trajectory import (
    AtifAgent,
    AtifObservation,
    AtifObservationResult,
    AtifStep,
    AtifSubagentTrajectoryRef,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)

pytestmark = pytest.mark.unit


def agent(name: str = "test-agent") -> AtifAgent:
    return AtifAgent(name=name, version="1.0", model_name="test-model")


def test_exact_v17_root_and_step_fields_reject_unknowns() -> None:
    trajectory = AtifTrajectory(
        trajectory_id="root",
        agent=agent(),
        steps=[AtifStep(step_id=1, source="user", message="hello")],
    )
    assert trajectory.schema_version == "ATIF-v1.7"
    with pytest.raises(ValidationError):
        AtifTrajectory.model_validate(trajectory.model_dump() | {"ale_private": True})
    with pytest.raises(ValidationError):
        AtifStep.model_validate(
            {"step_id": 1, "source": "user", "message": "hello", "unknown": True}
        )


def test_step_ids_are_sequential_from_one() -> None:
    with pytest.raises(ValidationError, match="sequential"):
        AtifTrajectory(
            agent=agent(),
            steps=[
                AtifStep(step_id=1, source="user", message="hello"),
                AtifStep(step_id=3, source="agent", message="skipped"),
            ],
        )


def test_tool_results_resolve_once_within_the_same_step() -> None:
    call = AtifToolCall(tool_call_id="call-1", function_name="lookup", arguments={})
    valid = AtifStep(
        step_id=1,
        source="agent",
        message="",
        tool_calls=[call],
        observation=AtifObservation(
            results=[AtifObservationResult(source_call_id="call-1", content="")]
        ),
    )
    assert AtifTrajectory(agent=agent(), steps=[valid]).steps[0] == valid

    with pytest.raises(ValidationError, match="not found"):
        AtifTrajectory(
            agent=agent(),
            steps=[
                valid.model_copy(
                    update={
                        "observation": AtifObservation(
                            results=[
                                AtifObservationResult(source_call_id="orphan", content="missing")
                            ]
                        )
                    }
                )
            ],
        )
    with pytest.raises(ValidationError, match="duplicate"):
        AtifTrajectory(
            agent=agent(),
            steps=[
                valid.model_copy(
                    update={
                        "observation": AtifObservation(
                            results=[
                                AtifObservationResult(source_call_id="call-1", content="a"),
                                AtifObservationResult(source_call_id="call-1", content="b"),
                            ]
                        )
                    }
                )
            ],
        )


def test_subagents_require_unique_ids_and_resolvable_refs() -> None:
    subagent = AtifTrajectory(
        trajectory_id="sub-1",
        agent=agent("subagent"),
        steps=[AtifStep(step_id=1, source="user", message="subtask")],
    )
    step = AtifStep(
        step_id=1,
        source="agent",
        message="delegated",
        tool_calls=[AtifToolCall(tool_call_id="delegate", function_name="delegate", arguments={})],
        observation=AtifObservation(
            results=[
                AtifObservationResult(
                    source_call_id="delegate",
                    subagent_trajectory_ref=[AtifSubagentTrajectoryRef(trajectory_id="sub-1")],
                )
            ]
        ),
    )
    AtifTrajectory(agent=agent(), steps=[step], subagent_trajectories=[subagent])

    with pytest.raises(ValidationError, match="does not resolve"):
        AtifTrajectory(agent=agent(), steps=[step])
    with pytest.raises(ValidationError, match="not unique"):
        AtifTrajectory(
            agent=agent(),
            steps=[AtifStep(step_id=1, source="user", message="root")],
            subagent_trajectories=[subagent, subagent],
        )


def test_builder_preserves_copied_context_and_complete_instruction() -> None:
    builder = TrajectoryBuilder(
        trajectory_id="trajectory-1",
        session_id="session-1",
        agent=agent(),
    )
    builder.add(source="system", message="system", is_copied_context=True)
    builder.add(source="user", message="full instruction\nwith details")
    built = builder.build()
    assert [step.step_id for step in built.steps] == [1, 2]
    assert built.steps[0].is_copied_context is True
    assert built.steps[1].message == "full instruction\nwith details"
