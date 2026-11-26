"""The agent's trajectory, as the verifier received it."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Trajectory:
    """The agent's Harbor ATIF v1.8 trajectory, its subagents' embedded in it.

    ``document`` is the ATIF JSON object, for Task code to read, condense and give a Judge
    as it sees fit. ``incomplete`` says why the trajectory is incomplete, or is None when
    it is complete: the agent was interrupted (``agent_interrupted``), its logs were not all
    copied (``collection_failed``) or they could not be converted (``conversion_failed``).
    """

    document: dict[str, Any]
    incomplete: str | None


def load_trajectory() -> Trajectory | None:
    """The agent's trajectory, or None where the episode recorded none, as when its agent
    never started. An oracle's, as in calibration, holds the instruction and its final
    message, its agent named ``oracle``.

    A validation round receives the trajectory up to the turn that ended; ``tests/``, all of
    it; ``orbit reverify``, its source episode's.
    """
    path = os.environ.get("ORBIT_TRAJECTORY_PATH")
    if not path:
        return None
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("steps"), list):
        raise ValueError(f"the trajectory is not an ATIF document: {path}")
    orbit = (document.get("extra") or {}).get("orbit") or {}
    incomplete = (
        str(orbit.get("incomplete_reason") or "incomplete") if orbit.get("incomplete") else None
    )
    return Trajectory(document, incomplete)
