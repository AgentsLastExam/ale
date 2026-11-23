"""Framework-owned Judge observations, independent of task scoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Self

HASH_PREFIX = "sha256:"
MAX_DIAGNOSTIC_CHARS = 2_000


def _name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("names must be non-empty strings")
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


def _timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("timestamps must be strings")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamps must be ISO 8601 date-times") from exc
    return value


@dataclass(frozen=True)
class EvidenceReference:
    kind: Literal[
        "file",
        "reference",
        "task_instruction",
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
    schema_hash: str
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
        object.__setattr__(self, "schema_hash", _hash(self.schema_hash))
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
                "schema_hash",
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
    name: str
    status: Literal["running", "recovering", "completed", "failed"]
    attempts: tuple[JudgeAttempt, ...] = ()
    adapter: Literal["codex-cli", "claude-code"] | None = None
    adapter_version: str | None = None
    failure: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _name(self.id))
        object.__setattr__(self, "name", _name(self.name))
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
                "name",
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


def _keys(value: dict[str, Any], expected: set[str]) -> None:
    if not isinstance(value, dict):
        raise ValueError("record values must be objects")
    missing = expected - set(value)
    extra = set(value) - expected
    if missing or extra:
        raise ValueError(f"record fields differ: missing={sorted(missing)}, extra={sorted(extra)}")
