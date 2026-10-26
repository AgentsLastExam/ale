"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PositiveInt = Annotated[int, Field(gt=0, strict=True)]


class ClaudeCodeSettings(BaseModel):
    """The complete ALE-supported Claude Code configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: PositiveInt | Literal["default", "unlimited"] = "unlimited"
    permission_mode: Literal[
        "default",
        "manual",
        "acceptEdits",
        "plan",
        "auto",
        "dontAsk",
        "bypassPermissions",
    ] = "bypassPermissions"
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    append_system_prompt: str | Literal["default"] = "default"
    effort: Literal["default", "low", "medium", "high", "xhigh", "max", "ultracode"] = "default"

    # --- validation ---

    @field_validator("allowed_tools", "disallowed_tools")
    @classmethod
    def _nonempty_tools(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("tool names must be non-empty")
        return values

    @field_validator("append_system_prompt")
    @classmethod
    def _nonempty_prompt(cls, value: str) -> str:
        if not value:
            raise ValueError("append_system_prompt must be non-empty or 'default'")
        return value
