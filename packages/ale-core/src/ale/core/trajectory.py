"""Strict local Harbor ATIF v1.7 trajectory models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AtifAgent",
    "AtifContentPart",
    "AtifFinalMetrics",
    "AtifImageSource",
    "AtifMetrics",
    "AtifObservation",
    "AtifObservationResult",
    "AtifStep",
    "AtifSubagentTrajectoryRef",
    "AtifToolCall",
    "AtifTrajectory",
    "TrajectoryBuilder",
]

_STRICT = ConfigDict(frozen=True, extra="forbid")


class AtifAgent(BaseModel):
    model_config = _STRICT

    name: str
    version: str
    model_name: str | None = None
    tool_definitions: list[dict[str, Any]] | None = None
    extra: dict[str, Any] | None = None


class AtifImageSource(BaseModel):
    model_config = _STRICT

    media_type: Literal["image/jpeg", "image/png", "image/gif", "image/webp"]
    path: str


class AtifContentPart(BaseModel):
    model_config = _STRICT

    type: Literal["text", "image"]
    text: str | None = None
    source: AtifImageSource | None = None

    @model_validator(mode="after")
    def _matching_content(self) -> Self:
        if self.type == "text" and (self.text is None or self.source is not None):
            raise ValueError("text content requires text and forbids source")
        if self.type == "image" and (self.source is None or self.text is not None):
            raise ValueError("image content requires source and forbids text")
        return self


class AtifMetrics(BaseModel):
    model_config = _STRICT

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    cost_usd: float | None = None
    prompt_token_ids: list[int] | None = None
    completion_token_ids: list[int] | None = None
    logprobs: list[float] | None = None
    extra: dict[str, Any] | None = None


class AtifFinalMetrics(BaseModel):
    model_config = _STRICT

    total_prompt_tokens: int | None = None
    total_completion_tokens: int | None = None
    total_cached_tokens: int | None = None
    total_cost_usd: float | None = None
    total_steps: int | None = Field(default=None, ge=0)
    extra: dict[str, Any] | None = None


class AtifSubagentTrajectoryRef(BaseModel):
    model_config = _STRICT

    trajectory_id: str | None = None
    session_id: str | None = None
    trajectory_path: str | None = None
    extra: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _resolvable(self) -> Self:
        if self.trajectory_id is None and self.trajectory_path is None:
            raise ValueError("set trajectory_id or trajectory_path")
        return self


class AtifToolCall(BaseModel):
    model_config = _STRICT

    tool_call_id: str
    function_name: str
    arguments: dict[str, Any]
    extra: dict[str, Any] | None = None


class AtifObservationResult(BaseModel):
    model_config = _STRICT

    source_call_id: str | None = None
    content: str | list[AtifContentPart] | None = None
    subagent_trajectory_ref: list[AtifSubagentTrajectoryRef] | None = None
    extra: dict[str, Any] | None = None


class AtifObservation(BaseModel):
    model_config = _STRICT

    results: list[AtifObservationResult]


class AtifStep(BaseModel):
    model_config = _STRICT

    step_id: int = Field(ge=1)
    timestamp: str | None = None
    source: Literal["system", "user", "agent"]
    model_name: str | None = None
    reasoning_effort: str | float | None = None
    message: str | list[AtifContentPart]
    reasoning_content: str | None = None
    tool_calls: list[AtifToolCall] | None = None
    observation: AtifObservation | None = None
    metrics: AtifMetrics | None = None
    is_copied_context: bool | None = None
    llm_call_count: int | None = Field(default=None, ge=0)
    extra: dict[str, Any] | None = None

    @field_validator("timestamp")
    @classmethod
    def _timestamp_is_iso8601(cls, value: str | None) -> str | None:
        if value is not None:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @model_validator(mode="after")
    def _agent_only_fields(self) -> Self:
        if self.source != "agent":
            for name in (
                "model_name",
                "reasoning_effort",
                "reasoning_content",
                "tool_calls",
                "metrics",
            ):
                if getattr(self, name) is not None:
                    raise ValueError(f"{name} is only valid for agent steps")
        if (
            self.source == "agent"
            and self.llm_call_count == 0
            and (self.metrics is not None or self.reasoning_content is not None)
        ):
            raise ValueError("metrics and reasoning_content require an LLM-backed agent step")
        return self


class AtifTrajectory(BaseModel):
    model_config = _STRICT

    schema_version: Literal["ATIF-v1.7"] = "ATIF-v1.7"
    session_id: str | None = None
    trajectory_id: str | None = None
    agent: AtifAgent
    steps: list[AtifStep] = Field(min_length=1)
    notes: str | None = None
    final_metrics: AtifFinalMetrics | None = None
    continued_trajectory_ref: str | None = None
    extra: dict[str, Any] | None = None
    subagent_trajectories: list[AtifTrajectory] | None = None

    @model_validator(mode="after")
    def _relationships(self) -> Self:
        for index, step in enumerate(self.steps, start=1):
            if step.step_id != index:
                raise ValueError(
                    f"steps[{index - 1}].step_id must be sequential from 1; "
                    f"expected {index}, got {step.step_id}"
                )
            call_ids = [call.tool_call_id for call in step.tool_calls or ()]
            if len(call_ids) != len(set(call_ids)):
                raise ValueError(f"step {step.step_id} has duplicate tool call IDs")
            result_ids = [
                result.source_call_id
                for result in (step.observation.results if step.observation else ())
                if result.source_call_id is not None
            ]
            if len(result_ids) != len(set(result_ids)):
                raise ValueError(f"step {step.step_id} has duplicate tool results")
            for result_id in result_ids:
                if result_id not in call_ids:
                    raise ValueError(
                        f"Observation source_call_id {result_id!r} is not found "
                        f"in step {step.step_id} tool calls"
                    )

        embedded = self.subagent_trajectories or ()
        embedded_ids = [trajectory.trajectory_id for trajectory in embedded]
        if any(identifier is None for identifier in embedded_ids):
            raise ValueError("embedded subagent trajectory_id is required")
        if len(embedded_ids) != len(set(embedded_ids)):
            raise ValueError("embedded subagent trajectory_id is not unique")
        known_ids = set(embedded_ids)
        for step in self.steps:
            for result in step.observation.results if step.observation else ():
                for ref in result.subagent_trajectory_ref or ():
                    if ref.trajectory_path is None and ref.trajectory_id not in known_ids:
                        raise ValueError(
                            f"subagent trajectory_id {ref.trajectory_id!r} does not resolve"
                        )
        return self

    def to_json_dict(self, *, exclude_none: bool = True) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=exclude_none)


class TrajectoryBuilder:
    """Assign contiguous step IDs and validate once at finalization."""

    def __init__(
        self,
        *,
        agent: AtifAgent,
        trajectory_id: str,
        session_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.agent = agent
        self.trajectory_id = trajectory_id
        self.session_id = session_id
        self.extra = extra
        self._steps: list[AtifStep] = []

    @property
    def steps(self) -> tuple[AtifStep, ...]:
        return tuple(self._steps)

    def add(
        self,
        *,
        source: Literal["system", "user", "agent"],
        message: Any,
        **data: Any,
    ) -> AtifStep:
        step = AtifStep(
            step_id=len(self._steps) + 1,
            source=source,
            message=message,
            **data,
        )
        self._steps.append(step)
        return step

    def append(self, step: AtifStep) -> AtifStep:
        normalized = step.model_copy(update={"step_id": len(self._steps) + 1})
        self._steps.append(normalized)
        return normalized

    def build(self, **data: Any) -> AtifTrajectory:
        return AtifTrajectory(
            session_id=self.session_id,
            trajectory_id=self.trajectory_id,
            agent=self.agent,
            steps=self._steps,
            extra=self.extra,
            **data,
        )
