"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PositiveInt = Annotated[int, Field(gt=0, strict=True)]


class PiCliSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Pi clamps ``xhigh`` and ``max`` to ``high`` for a custom model without a level
    #: map, so only levels it sends as requested are accepted.
    thinking: Literal["default", "off", "minimal", "low", "medium", "high"] = "default"
    context_window: PositiveInt | Literal["default"] = "default"
    max_output_tokens: PositiveInt | Literal["default"] = "default"
    disabled_tools: tuple[str, ...] = ()

    # --- validation ---

    @field_validator("disabled_tools")
    @classmethod
    def _nonempty_tools(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or "," in value for value in values):
            raise ValueError("tool names must be non-empty and contain no comma")
        return values
