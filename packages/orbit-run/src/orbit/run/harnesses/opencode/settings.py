"""Validated settings for the native CLI."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PositiveInt = Annotated[int, Field(gt=0, strict=True)]
Modality = Literal["text", "image", "pdf", "audio", "video"]


class OpenCodeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The model variant OpenCode builds for this effort and the Gateway dialect.
    reasoning_effort: Literal[
        "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "default"
    #: The model's ``limit.context``; OpenCode assumes 200,000 for a model it has no
    #: catalog entry for.
    context_window: PositiveInt | Literal["default"] = "default"
    #: The model's ``limit.output``; OpenCode otherwise asks for 32,000.
    max_output_tokens: PositiveInt | Literal["default"] = "default"
    #: The model's ``capabilities.input``; OpenCode otherwise takes text and images.
    input: tuple[Modality, ...] | Literal["default"] = "default"
    #: The build agent's ``steps``: after that many model steps OpenCode asks for a final
    #: answer without tools.
    max_steps: PositiveInt | Literal["default"] = "default"
    #: The search integration behind the ``websearch`` tool; ``off`` removes the tool.
    web_search: Literal["off", "exa", "firecrawl", "parallel", "tavily", "tinyfish", "random"] = (
        "off"
    )

    # --- validation ---

    @field_validator("input")
    @classmethod
    def _input(
        cls, value: tuple[Modality, ...] | Literal["default"]
    ) -> tuple[Modality, ...] | Literal["default"]:
        if value == "default":
            return value
        if len(set(value)) != len(value):
            raise ValueError("input must name each modality once")
        if "text" not in value:
            # OpenCode offers no session a model that does not take text.
            raise ValueError("input must include text")
        return value
