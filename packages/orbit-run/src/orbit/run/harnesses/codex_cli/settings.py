"""Validated settings for the native CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class CodexCliSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning_effort: Literal[
        "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "default"
    #: OpenAI's hosted web search, used only when the Task's network is open.
    web_search: bool = True
    #: A Host-local official Codex 0.170.0 Linux x86-64 package archive.
    native_package_archive: Path | None = None
