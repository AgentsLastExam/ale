"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class ZCodeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    disallowed_tools: tuple[str, ...] = ()
    workflow_mode: Literal["default", "disabled", "onDemand", "alwaysOn"] = "default"
    toolsearch_mode: Literal["default", "auto", "native", "local", "off"] = "default"

    # --- validation ---

    @field_validator("disallowed_tools")
    @classmethod
    def _plain_tool_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        # ZCode splits the list on commas and spaces.
        if any(not value or any(c in value for c in ", \t\n") for value in values):
            raise ValueError("tool names must be non-empty and contain no commas or spaces")
        return values
