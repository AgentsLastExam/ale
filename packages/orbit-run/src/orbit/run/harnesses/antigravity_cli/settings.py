"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class AntigravityCliSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    effort: Literal["default", "low", "medium", "high"] = "high"
