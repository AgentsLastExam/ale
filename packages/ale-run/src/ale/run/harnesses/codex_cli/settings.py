"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class CodexCliSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning_effort: Literal[
        "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "default"
    #: OpenAI's hosted web search, used only when the Task's network is open.
    web_search: bool = True
