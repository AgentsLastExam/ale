"""Stateful sandbox-local verification for ALE tasks."""

from __future__ import annotations

import math
import os
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from . import _agents, _llm
from ._io import (
    ConfigurationError,
    EvidenceError,
    JudgeError,
    VerificationError,
    atomic_json,
    inline_evidence,
    load_config,
    read_text_evidence,
    sanitize,
)
from ._records import (
    AggregateResult,
    CheckResult,
    CriterionResult,
    EvidenceReference,
    JudgeAttempt,
    JudgeInvocation,
    ScoredChoice,
    VerificationRecord,
)

__version__ = "0.1.0"

__all__ = [
    "AggregateResult",
    "CheckResult",
    "ConfigurationError",
    "CriterionResult",
    "EvidenceError",
    "EvidenceReference",
    "JudgeAttempt",
    "JudgeError",
    "JudgeInvocation",
    "ScoredChoice",
    "Verification",
    "VerificationError",
    "VerificationRecord",
    "checks",
]


class Verification:
    """One ordered scoring program and one atomically maintained record."""

    def __init__(self) -> None:
        self._criteria: OrderedDict[str, CriterionResult] = OrderedDict()
        self._metrics: OrderedDict[str, float] = OrderedDict()
        self._aggregates: OrderedDict[str, AggregateResult] = OrderedDict()
        self._invocations: list[JudgeInvocation] = []
        self._diagnostics: list[str] = []
        self._status = "in_progress"
        self._failure: str | None = None
        self._agent_judge_started = False
        self._record_path = os.environ.get("ALE_VERIFICATION_PATH", "")
        self._verdict_path = os.environ.get("ALE_VERDICT_PATH", "")

    def check(self, name: str, result: CheckResult, *, weight: float = 1.0) -> float:
        self._assert_mutable()
        self._new_name(name)
        if not isinstance(result, CheckResult):
            raise TypeError("result must be a CheckResult")
        criterion = CriterionResult(
            name=name,
            source="check",
            score=result.score,
            weight=weight,
            reasoning=result.diagnostic or None,
            raw_value=result.raw_value,
            evidence=result.evidence,
        )
        self._criteria[name] = criterion
        self._persist()
        return criterion.score

    def metric(self, name: str, value: float) -> float:
        self._assert_mutable()
        _name(name)
        if name in self._criteria or name in self._aggregates:
            raise ValueError(f"metric name collides with a reward: {name}")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("metric values must be finite")
        self._metrics[name] = number
        self._persist()
        return number

    def judge(
        self,
        kind: str,
        name: str,
        *,
        prompt: str,
        rubric: Mapping[str, Mapping[str, object] | ScoredChoice],
        files: Sequence[str] = (),
        reference: str | None = None,
        trajectory: bool = False,
        weight: float = 1.0,
    ) -> float:
        self._assert_mutable()
        self._new_name(name)
        if kind not in {"llm", "agent"}:
            raise ValueError("Judge kind must be 'llm' or 'agent'")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Judge prompt must be non-empty")
        choices = _rubric(rubric)
        weight = _weight(weight)
        if kind == "agent":
            if self._agent_judge_started:
                raise RuntimeError("only one Agent Judge invocation is allowed")
            self._agent_judge_started = True

        evidence_text: list[tuple[str, EvidenceReference]] = []
        for path in files:
            evidence_text.append(read_text_evidence(path))
        reference_text = None
        if reference is not None:
            reference_text = inline_evidence(reference, kind="reference")
            evidence_text.append(reference_text)
        trajectory_text = None
        if trajectory:
            path = os.environ.get("ALE_TRAJECTORY_PATH")
            if not path:
                return self._fail("ALE_TRAJECTORY_PATH is not configured")
            trajectory_text = read_text_evidence(path, kind="solver_trajectory")
            evidence_text.append(trajectory_text)

        invocation_id = "judge-" + uuid.uuid4().hex
        try:
            config = load_config().get(kind)
            if config is None:
                raise ConfigurationError(f"verification {kind} configuration is missing")
            runner = _llm.run if kind == "llm" else _agents.run
            choice, reasoning, invocation = runner(
                invocation_id=invocation_id,
                name=name,
                prompt=prompt,
                rubric=choices,
                evidence=tuple(evidence_text),
                reference=reference_text[0] if reference_text else None,
                trajectory=trajectory_text[0] if trajectory_text else None,
                config=config,
            )
            if not isinstance(invocation, JudgeInvocation):
                raise RuntimeError("Judge returned a malformed invocation")
            selected = choices.get(choice)
            if selected is None:
                raise RuntimeError(f"Judge returned unknown choice {choice!r}")
            if not isinstance(reasoning, str) or not reasoning.strip():
                raise RuntimeError("Judge returned empty reasoning")
        except Exception as exc:
            invocation = getattr(exc, "invocation", None)
            if (
                invocation is None
                and len(exc.args) > 1
                and isinstance(exc.args[1], JudgeInvocation)
            ):
                invocation = exc.args[1]
            if isinstance(invocation, JudgeInvocation):
                self._invocations.append(invocation)
            return self._fail(f"judge infrastructure: {sanitize(exc)}")

        self._invocations.append(invocation)
        criterion = CriterionResult(
            name=name,
            source="llm_judge" if kind == "llm" else "agent_judge",
            score=selected.score,
            weight=weight,
            reasoning=reasoning,
            raw_value=choice,
            evidence=tuple(reference for _, reference in evidence_text),
            judge_invocation_id=invocation.id,
        )
        self._criteria[name] = criterion
        self._persist()
        return criterion.score

    def aggregate(
        self,
        name: str,
        *,
        inputs: Sequence[str] | None = None,
        weights: Mapping[str, float] | None = None,
    ) -> float:
        self._assert_terminal_mutation()
        self._new_name(name)
        selected = list(self._criteria) if inputs is None else list(inputs)
        if not selected:
            raise ValueError("aggregate requires at least one input")
        if len(selected) != len(set(selected)):
            raise ValueError("aggregate inputs must be unique")
        values: OrderedDict[str, float] = OrderedDict()
        stored_weights: OrderedDict[str, float] = OrderedDict()
        for input_name in selected:
            if input_name in self._criteria:
                item = self._criteria[input_name]
                values[input_name] = item.score
                stored_weights[input_name] = item.weight
            elif inputs is not None and input_name in self._aggregates:
                values[input_name] = self._aggregates[input_name].score
                stored_weights[input_name] = 1.0
            else:
                raise ValueError(f"unknown aggregate input: {input_name}")
        if weights is not None:
            if set(weights) != set(values):
                raise ValueError("aggregate weight keys must match inputs exactly")
            stored_weights = OrderedDict(
                (input_name, _weight(weights[input_name])) for input_name in values
            )
        total = sum(stored_weights.values())
        score = sum(values[key] * stored_weights[key] for key in values) / total
        aggregate = AggregateResult(
            name=name,
            method="weighted_mean" if weights is not None else "mean",
            inputs=dict(values),
            weights=dict(stored_weights),
            score=score,
        )
        self._aggregates[name] = aggregate
        self._persist()
        return aggregate.score

    def write(self) -> None:
        self._assert_terminal_mutation()
        if not self._criteria and not self._aggregates:
            raise ValueError("verification must contain at least one reward")
        self._status = "completed"
        record = self._persist()
        if not self._verdict_path:
            raise RuntimeError("ALE_VERDICT_PATH is not configured")
        atomic_json(
            self._verdict_path,
            {"rewards": record.rewards, "metrics": dict(record.metrics)},
        )

    def _new_name(self, name: str) -> None:
        _name(name)
        if name in self._criteria or name in self._aggregates or name in self._metrics:
            raise ValueError(f"duplicate verification name: {name}")

    def _assert_mutable(self) -> None:
        self._assert_terminal_mutation()
        if self._agent_judge_started:
            raise RuntimeError("check and Judge calls are forbidden after an Agent Judge")

    def _assert_terminal_mutation(self) -> None:
        if self._status != "in_progress":
            raise RuntimeError("verification is already terminal")

    def _fail(self, message: str) -> Any:
        self._status = "failed"
        self._failure = message
        self._persist()
        raise VerificationError(message)

    def _record(self) -> VerificationRecord:
        return VerificationRecord(
            status=self._status,  # type: ignore[arg-type]
            criteria=tuple(self._criteria.values()),
            metrics=dict(self._metrics),
            aggregates=tuple(self._aggregates.values()),
            judge_invocations=tuple(self._invocations),
            diagnostics=tuple(self._diagnostics),
            failure=self._failure,
        )

    def _persist(self) -> VerificationRecord:
        if not self._record_path:
            raise RuntimeError("ALE_VERIFICATION_PATH is not configured")
        record = self._record()
        atomic_json(self._record_path, record.to_dict())
        return record


def _name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("verification names must be non-empty strings")
    return value


def _weight(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("weights must be finite and positive")
    return value


def _rubric(
    rubric: Mapping[str, Mapping[str, object] | ScoredChoice],
) -> OrderedDict[str, ScoredChoice]:
    if not isinstance(rubric, Mapping) or len(rubric) < 2:
        raise ValueError("Judge rubric requires at least two choices")
    normalized: OrderedDict[str, ScoredChoice] = OrderedDict()
    for label, authored in rubric.items():
        _name(label)
        if label in normalized:
            raise ValueError(f"duplicate rubric choice: {label}")
        if isinstance(authored, ScoredChoice):
            normalized[label] = authored
        elif isinstance(authored, Mapping):
            if set(authored) != {"score", "description"}:
                raise ValueError("rubric choices require only score and description")
            normalized[label] = ScoredChoice(
                score=authored["score"],  # type: ignore[arg-type]
                description=authored["description"],  # type: ignore[arg-type]
            )
        else:
            raise ValueError("rubric choices must be mappings")
    return normalized


from . import checks  # noqa: E402
