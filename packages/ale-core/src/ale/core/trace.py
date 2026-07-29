"""Canonical transport and framework-execution trace contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ale.core.blob import InlineTextOrBlob

__all__ = [
    "CommandFinished",
    "CommandStarted",
    "DesktopAction",
    "ExecutionEvent",
    "ExecutionFailure",
    "ExecutionLog",
    "JsonlReadResult",
    "PartialOutputRecovered",
    "PhaseFinished",
    "PhaseStarted",
    "PolicyApplied",
    "TrajectoryLink",
    "TransportCall",
    "TransportEvent",
    "TransportReplay",
    "read_jsonl",
]

_STRICT = ConfigDict(frozen=True, extra="forbid")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DesktopAction(BaseModel):
    """Normalized desktop action used by policy and CUA harnesses."""

    model_config = _STRICT

    type: Literal[
        "screenshot",
        "click",
        "double_click",
        "right_click",
        "move",
        "drag",
        "scroll",
        "type",
        "key",
        "key_down",
        "key_up",
        "hold_key",
        "mouse_down",
        "mouse_up",
        "cursor_position",
        "wait",
    ]
    coordinate: tuple[float, float] | None = None
    to: tuple[float, float] | None = None
    text: str | None = None
    keys: tuple[str, ...] | None = None
    button: Literal["left", "right", "middle"] | None = None
    clicks: int | None = Field(default=None, ge=1, le=3)
    direction: Literal["up", "down", "left", "right"] | None = None
    amount: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)


class _Event(BaseModel):
    model_config = _STRICT

    schema_version: Literal[1] = 1
    kind: str
    seq: int = Field(default=0, ge=0)
    timestamp: str = Field(default_factory=_now)
    episode_id: str


class TransportCall(_Event):
    kind: Literal["call"] = "call"
    call_id: str
    model: str
    request_digest: str
    response_digest: str | None = None
    provider_response_id: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    stop_reason: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    disposition: Literal["forwarded", "refused", "failed"]
    refusal_limit: str | None = None
    upstream_status: int | None = None

    @model_validator(mode="after")
    def _disposition_fields(self) -> Self:
        if self.disposition == "refused" and not self.refusal_limit:
            raise ValueError("refused calls require refusal_limit")
        if self.disposition != "refused" and self.refusal_limit is not None:
            raise ValueError("refusal_limit is valid only for refused calls")
        return self


class TransportReplay(_Event):
    kind: Literal["replay"] = "replay"
    call_id: str
    request_digest: str
    reason: Literal["completed-cache", "inflight-coalesced"]
    charged: Literal[False] = False


class TrajectoryLink(_Event):
    kind: Literal["trajectory_link"] = "trajectory_link"
    call_id: str
    trajectory_id: str
    step_id: int = Field(ge=1)


TransportEvent = Annotated[
    TransportCall | TransportReplay | TrajectoryLink,
    Field(discriminator="kind"),
]


class _ExecutionEvent(_Event):
    phase: str | None = None
    component: str
    level: Literal["debug", "info", "warning", "error"] = "info"


class PhaseStarted(_ExecutionEvent):
    kind: Literal["phase_started"] = "phase_started"
    phase: str


class PhaseFinished(_ExecutionEvent):
    kind: Literal["phase_finished"] = "phase_finished"
    phase: str
    outcome: Literal["succeeded", "failed", "timed_out", "cancelled"]
    duration_ms: int = Field(ge=0)


class CommandStarted(_ExecutionEvent):
    kind: Literal["command_started"] = "command_started"
    execution_id: str
    actor: Literal["framework", "task"]
    argv: list[str]
    cwd: str | None = None


class CommandFinished(_ExecutionEvent):
    kind: Literal["command_finished"] = "command_finished"
    execution_id: str
    outcome: Literal["succeeded", "failed", "timed_out", "cancelled"]
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    stdout: InlineTextOrBlob
    stderr: InlineTextOrBlob
    truncated: bool = False


class PolicyApplied(_ExecutionEvent):
    kind: Literal["policy_applied"] = "policy_applied"
    policy: str
    requested_mode: str
    succeeded: bool


class ExecutionFailure(_ExecutionEvent):
    kind: Literal["failure"] = "failure"
    error_type: str
    message: str
    execution_id: str | None = None


class ExecutionLog(_ExecutionEvent):
    kind: Literal["log"] = "log"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class PartialOutputRecovered(_ExecutionEvent):
    kind: Literal["partial_output_recovered"] = "partial_output_recovered"
    execution_id: str
    stream: Literal["stdout", "stderr"]
    output: InlineTextOrBlob


ExecutionEvent = Annotated[
    PhaseStarted
    | PhaseFinished
    | CommandStarted
    | CommandFinished
    | PolicyApplied
    | ExecutionFailure
    | ExecutionLog
    | PartialOutputRecovered,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class JsonlReadResult:
    records: tuple[dict[str, Any], ...]
    torn_suffix: str | None = None


def read_jsonl(path: Path) -> JsonlReadResult:
    """Return every complete JSON-object line and expose the first torn suffix."""
    if not path.exists():
        return JsonlReadResult(())
    records: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        for raw in handle:
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                return JsonlReadResult(tuple(records), raw.decode("utf-8", errors="replace"))
            if not raw.endswith(b"\n"):
                return JsonlReadResult(tuple(records), decoded)
            try:
                value = json.loads(decoded)
            except json.JSONDecodeError:
                return JsonlReadResult(tuple(records), decoded.rstrip("\n"))
            if not isinstance(value, dict):
                return JsonlReadResult(tuple(records), decoded.rstrip("\n"))
            records.append(value)
    return JsonlReadResult(tuple(records))
