"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveInt = Annotated[int, Field(gt=0, strict=True)]
Capability = Literal[
    "thinking",
    "always_thinking",
    "image_in",
    "video_in",
    "audio_in",
    "tool_use",
    "dynamically_loaded_tools",
]


class KimiCodeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Kimi requires every model to declare its context window; there is no native default.
    max_context_size: PositiveInt = 262144
    capabilities: tuple[Capability, ...] = ()
    thinking_effort: Literal["default", "off", "low", "medium", "high", "xhigh", "max"] = "default"
    max_steps_per_turn: PositiveInt | Literal["default", "unlimited"] = "unlimited"

    # --- validation ---

    @model_validator(mode="after")
    def _effort_needs_thinking(self) -> KimiCodeSettings:
        if self.thinking_effort not in {"default", "off"} and not {
            "thinking",
            "always_thinking",
        } & set(self.capabilities):
            raise ValueError("thinking_effort requires the thinking or always_thinking capability")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        return self
