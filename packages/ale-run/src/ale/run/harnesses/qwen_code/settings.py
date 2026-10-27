"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PositiveInt = Annotated[int, Field(gt=0, strict=True)]
Modality = Literal["image", "pdf", "audio", "video"]


class QwenCodeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_session_turns: PositiveInt | Literal["default", "unlimited"] = "unlimited"
    reasoning_effort: Literal["default", "none", "low", "medium", "high", "xhigh", "max"] = (
        "default"
    )
    context_window_size: PositiveInt | Literal["default"] = "default"
    #: Input modalities the model accepts; ``default`` keeps Qwen's detection by model name.
    modalities: tuple[Modality, ...] | Literal["default"] = "default"

    # --- validation ---

    @field_validator("modalities")
    @classmethod
    def _unique(
        cls, value: tuple[Modality, ...] | Literal["default"]
    ) -> tuple[Modality, ...] | Literal["default"]:
        if value != "default" and len(set(value)) != len(value):
            raise ValueError("modalities must be unique")
        return value
