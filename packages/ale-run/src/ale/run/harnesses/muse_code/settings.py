"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

PositiveInt = Annotated[int, Field(gt=0, strict=True)]


class MuseCodeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning_effort: Literal[
        "default", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
    ] = "default"
    #: Muse sets no step ceiling unless asked, so ``default`` and ``unlimited`` agree.
    max_model_steps: PositiveInt | Literal["default", "unlimited"] = "unlimited"
    #: Muse's reminder agents, which make model calls of their own beside the main loop.
    reminders: StrictBool = True
    #: Muse's web tools; ALE turns them off unless the Task's network is open.
    web_tools: StrictBool = True
