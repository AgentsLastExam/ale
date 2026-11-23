"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PositiveInt = Annotated[int, Field(gt=0, strict=True)]


class GrokBuildSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: PositiveInt | Literal["default", "unlimited"] = "unlimited"
    reasoning_effort: Literal[
        "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "high"
    disabled_tools: tuple[str, ...] = ()

    # --- validation ---

    @field_validator("disabled_tools")
    @classmethod
    def _nonempty_tools(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("tool names must be non-empty")
        return values
