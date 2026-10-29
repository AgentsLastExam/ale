"""Plain task results and optional Judge tools."""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from . import _agents, _llm, checks
from ._io import (
    MAX_TOTAL_EVIDENCE_BYTES,
    ConfigurationError,
    EvidenceError,
    JudgeError,
    VerificationError,
    atomic_json,
    configured_secrets,
    endpoint_identity,
    evidence_text,
    inline_evidence,
    load_config,
    media_type,
    read_file_evidence,
    read_text_evidence,
    redact,
)
from ._journal import journal
from ._json import json_object, schema_validator
from ._records import EvidenceReference, JudgeAttempt, JudgeInvocation

__version__ = "0.1.1"
__all__ = [
    "ConfigurationError",
    "EvidenceError",
    "EvidenceReference",
    "JudgeAttempt",
    "JudgeError",
    "JudgeInvocation",
    "VerificationError",
    "checks",
    "endpoint_identity",
    "judge",
    "judge_route",
    "probe_judge",
    "read_result",
    "write",
]

#: Where a verifier leaves its result: Harbor's verifier directory.
_VERIFIER_DIR = Path("C:/logs/verifier" if os.name == "nt" else "/logs/verifier")


def judge_route(kind: str, config: Mapping[str, str]) -> tuple[str, str]:
    """The protocol a configured Judge speaks and the exact URL its requests go to.

    The engine forwards Judge traffic through its Gateway, so it needs the same answer the
    Judge itself would reach: an LLM Judge's protocol from its endpoint and model, an Agent
    Judge's from its CLI (Codex speaks Responses, Claude Code speaks Messages).
    """
    if kind == "llm":
        try:
            protocol = _llm.resolve_protocol(config)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
    elif kind == "agent":
        protocol = "openai-responses" if config["adapter"] == "codex-cli" else "anthropic"
    else:
        raise ValueError("Judge kind must be 'llm' or 'agent'")
    return protocol, _llm._endpoint(config["base_url"], protocol)


def probe_judge(kind: str, config: Mapping[str, str]) -> None:
    """Prove one configured Judge can be reached before any agent runs.

    ``config`` is that kind's section of the verification config. An LLM Judge is probed
    with its own protocol and effort; an Agent Judge with the protocol its CLI speaks to
    the same endpoint, key and model. Raises ConfigurationError when the Judge cannot
    answer.
    """
    if kind == "llm":
        if config.get("reasoning_effort") not in _llm._EFFORTS:
            raise ConfigurationError(
                f"LLM Judge reasoning_effort {config.get('reasoning_effort')!r} is unsupported"
            )
        try:
            protocol = _llm.resolve_protocol(config)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        _llm.probe(config, protocol)
    elif kind == "agent":
        protocol = "openai-responses" if config["adapter"] == "codex-cli" else "anthropic"
        _llm.probe(
            {key: value for key, value in config.items() if key != "reasoning_effort"},
            protocol,
        )
    else:
        raise ValueError("Judge kind must be 'llm' or 'agent'")


def read_result(reward: str | bytes, verification: str | bytes | None = None) -> dict[str, Any]:
    """Check what a verifier wrote and return the complete result, ``reward`` rounded.

    ``reward`` is the content of ``reward.json``: a flat object of numbers whose ``reward``
    is from 0 through 1. ``verification`` is the content of ``verification.json`` when one
    was written: any object whose ``reward`` equals ``reward.json``'s. Only ``reward`` is
    rounded, to two decimal places, half up.
    """
    rewards = json_object(reward)
    for name, value in rewards.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"reward.json values must be numbers: {name}")
    if "reward" not in rewards:
        raise ValueError("reward.json must contain reward")
    rounded = _rounded(rewards["reward"])
    result = rewards if verification is None else json_object(verification)
    claimed = result.get("reward")
    if isinstance(claimed, bool) or claimed != rewards["reward"]:
        raise ValueError("verification.json reward must equal reward.json reward")
    return {**result, "reward": rounded}


def write(result: dict[str, Any]) -> None:
    """Atomically save a result. The engine accepts it only after normal exit.

    ``verification.json`` receives the complete result and ``reward.json`` its ``reward``,
    both with ``reward`` rounded, in the verifier directory Harbor also uses.
    """
    # Round-tripping also rejects non-JSON Python objects and non-finite values.
    _json_types(result)
    if not isinstance(result, dict) or "reward" not in result:
        raise ValueError("a result must contain reward")
    complete = json.dumps(result, ensure_ascii=False, allow_nan=False)
    normalized = read_result(json.dumps({"reward": result["reward"]}), complete)
    atomic_json(_VERIFIER_DIR / "verification.json", normalized)
    atomic_json(_VERIFIER_DIR / "reward.json", {"reward": normalized["reward"]})


def _rounded(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("reward must be a number")
    if not 0 <= value <= 1 or not math.isfinite(value):
        raise ValueError("reward must be finite and between 0 and 1")
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _json_types(value: Any) -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for item in value.values():
            _json_types(item)
    elif isinstance(value, list):
        for item in value:
            _json_types(item)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("result must contain only JSON values")


def judge(
    kind: str,
    name: str = "judge",
    *,
    prompt: str,
    output_schema: dict[str, Any] | None = None,
    files: Sequence[str] = (),
    reference: str | None = None,
    trajectory: bool = False,
    mcp_servers: Sequence[Mapping[str, object]] = (),
) -> dict[str, Any]:
    """Return model data; all interpretation and scoring belong to the caller."""
    if kind not in {"llm", "agent"}:
        raise ValueError("Judge kind must be 'llm' or 'agent'")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Judge prompt must be non-empty")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Judge name must be non-empty")
    schema_validator(output_schema)  # Reject invalid schemas before a paid call.
    invocation_id = "judge-" + uuid.uuid4().hex
    secrets: tuple[str, ...] = ()
    try:
        configs = load_config()
        secrets = configured_secrets(configs)
        journal(
            kind,
            {
                "event": "started",
                "id": invocation_id,
                "kind": kind,
                "name": name,
                "prompt": prompt,
                "output_schema": output_schema,
            },
            secrets,
        )
        config = configs.get(kind)
        if config is None:
            raise ConfigurationError(f"verification {kind} configuration is missing")
        evidence = []
        for path in files:
            raw, item = read_file_evidence(path)
            content = evidence_text(raw, item) if kind == "llm" and media_type(raw) is None else raw
            evidence.append((content, item))
        if reference is not None:
            evidence.append(inline_evidence(reference, kind="reference"))
        if trajectory:
            path = os.environ.get("ALE_TRAJECTORY_PATH")
            if not path:
                raise EvidenceError("no solver trajectory was recorded")
            evidence.append(read_text_evidence(path, kind="solver_trajectory"))
        if sum(item.size_bytes for _, item in evidence) > MAX_TOTAL_EVIDENCE_BYTES:
            raise EvidenceError("Judge evidence exceeds the total byte limit")
        journal(
            kind,
            {
                "event": "evidence",
                "id": invocation_id,
                "files": [asdict(item) for _, item in evidence],
            },
            secrets,
        )
        arguments = dict(
            invocation_id=invocation_id,
            name=name,
            prompt=prompt,
            output_schema=output_schema,
            evidence=evidence,
            config=config,
        )
        if kind == "agent":
            arguments["mcp_servers"] = mcp_servers
        response, invocation = (_llm.run if kind == "llm" else _agents.run)(**arguments)
        journal(
            kind,
            {
                "event": "completed",
                "id": invocation_id,
                "invocation": asdict(invocation),
                "response": response,
            },
            secrets,
        )
        return response
    except Exception as exc:
        invocation = getattr(exc, "invocation", None)
        journal(
            kind,
            {
                "event": "failed",
                "id": invocation_id,
                "error": redact(exc, secrets),
                "invocation": asdict(invocation)
                if isinstance(invocation, JudgeInvocation)
                else None,
            },
            secrets,
        )
        raise
