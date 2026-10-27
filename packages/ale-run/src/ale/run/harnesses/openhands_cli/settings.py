"""Validated settings for the native CLI."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OpenHandsCliSettings(BaseModel):
    """OpenHands CLI headless mode takes its model only from the environment, so nothing
    else about it is configurable without replacing the CLI's own agent definition."""

    model_config = ConfigDict(frozen=True, extra="forbid")
