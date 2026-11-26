from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orbit_verify import Trajectory, load_trajectory


def document(**extra: Any) -> dict[str, Any]:
    return {
        "schema_version": "ATIF-v1.8",
        "agent": {"name": "scripted", "version": "1"},
        "steps": [{"step_id": 1, "source": "user", "message": "Solve it."}],
        **extra,
    }


def staged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: object) -> None:
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(value))
    monkeypatch.setenv("ORBIT_TRAJECTORY_PATH", str(path))


def test_no_trajectory_is_none_rather_than_an_empty_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORBIT_TRAJECTORY_PATH", raising=False)
    assert load_trajectory() is None


def test_a_complete_trajectory_is_its_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staged(monkeypatch, tmp_path, document())
    assert load_trajectory() == Trajectory(document(), None)


@pytest.mark.parametrize("reason", ["agent_interrupted", "collection_failed", "conversion_failed"])
def test_an_incomplete_trajectory_says_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str
) -> None:
    marked = document(extra={"orbit": {"incomplete": True, "incomplete_reason": reason}})
    staged(monkeypatch, tmp_path, marked)
    trajectory = load_trajectory()
    assert trajectory is not None
    assert (trajectory.document, trajectory.incomplete) == (marked, reason)


def test_a_file_that_is_no_trajectory_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staged(monkeypatch, tmp_path, ["not", "a", "trajectory"])
    with pytest.raises(ValueError, match="ATIF"):
        load_trajectory()
