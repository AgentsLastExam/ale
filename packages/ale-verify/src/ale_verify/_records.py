"""Strict, dependency-free verification record values."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, Self

HASH_PREFIX = "sha256:"
MAX_DIAGNOSTIC_CHARS = 2_000
MAX_REASONING_CHARS = 10_000
MAX_RAW_JSON_BYTES = 64 * 1024


def _name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("names must be non-empty strings")
    return value


def _score(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("scores must be finite and between 0 and 1")
    return value


def _weight(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("weights must be finite and positive")
    return value


def _finite(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("values must be finite")
    return value


def _bounded(value: str | None, limit: int, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if len(value) > limit:
        raise ValueError(f"{field_name} exceeds {limit} characters")
    return value


def _hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(HASH_PREFIX)
        or len(value) != len(HASH_PREFIX) + 64
    ):
        raise ValueError("hashes must use sha256:<64 lowercase hex>")
    try:
        int(value.removeprefix(HASH_PREFIX), 16)
    except ValueError as exc:
        raise ValueError("hashes must use sha256:<64 lowercase hex>") from exc
    if value != value.lower():
        raise ValueError("hashes must use lowercase hex")
    return value


def _json_value(value: Any) -> Any:
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("raw_value must be valid finite JSON") from exc
    if len(payload.encode("utf-8")) > MAX_RAW_JSON_BYTES:
        raise ValueError(f"raw_value exceeds {MAX_RAW_JSON_BYTES} bytes")
    return value


def _timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("timestamps must be strings")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamps must be ISO 8601 date-times") from exc
    return value


@dataclass(frozen=True)
class ScoredChoice:
    score: float
    description: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _score(self.score))
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("choice descriptions must be non-empty strings")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        _keys(value, {"score", "description"})
        return cls(score=value["score"], description=value["description"])


@dataclass(frozen=True)
class CheckResult:
    score: float
    diagnostic: str = ""
    raw_value: Any = None
    evidence: tuple[EvidenceReference, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _score(self.score))
        object.__setattr__(
            self,
            "diagnostic",
            _bounded(self.diagnostic, MAX_DIAGNOSTIC_CHARS, "diagnostic") or "",
        )
        object.__setattr__(self, "raw_value", _json_value(self.raw_value))
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class EvidenceReference:
    kind: Literal[
        "file",
        "reference",
        "task_instruction",
        "task_parameters",
        "solver_trajectory",
    ]
    location: str | None
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if self.kind not in {
            "file",
            "reference",
            "task_instruction",
            "task_parameters",
            "solver_trajectory",
        }:
            raise ValueError("unknown evidence kind")
        if self.location is not None and not isinstance(self.location, str):
            raise ValueError("evidence location must be a string or null")
        object.__setattr__(self, "sha256", _hash(self.sha256))
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("evidence size_bytes must be a non-negative integer")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        _keys(value, {"kind", "location", "sha256", "size_bytes"})
        return cls(**value)


@dataclass(frozen=True)
class CriterionResult:
    name: str
    source: Literal["check", "llm_judge", "agent_judge"]
    score: float
    weight: float = 1.0
    reasoning: str | None = None
    raw_value: Any = None
    evidence: tuple[EvidenceReference, ...] = ()
    judge_invocation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name))
        if self.source not in {"check", "llm_judge", "agent_judge"}:
            raise ValueError("unknown criterion source")
        object.__setattr__(self, "score", _score(self.score))
        object.__setattr__(self, "weight", _weight(self.weight))
        object.__setattr__(
            self, "reasoning", _bounded(self.reasoning, MAX_REASONING_CHARS, "reasoning")
        )
        object.__setattr__(self, "raw_value", _json_value(self.raw_value))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        judged = self.source != "check"
        if judged != (self.judge_invocation_id is not None):
            raise ValueError("Judge criteria require an invocation ID; checks forbid one")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        _keys(
            value,
            {
                "name",
                "source",
                "score",
                "weight",
                "reasoning",
                "raw_value",
                "evidence",
                "judge_invocation_id",
            },
        )
        return cls(
            **{
                **value,
                "evidence": tuple(EvidenceReference.from_dict(item) for item in value["evidence"]),
            }
        )


@dataclass(frozen=True)
class AggregateResult:
    name: str
    method: Literal["mean", "weighted_mean"]
    inputs: dict[str, float]
    weights: dict[str, float]
    score: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name))
        if self.method not in {"mean", "weighted_mean"}:
            raise ValueError("unknown aggregate method")
        if not self.inputs or set(self.inputs) != set(self.weights):
            raise ValueError("aggregate input and weight keys must match and be non-empty")
        object.__setattr__(
            self, "inputs", {_name(name): _score(value) for name, value in self.inputs.items()}
        )
        object.__setattr__(
            self, "weights", {_name(name): _weight(value) for name, value in self.weights.items()}
        )
        object.__setattr__(self, "score", _score(self.score))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        _keys(value, {"name", "method", "inputs", "weights", "score"})
        return cls(**value)


@dataclass(frozen=True)
class JudgeAttempt:
    index: int
    mode: Literal["initial", "retry", "schema_repair"]
    started_at: str
    finished_at: str
    outcome: Literal["completed", "invalid", "refused", "timed_out", "failed"]
    model: str
    reasoning_effort: str
    endpoint_identity: str
    prompt_hash: str
    rubric_hash: str
    request_id: str | None = None
    usage: dict[str, int] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.index, int) or self.index < 1:
            raise ValueError("attempt index must be positive")
        if self.mode not in {"initial", "retry", "schema_repair"}:
            raise ValueError("unknown attempt mode")
        if self.outcome not in {"completed", "invalid", "refused", "timed_out", "failed"}:
            raise ValueError("unknown attempt outcome")
        object.__setattr__(self, "started_at", _timestamp(self.started_at))
        object.__setattr__(self, "finished_at", _timestamp(self.finished_at))
        if datetime.fromisoformat(self.finished_at.replace("Z", "+00:00")) < datetime.fromisoformat(
            self.started_at.replace("Z", "+00:00")
        ):
            raise ValueError("attempt finish precedes start")
        for field_name in ("model", "reasoning_effort", "endpoint_identity"):
            _name(getattr(self, field_name))
        object.__setattr__(self, "prompt_hash", _hash(self.prompt_hash))
        object.__setattr__(self, "rubric_hash", _hash(self.rubric_hash))
        if self.usage is not None:
            if any(not isinstance(value, int) or value < 0 for value in self.usage.values()):
                raise ValueError("usage values must be non-negative integers")
            object.__setattr__(self, "usage", dict(self.usage))
        object.__setattr__(self, "error", _bounded(self.error, MAX_DIAGNOSTIC_CHARS, "error"))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        _keys(
            value,
            {
                "index",
                "mode",
                "started_at",
                "finished_at",
                "outcome",
                "model",
                "reasoning_effort",
                "endpoint_identity",
                "prompt_hash",
                "rubric_hash",
                "request_id",
                "usage",
                "error",
            },
        )
        return cls(**value)


@dataclass(frozen=True)
class JudgeInvocation:
    id: str
    kind: Literal["llm", "agent"]
    criterion_name: str
    status: Literal["running", "recovering", "completed", "failed"]
    attempts: tuple[JudgeAttempt, ...] = ()
    adapter: Literal["codex-cli", "claude-code"] | None = None
    adapter_version: str | None = None
    failure: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _name(self.id))
        object.__setattr__(self, "criterion_name", _name(self.criterion_name))
        if self.kind not in {"llm", "agent"}:
            raise ValueError("unknown Judge kind")
        if self.status not in {"running", "recovering", "completed", "failed"}:
            raise ValueError("unknown Judge status")
        object.__setattr__(self, "attempts", tuple(self.attempts))
        if self.kind == "llm" and (self.adapter is not None or self.adapter_version is not None):
            raise ValueError("LLM Judge invocations forbid Agent adapter fields")
        if self.kind == "agent" and self.adapter not in {"codex-cli", "claude-code"}:
            raise ValueError("Agent Judge invocations require a supported adapter")
        if self.status == "failed":
            if not self.failure or not self.attempts:
                raise ValueError("failed Judge invocations require attempts and failure")
        elif self.failure is not None:
            raise ValueError("only failed Judge invocations may carry failure")
        object.__setattr__(self, "failure", _bounded(self.failure, MAX_DIAGNOSTIC_CHARS, "failure"))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        _keys(
            value,
            {
                "id",
                "kind",
                "criterion_name",
                "adapter",
                "adapter_version",
                "status",
                "attempts",
                "failure",
            },
        )
        return cls(
            **{
                **value,
                "attempts": tuple(JudgeAttempt.from_dict(item) for item in value["attempts"]),
            }
        )


@dataclass(frozen=True)
class VerificationRecord:
    schema_version: Literal[1] = 1
    status: Literal["in_progress", "completed", "failed"] = "in_progress"
    criteria: tuple[CriterionResult, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)
    aggregates: tuple[AggregateResult, ...] = ()
    judge_invocations: tuple[JudgeInvocation, ...] = ()
    diagnostics: tuple[str, ...] = ()
    failure: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported verification record schema_version")
        if self.status not in {"in_progress", "completed", "failed"}:
            raise ValueError("unknown verification status")
        object.__setattr__(self, "criteria", tuple(self.criteria))
        object.__setattr__(self, "aggregates", tuple(self.aggregates))
        object.__setattr__(self, "judge_invocations", tuple(self.judge_invocations))
        object.__setattr__(
            self,
            "metrics",
            {_name(name): _finite(value) for name, value in self.metrics.items()},
        )
        object.__setattr__(
            self,
            "diagnostics",
            tuple(
                _bounded(value, MAX_DIAGNOSTIC_CHARS, "diagnostic") or ""
                for value in self.diagnostics
            ),
        )
        object.__setattr__(self, "failure", _bounded(self.failure, MAX_DIAGNOSTIC_CHARS, "failure"))
        criterion_names = [item.name for item in self.criteria]
        aggregate_names = [item.name for item in self.aggregates]
        invocation_ids = [item.id for item in self.judge_invocations]
        if len(criterion_names) != len(set(criterion_names)):
            raise ValueError("criterion names must be unique")
        if len(aggregate_names) != len(set(aggregate_names)):
            raise ValueError("aggregate names must be unique")
        rewards = set(criterion_names) | set(aggregate_names)
        if set(criterion_names) & set(aggregate_names) or rewards & set(self.metrics):
            raise ValueError("criterion, aggregate, and metric names may not collide")
        if len(invocation_ids) != len(set(invocation_ids)):
            raise ValueError("Judge invocation IDs must be unique")
        known = set(invocation_ids)
        if any(
            item.judge_invocation_id not in known
            for item in self.criteria
            if item.judge_invocation_id is not None
        ):
            raise ValueError("Judge criterion references an unknown invocation")
        if self.status == "completed" and not rewards:
            raise ValueError("completed verification requires at least one reward")
        if self.status == "failed":
            if not self.failure:
                raise ValueError("failed verification records require failure")
        elif self.failure is not None:
            raise ValueError("only failed verification records may carry failure")

    @property
    def rewards(self) -> dict[str, float]:
        return {
            **{item.name: item.score for item in self.criteria},
            **{item.name: item.score for item in self.aggregates},
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        _keys(
            value,
            {
                "schema_version",
                "status",
                "criteria",
                "metrics",
                "aggregates",
                "judge_invocations",
                "diagnostics",
                "failure",
            },
        )
        return cls(
            schema_version=value["schema_version"],
            status=value["status"],
            criteria=tuple(CriterionResult.from_dict(item) for item in value["criteria"]),
            metrics=value["metrics"],
            aggregates=tuple(AggregateResult.from_dict(item) for item in value["aggregates"]),
            judge_invocations=tuple(
                JudgeInvocation.from_dict(item) for item in value["judge_invocations"]
            ),
            diagnostics=tuple(value["diagnostics"]),
            failure=value["failure"],
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> Self:
        try:
            loaded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"malformed verification JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("verification record must be a JSON object")
        return cls.from_dict(loaded)


def _keys(value: dict[str, Any], expected: set[str]) -> None:
    if not isinstance(value, dict):
        raise ValueError("record values must be objects")
    missing = expected - set(value)
    extra = set(value) - expected
    if missing or extra:
        raise ValueError(f"record fields differ: missing={sorted(missing)}, extra={sorted(extra)}")
