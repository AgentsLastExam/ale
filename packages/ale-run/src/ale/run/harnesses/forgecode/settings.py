"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PositiveInt = Annotated[int, Field(gt=0, strict=True)]


class ForgecodeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning_effort: Literal[
        "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "default"
    max_output_tokens: PositiveInt | Literal["default"] = "default"
    max_requests_per_turn: PositiveInt | Literal["default"] = "default"
    #: The model's ``input_modalities``.
    input_modalities: tuple[Literal["text", "image"], ...] | Literal["default"] = "default"

    # --- validation ---

    @field_validator("input_modalities")
    @classmethod
    def _modalities(
        cls, value: tuple[str, ...] | Literal["default"]
    ) -> tuple[str, ...] | Literal["default"]:
        if value != "default" and (not value or len(set(value)) != len(value)):
            raise ValueError("input_modalities must name each modality once")
        return value
