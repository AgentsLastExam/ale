"""The canonical ALE document is accepted directly by the adjacent Harbor checkout."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from tests.support import sibling_checkout

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
)

pytestmark = pytest.mark.integration

HARBOR = sibling_checkout("harbor")
SCHEMA_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/atif-v1.8.schema.json"


def test_harbor_schema_fixture_matches_adjacent_checkout() -> None:
    if not (HARBOR / "pyproject.toml").is_file():
        pytest.skip("adjacent Harbor checkout is unavailable")
    command = (
        "import json; "
        "from harbor.models.trajectories import Trajectory; "
        "print(json.dumps(Trajectory.model_json_schema(), sort_keys=True))"
    )
    completed = subprocess.run(
        ["uv", "run", "--project", str(HARBOR), "python", "-c", command],
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == json.loads(SCHEMA_FIXTURE.read_text())


def test_harbor_accepts_tools_images_audio_subagents_and_continuation(tmp_path: Path) -> None:
    if not (HARBOR / "pyproject.toml").is_file():
        pytest.skip("adjacent Harbor checkout is unavailable")
    image = tmp_path / "blobs/image/image.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    audio = tmp_path / "blobs/audio/question.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    audio_part = AtifContentPart(
        type="audio",
        source=AtifAudioSource(
            media_type="audio/wav",
            path="blobs/audio/question.wav",
            duration_sec=1.25,
        ),
    )
    subagent = AtifTrajectory(
        trajectory_id="subagent-1",
        agent=AtifAgent(name="subagent", version="1"),
        steps=[AtifStep(step_id=1, source="user", message="research")],
    )
    trajectory = AtifTrajectory(
        trajectory_id="root",
        session_id="session",
        continued_trajectory_ref="segments/next.json",
        agent=AtifAgent(name="ale", version="1", model_name="model"),
        steps=[
            AtifStep(
                step_id=1,
                source="user",
                message=[
                    AtifContentPart(type="text", text="inspect"),
                    AtifContentPart(
                        type="image",
                        source=AtifImageSource(
                            media_type="image/png",
                            path="blobs/image/image.png",
                        ),
                    ),
                    audio_part,
                ],
            ),
            AtifStep(
                step_id=2,
                source="agent",
                message=[AtifContentPart(type="text", text="delegating"), audio_part],
                tool_calls=[
                    AtifToolCall(
                        tool_call_id="delegate",
                        function_name="Task",
                        arguments={"prompt": "research"},
                    )
                ],
                observation=AtifObservation(
                    results=[
                        AtifObservationResult(
                            source_call_id="delegate",
                            content=[AtifContentPart(type="text", text="done"), audio_part],
                            subagent_trajectory_ref=[
                                AtifSubagentTrajectoryRef(trajectory_id="subagent-1")
                            ],
                        )
                    ]
                ),
            ),
        ],
        subagent_trajectories=[subagent],
    )
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(trajectory.model_dump(mode="json", exclude_none=True)))
    command = (
        "from harbor.utils.trajectory_validator import TrajectoryValidator; "
        "from harbor.models.trajectories import Trajectory; "
        f"v=TrajectoryValidator(); ok=v.validate({str(path)!r}); "
        "assert ok, v.get_errors(); "
        f"t=Trajectory.model_validate_json(open({str(path)!r}).read()); "
        "assert t.schema_version == 'ATIF-v1.8'; "
        "assert t.has_multimodal_content(); "
        "assert t.steps[0].message[-1].source.duration_sec == 1.25"
    )
    completed = subprocess.run(
        ["uv", "run", "--project", str(HARBOR), "python", "-c", command],
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
