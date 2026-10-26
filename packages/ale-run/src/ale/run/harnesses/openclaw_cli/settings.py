"""Validated settings for the native CLI."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PositiveInt = Annotated[int, Field(gt=0, strict=True)]
_PROVIDER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class OpenClawCliSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = "openai"
    thinking: Literal[
        "default", "off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"
    ] = "default"
    timeout_seconds: PositiveInt | Literal["default", "unlimited"] = "unlimited"
    tool_profile: Literal["minimal", "coding", "messaging", "full"] = "coding"
    tools_allow: tuple[str, ...] = ()
    tools_deny: tuple[str, ...] = ()
    model_params: dict[str, Any] = Field(default_factory=dict)

    # --- validation ---

    @field_validator("provider")
    @classmethod
    def _provider_id(cls, value: str) -> str:
        if not _PROVIDER.fullmatch(value):
            raise ValueError("provider must be an OpenClaw provider identifier")
        return value

    @field_validator("tools_allow", "tools_deny")
    @classmethod
    def _nonempty_tools(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("tool names must be non-empty")
        return values

    @model_validator(mode="after")
    def _allow_list_needs_full_profile(self) -> OpenClawCliSettings:
        if self.tools_allow and self.tool_profile != "full":
            raise ValueError("tools_allow requires tool_profile='full'")
        return self
