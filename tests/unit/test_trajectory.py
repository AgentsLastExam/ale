"""Canonical Harbor ATIF v1.8 trajectory contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ale.core.trajectory import (
    AtifAgent,
    AtifAudioSource,
    AtifContentPart,
    AtifImageSource,
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


def test_exact_v18_root_and_step_fields_reject_unknowns() -> None:
    trajectory = AtifTrajectory(
        trajectory_id="root",
        agent=agent(),
        steps=[AtifStep(step_id=1, source="user", message="hello")],
    )
    assert trajectory.schema_version == "ATIF-v1.8"
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


@pytest.mark.parametrize(
    ("observed", "canonical"),
    [
        ("audio/wav", "audio/wav"),
        ("audio/mpeg", "audio/mpeg"),
        ("audio/mp4", "audio/mp4"),
        ("audio/aac", "audio/aac"),
        ("audio/ogg", "audio/ogg"),
        ("audio/flac", "audio/flac"),
        ("audio/webm", "audio/webm"),
        ("audio/aiff", "audio/aiff"),
        (" AUDIO/MP3 ", "audio/mpeg"),
        ("audio/mpga", "audio/mpeg"),
        ("audio/x-mpeg", "audio/mpeg"),
        ("audio/x-wav", "audio/wav"),
        ("audio/wave", "audio/wav"),
        ("audio/vnd.wave", "audio/wav"),
        ("audio/x-m4a", "audio/mp4"),
        ("audio/m4a", "audio/mp4"),
        ("audio/x-aac", "audio/aac"),
        ("audio/x-flac", "audio/flac"),
        ("audio/x-aiff", "audio/aiff"),
    ],
)
def test_audio_mime_types_normalize_like_harbor(observed: str, canonical: str) -> None:
    source = AtifAudioSource.model_validate(
        {"media_type": observed, "path": "blobs/audio/recording", "duration_sec": 1.25}
    )
    part = AtifContentPart(type="audio", source=source)
    document = AtifTrajectory(
        agent=agent(),
        steps=[AtifStep(step_id=1, source="user", message=[part])],
    )
    restored = AtifTrajectory.model_validate_json(document.model_dump_json())
    assert restored == document
    assert source.media_type == canonical
    assert source.duration_sec == 1.25


@pytest.mark.parametrize("media_type", ["audio/pcm", "audio/g711", "video/mp4"])
def test_audio_rejects_formats_outside_atif(media_type: str) -> None:
    with pytest.raises(ValidationError):
        AtifAudioSource.model_validate({"media_type": media_type, "path": "recording"})


def test_audio_duration_is_optional_and_nonnegative() -> None:
    source = AtifAudioSource(media_type="audio/wav", path="recording.wav")
    assert "duration_sec" not in source.model_dump(exclude_none=True)
    assert source.model_validate(source.model_dump() | {"duration_sec": 0}).duration_sec == 0
    with pytest.raises(ValidationError):
        source.model_validate(source.model_dump() | {"duration_sec": -0.1})


@pytest.mark.parametrize(
    "part",
    [
        {"type": "audio"},
        {"type": "audio", "source": {"media_type": "image/png", "path": "image.png"}},
        {"type": "image", "source": {"media_type": "audio/wav", "path": "audio.wav"}},
        {"type": "text", "text": "x", "source": {"media_type": "audio/wav", "path": "a.wav"}},
        {"type": "audio", "text": "x", "source": {"media_type": "audio/wav", "path": "a.wav"}},
    ],
)
def test_content_rejects_missing_or_mismatched_media(part: dict) -> None:
    with pytest.raises(ValidationError):
        AtifContentPart.model_validate(part)


def test_image_content_remains_valid() -> None:
    image = AtifImageSource(media_type="image/png", path="image.png")
    assert AtifContentPart(type="image", source=image).source == image
