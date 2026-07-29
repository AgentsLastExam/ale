"""Loss-aware exports from ALE's canonical episode records."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from ale.core.errors import IncompleteTrainingDataError
from ale.core.result import ResultRecord
from ale.core.trajectory import (
    AtifContentPart,
    AtifStep,
    AtifTrajectory,
)

__all__ = [
    "PrimeRlSample",
    "harbor_result",
    "harbor_trajectory",
    "prime_rl_samples",
    "prime_verifiers_record",
    "scalar_reward",
]


class PrimeRlSample(BaseModel):
    """The Prime-RL TrainingSample fields ALE can export without importing Prime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_ids: list[int]
    mask: list[bool]
    logprobs: list[float]
    temperatures: list[float]
    env_name: str
    mm_kwargs: dict[str, Any] | None = None
    mm_token_type_ids: list[int] | None = None


def harbor_trajectory(trajectory: AtifTrajectory) -> dict[str, Any]:
    """Harbor consumes the canonical ATIF document directly."""
    return trajectory.model_dump(mode="json", exclude_none=True)


def harbor_result(result: ResultRecord) -> dict[str, Any]:
    """Preserve Harbor's reward map without selecting a headline reward."""
    return {"rewards": dict(result.rewards)} if result.rewards is not None else {"rewards": None}


def scalar_reward(
    result: ResultRecord,
    aggregate: Callable[[dict[str, float]], float],
) -> float:
    if result.rewards is None:
        raise ValueError("failed episodes have no rewards to aggregate")
    return float(aggregate(dict(result.rewards)))


def prime_verifiers_record(
    trajectory: AtifTrajectory,
    result: ResultRecord,
    transport: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build a Prime Verifiers v1 wire record without importing that optional project."""
    nodes: list[dict[str, Any]] = []
    step_nodes: dict[int, int] = {}
    parent: int | None = None
    for step in trajectory.steps:
        parent, step_node = _append_step(nodes, step, parent)
        step_nodes[step.step_id] = step_node

    embedded = {
        item.trajectory_id: item
        for item in trajectory.subagent_trajectories or ()
        if item.trajectory_id is not None
    }
    for step in trajectory.steps:
        branch_parent = step_nodes[step.step_id]
        refs = [
            ref
            for observation in (step.observation.results if step.observation else ())
            for ref in observation.subagent_trajectory_ref or ()
        ]
        for ref in refs:
            subagent = embedded.get(ref.trajectory_id)
            if subagent is None:
                continue
            sub_parent = branch_parent
            for sub_step in subagent.steps:
                sub_parent, _ = _append_step(nodes, sub_step, sub_parent)

    links = {
        record["call_id"]: record["step_id"]
        for record in transport
        if record.get("kind") == "trajectory_link"
    }
    calls = [
        _prime_call(record, step_nodes.get(links.get(record.get("call_id"))))
        for record in transport
        if record.get("kind") == "call"
    ]
    prompt = next(
        (_text(step.message) for step in trajectory.steps if step.source == "user"),
        "",
    )
    return {
        "id": trajectory.trajectory_id or result.episode_id,
        "task": {
            "type": "AleTask",
            "data": {"name": result.episode_id, "prompt": prompt},
        },
        "agent": {
            "model": trajectory.agent.model_name or "",
            "name": trajectory.agent.name,
        },
        "nodes": nodes,
        "calls": calls,
        "rewards": dict(result.rewards or {}),
        "metrics": dict(result.metrics),
        "info": {
            "ale": {
                "trajectory_id": trajectory.trajectory_id,
                "phases": [
                    phase.model_dump(mode="json", exclude_none=True) for phase in result.phases
                ],
            }
        },
        "is_completed": True,
        "ok": result.status.value == "completed",
        "stop_condition": result.status.value,
        "errors": (
            []
            if result.failure is None
            else [
                {
                    "type": result.failure.error_type,
                    "message": result.failure.message,
                }
            ]
        ),
    }


def prime_rl_samples(
    trajectory: AtifTrajectory,
    *,
    env_name: str = "",
) -> list[PrimeRlSample]:
    """Export exact observed training evidence; never retokenize ATIF text."""
    samples: list[PrimeRlSample] = []
    context_has_images = False
    for step in trajectory.steps:
        context_has_images = context_has_images or _has_images(step)
        if step.source != "agent" or step.metrics is None:
            continue
        metrics = step.metrics
        prompt = metrics.prompt_token_ids
        completion = metrics.completion_token_ids
        logprobs = metrics.logprobs
        ale = (metrics.extra or {}).get("ale") or {}
        mask = ale.get("sampled_mask")
        temperatures = ale.get("temperatures")
        if temperatures is None and isinstance(ale.get("temperature"), int | float):
            temperatures = [float(ale["temperature"])] * (len(prompt or ()) + len(completion or ()))
        missing = [
            name
            for name, value in (
                ("prompt_token_ids", prompt),
                ("completion_token_ids", completion),
                ("logprobs", logprobs),
                ("sampled_mask", mask),
                ("temperatures", temperatures),
            )
            if value is None
        ]
        if missing:
            raise IncompleteTrainingDataError(
                f"step {step.step_id} lacks exact {', '.join(missing)}"
            )
        assert prompt is not None and completion is not None and logprobs is not None
        token_ids = [*prompt, *completion]
        if len(logprobs) != len(completion):
            raise IncompleteTrainingDataError(
                f"step {step.step_id} logprobs do not align with completion tokens"
            )
        if not isinstance(mask, list) or len(mask) != len(token_ids):
            raise IncompleteTrainingDataError(
                f"step {step.step_id} sampled_mask does not align with token IDs"
            )
        if sum(bool(value) for value in mask) != len(completion):
            raise IncompleteTrainingDataError(
                f"step {step.step_id} sampled_mask does not identify the completion"
            )
        if not isinstance(temperatures, list) or len(temperatures) != len(token_ids):
            raise IncompleteTrainingDataError(
                f"step {step.step_id} temperatures do not align with token IDs"
            )
        mm_kwargs = ale.get("mm_kwargs")
        mm_token_type_ids = ale.get("mm_token_type_ids")
        if context_has_images and (mm_kwargs is None or mm_token_type_ids is None):
            raise IncompleteTrainingDataError(
                f"step {step.step_id} lacks exact multimodal processor data"
            )
        if mm_token_type_ids is not None and len(mm_token_type_ids) != len(token_ids):
            raise IncompleteTrainingDataError(
                f"step {step.step_id} multimodal token types do not align"
            )
        samples.append(
            PrimeRlSample(
                token_ids=token_ids,
                mask=[bool(value) for value in mask],
                logprobs=[0.0] * len(prompt) + [float(value) for value in logprobs],
                temperatures=[float(value) for value in temperatures],
                env_name=env_name,
                mm_kwargs=mm_kwargs,
                mm_token_type_ids=mm_token_type_ids,
            )
        )
    if not samples:
        raise IncompleteTrainingDataError("trajectory contains no exact trainable steps")
    return samples


def _append_step(
    nodes: list[dict[str, Any]],
    step: AtifStep,
    parent: int | None,
) -> tuple[int, int]:
    role = "assistant" if step.source == "agent" else step.source
    message: dict[str, Any] = {"role": role, "content": _prime_content(step.message)}
    if step.source == "agent":
        message["content"] = _text(step.message) or None
        if step.reasoning_content is not None:
            message["reasoning_content"] = step.reasoning_content
        if step.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.tool_call_id,
                    "name": call.function_name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False, sort_keys=True),
                }
                for call in step.tool_calls
            ]
    nodes.append(
        {
            "parent": parent,
            "message": message,
            "sampled": step.source == "agent" and step.llm_call_count != 0,
            "timestamp": _timestamp(step.timestamp),
        }
    )
    step_node = parent = len(nodes) - 1
    for observation in step.observation.results if step.observation else ():
        if observation.source_call_id is None:
            continue
        nodes.append(
            {
                "parent": parent,
                "message": {
                    "role": "tool",
                    "tool_call_id": observation.source_call_id,
                    "content": _prime_content(observation.content or ""),
                },
                "sampled": False,
                "timestamp": _timestamp(step.timestamp),
            }
        )
        parent = len(nodes) - 1
    return parent, step_node


def _prime_call(record: dict[str, Any], node: int | None) -> dict[str, Any]:
    timestamp = _timestamp(record.get("timestamp"))
    latency = max(float(record.get("latency_ms") or 0) / 1000, 0.0)
    stop = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }.get(record.get("stop_reason"))
    cached = int(record.get("cached_tokens") or 0)
    usage = None
    if record.get("disposition") == "forwarded":
        usage = {
            "prompt_tokens": max(int(record.get("input_tokens") or 0) - cached, 0),
            "completion_tokens": int(record.get("output_tokens") or 0),
            "cached_input_tokens": cached or None,
            "cost": record.get("cost_usd"),
        }
    error = None
    if record.get("disposition") != "forwarded":
        error = {
            "type": "GatewayRefusal" if record.get("disposition") == "refused" else "ProviderError",
            "message": record.get("refusal_limit")
            or f"upstream status {record.get('upstream_status')}",
            "status_code": record.get("upstream_status"),
        }
    return {
        "node": node,
        "model": record.get("model"),
        "finish_reason": stop,
        "usage": usage,
        "time": {"start": timestamp, "end": timestamp + latency},
        "error": error,
    }


def _prime_content(value: str | list[AtifContentPart]) -> Any:
    if isinstance(value, str):
        return value
    return [
        (
            {"type": "text", "text": part.text or ""}
            if part.type == "text"
            else {
                "type": "image_url",
                "image_url": {"url": part.source.path},
            }
        )
        for part in value
    ]


def _text(value: str | list[AtifContentPart]) -> str:
    if isinstance(value, str):
        return value
    return "\n".join(part.text or "" for part in value if part.type == "text")


def _timestamp(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _has_images(step: AtifStep) -> bool:
    values = [step.message]
    values.extend(
        result.content
        for result in (step.observation.results if step.observation else ())
        if result.content is not None
    )
    return any(
        isinstance(value, list) and any(part.type == "image" for part in value) for value in values
    )
