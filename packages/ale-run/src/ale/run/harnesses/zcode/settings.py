"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool, field_validator, model_validator


class InputFormat(BaseModel):
    """What a model takes besides text, as ZCode's ``inputFormat`` names it; unset keeps
    what ZCode's catalog says of the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    supports_image: StrictBool | None = None
    supports_pdf: StrictBool | None = None
    supports_audio: StrictBool | None = None
    supports_video: StrictBool | None = None

    @property
    def native(self) -> dict[str, bool]:
        names = {
            "supports_image": "supportsImage",
            "supports_pdf": "supportsPdf",
            "supports_audio": "supportsAudio",
            "supports_video": "supportsVideo",
        }
        values = self.model_dump(exclude_none=True)
        return {names[key]: value for key, value in values.items()}

    # --- validation ---

    @model_validator(mode="after")
    def _something(self) -> InputFormat:
        if not self.model_dump(exclude_none=True):
            raise ValueError("input_format must set at least one input")
        return self


class ZCodeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    disallowed_tools: tuple[str, ...] = ()
    workflow_mode: Literal["default", "disabled", "onDemand", "alwaysOn"] = "default"
    toolsearch_mode: Literal["default", "auto", "native", "local", "off"] = "default"
    #: Overlaid on the catalog's ``inputFormat`` for the run's model.
    input_format: InputFormat | Literal["default"] = "default"

    # --- validation ---

    @field_validator("disallowed_tools")
    @classmethod
    def _plain_tool_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        # ZCode splits the list on commas and spaces.
        if any(not value or any(c in value for c in ", \t\n") for value in values):
            raise ValueError("tool names must be non-empty and contain no commas or spaces")
        return values
