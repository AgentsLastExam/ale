"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

PositiveInt = Annotated[int, Field(gt=0, strict=True)]


class ForgecodeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning_effort: Literal[
        "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "default"
    max_output_tokens: PositiveInt | Literal["default"] = "default"
    max_requests_per_turn: PositiveInt | Literal["default"] = "default"
