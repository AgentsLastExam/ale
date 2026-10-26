"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

PositiveInt = Annotated[int, Field(gt=0, strict=True)]


class HermesSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: PositiveInt | Literal["default"] = "default"
    reasoning_effort: Literal[
        "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "default"
