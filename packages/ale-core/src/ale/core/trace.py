"""Episode traces.

Two layers, because there are exactly two places worth trusting as a source of truth:

* **transport** — every model call, recorded by the gateway. Nothing else may write it,
  which is what makes "did this agent call the model?" answerable rather than inferred.
* **semantic** — what happened in the episode: the instruction, commands, observations,
  actions, verification. Uniform across harness families, so a CLI agent and a GUI
  agent produce comparable trajectories.

Large payloads (screenshots, command output) are written as files and referenced by
relative path. Inlining them as base64 is how traces become unreadable and unbounded.

A ``training`` slot is reserved for token-level data; it stays absent until a trainer
integration actually exists.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ActionRecord",
    "AgentOutputRecord",
    "DesktopAction",
    "ExecRecord",
    "InstructionRecord",
    "NoteRecord",
    "ObservationRecord",
    "PhaseSpan",
    "SemanticRecord",
    "TimingRecord",
    "TraceWriter",
    "TransportRecord",
    "VerifierRecord",
    "read_records",
]

_RECORD = ConfigDict(frozen=True, extra="forbid")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TransportRecord(BaseModel):
    """One model call, as observed by the gateway.

    A retry served from the gateway's replay cache produces exactly one record: usage
    is never double counted, and the message history never forks.
    """

    model_config = _RECORD

    seq: int = Field(ge=0)
    ts: str = Field(default_factory=_now)
    episode_id: str
    model: str
    request_digest: str
    response_digest: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    stop_reason: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    refused: bool = False
    """True when the gateway declined the call because a limit was reached."""

    refusal_limit: str | None = None

    upstream_status: int | None = Field(
        default=None,
        description="Provider HTTP status when the call did not succeed. A failed call "
        "is still a call: without it a run that burned an hour on 529s looks idle.",
    )


class DesktopAction(BaseModel):
    """A normalised desktop action.

    Coordinates live in a [0, 1000] space so a trajectory stays meaningful across
    resolutions; the provider maps them to pixels at dispatch time.
    """

    model_config = _RECORD

    type: Literal[
        "click", "double_click", "right_click", "move", "drag", "scroll", "type", "key", "wait"
    ]
    coordinate: tuple[int, int] | None = None
    to: tuple[int, int] | None = Field(default=None, description="Drag destination")
    text: str | None = None
    keys: tuple[str, ...] | None = None
    direction: Literal["up", "down", "left", "right"] | None = None
    amount: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)


class _Base(BaseModel):
    model_config = _RECORD

    seq: int = Field(ge=0)
    ts: str = Field(default_factory=_now)


class InstructionRecord(_Base):
    kind: Literal["instruction"] = "instruction"
    text_digest: str
    chars: int = Field(ge=0)


class AgentOutputRecord(_Base):
    kind: Literal["agent_output"] = "agent_output"
    text_digest: str
    text_ref: str | None = None


class ExecRecord(_Base):
    kind: Literal["exec"] = "exec"
    argv_digest: str
    exit_code: int | None = None
    duration_ms: int = Field(default=0, ge=0)
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    truncated: bool = False


class ObservationRecord(_Base):
    kind: Literal["observation"] = "observation"
    step: int = Field(ge=0)
    screenshot_ref: str | None = None
    text: str | None = None


class ActionRecord(_Base):
    kind: Literal["action"] = "action"
    step: int = Field(ge=0)
    action: DesktopAction
    accepted: bool = True
    rejection: str | None = Field(
        default=None, description="Why an action was not dispatched, if it was not"
    )


class VerifierRecord(_Base):
    kind: Literal["verifier"] = "verifier"
    entry: str
    exit_code: int | None = None
    rewards: dict[str, float] | None = None
    stdout_ref: str | None = None


class PhaseSpan(BaseModel):
    """How long one phase took."""

    model_config = _RECORD

    name: str
    duration_ms: int = Field(default=0, ge=0)


class TimingRecord(_Base):
    """Where an episode's wall clock went.

    A single duration answers almost nothing: an episode that took twenty minutes is a
    slow model, a slow sandbox or a slow framework, and those have different fixes. The
    split is computed from evidence already recorded — model time from the gateway's
    calls, sandbox time from executed commands — so it cannot drift from what happened.

    ``model_ms + sandbox_ms + framework_ms == total_ms`` by construction: the framework
    share is the remainder, which is the honest way to report time nobody accounted for.
    """

    kind: Literal["timing"] = "timing"
    total_ms: int = Field(ge=0)
    model_ms: int = Field(default=0, ge=0)
    sandbox_ms: int = Field(default=0, ge=0)
    framework_ms: int = Field(default=0, ge=0)
    phases: tuple[PhaseSpan, ...] = ()


class NoteRecord(_Base):
    kind: Literal["note"] = "note"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


SemanticRecord = Annotated[
    InstructionRecord
    | AgentOutputRecord
    | ExecRecord
    | ObservationRecord
    | ActionRecord
    | VerifierRecord
    | TimingRecord
    | NoteRecord,
    Field(discriminator="kind"),
]


class TraceLayer(StrEnum):
    TRANSPORT = "transport"
    SEMANTIC = "semantic"


class TraceWriter:
    """Append-only JSON Lines writer for one episode's traces.

    Sequence numbers are assigned here so callers cannot produce gaps or duplicates,
    and each line is flushed as it is written: a killed process loses the current call,
    not the trajectory.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._counters: dict[str, int] = {layer: 0 for layer in TraceLayer}

    def path(self, layer: TraceLayer) -> Path:
        return self.directory / f"trace.{layer}.jsonl"

    def next_seq(self, layer: TraceLayer) -> int:
        seq = self._counters[layer]
        self._counters[layer] = seq + 1
        return seq

    def _append(self, layer: TraceLayer, payload: dict[str, Any]) -> None:
        with self.path(layer).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()

    def write_transport(self, record: TransportRecord) -> None:
        self._append(TraceLayer.TRANSPORT, record.model_dump(mode="json"))

    def write_semantic(self, record: SemanticRecord) -> None:
        self._append(TraceLayer.SEMANTIC, record.model_dump(mode="json"))  # type: ignore[union-attr]

    def blob_path(self, name: str) -> Path:
        """Path for an externalised payload, referenced from records by relative path."""
        blobs = self.directory / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        return blobs / name

    def relative(self, path: Path) -> str:
        return str(path.relative_to(self.directory))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the records in a trace file, skipping a torn final line.

    A partially written last line means the process died mid-write; the rest of the
    trace is still evidence.
    """
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                return
