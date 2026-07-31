"""Direct synchronous LLM Judge adapters."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from ._io import JudgeError, digest, endpoint_identity, sanitize
from ._records import (
    EvidenceReference,
    JudgeAttempt,
    JudgeInvocation,
    ScoredChoice,
)

ATTEMPTS = 4
TIMEOUT_SECONDS = 180
_EFFORTS = {"low", "medium", "high"}


def resolve_protocol(config: Mapping[str, str]) -> str:
    host = (urlsplit(config["base_url"]).hostname or "").lower()
    model = config["model"].lower()
    if "anthropic" in host or model.startswith("claude"):
        if "openai" in host or model.startswith(("gpt-", "o1", "o3", "o4")):
            raise ValueError("cannot safely infer the LLM Judge protocol")
        return "anthropic"
    if "openai" in host or model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai-responses"
    raise ValueError("cannot safely infer the LLM Judge protocol from base_url and model")


def run(
    *,
    invocation_id: str,
    name: str,
    prompt: str,
    rubric: Mapping[str, ScoredChoice],
    evidence: Sequence[tuple[str, EvidenceReference]],
    reference: str | None,
    trajectory: str | None,
    config: Mapping[str, str],
) -> tuple[str, str, JudgeInvocation]:
    del reference, trajectory
    protocol = resolve_protocol(config)
    effort = config["reasoning_effort"]
    if effort not in _EFFORTS:
        raise ValueError(f"reasoning_effort {effort!r} is unsupported")
    rendered = _prompt(name, prompt, rubric, evidence)
    prompt_hash = digest(rendered)
    rubric_hash = digest(_rubric_json(rubric))
    endpoint = endpoint_identity(config["base_url"])
    secret = os.environ.get(config["api_key_env"], "")
    if not secret:
        invocation = _failed_preflight(
            invocation_id,
            name,
            config,
            endpoint,
            prompt_hash,
            rubric_hash,
            f"{config['api_key_env']} is not set for the LLM Judge",
        )
        raise JudgeError(invocation.failure or "LLM Judge configuration failed", invocation)

    attempts: list[JudgeAttempt] = []
    last_error = ""
    repair = False
    for index in range(1, ATTEMPTS + 1):
        attempt_mode = "initial" if index == 1 else "schema_repair" if repair else "retry"
        request_prompt = (
            _repair_prompt(rendered, rubric, last_error) if index > 1 and repair else rendered
        )
        started = _now()
        request_id = None
        usage = None
        try:
            payload = _request(protocol, config, request_prompt, secret)
            request_id = _request_id(payload, secret)
            usage = _usage(payload)
            text = _response_text(protocol, payload)
            choice, reasoning = _verdict(text, rubric)
        except Exception as exc:
            finished = _now()
            last_error = sanitize(exc, (secret,))
            outcome, invalid_verdict = _outcome(exc)
            repair = invalid_verdict or attempt_mode == "schema_repair"
            attempts.append(
                JudgeAttempt(
                    index=index,
                    mode=attempt_mode,
                    started_at=started,
                    finished_at=finished,
                    outcome=outcome,
                    model=config["model"],
                    reasoning_effort=effort,
                    endpoint_identity=endpoint,
                    prompt_hash=prompt_hash,
                    rubric_hash=rubric_hash,
                    request_id=request_id,
                    usage=usage,
                    error=last_error,
                )
            )
            if index < ATTEMPTS:
                time.sleep(0.25 * index)
            continue

        attempts.append(
            JudgeAttempt(
                index=index,
                mode=attempt_mode,
                started_at=started,
                finished_at=_now(),
                outcome="completed",
                model=config["model"],
                reasoning_effort=effort,
                endpoint_identity=endpoint,
                prompt_hash=prompt_hash,
                rubric_hash=rubric_hash,
                request_id=request_id,
                usage=usage,
            )
        )
        return (
            choice,
            sanitize(reasoning, (secret,)),
            JudgeInvocation(
                id=invocation_id,
                kind="llm",
                criterion_name=name,
                status="completed",
                attempts=tuple(attempts),
            ),
        )

    failure = f"LLM Judge produced no valid verdict after {ATTEMPTS} attempts: {last_error}"
    invocation = JudgeInvocation(
        id=invocation_id,
        kind="llm",
        criterion_name=name,
        status="failed",
        attempts=tuple(attempts),
        failure=failure,
    )
    raise JudgeError(failure, invocation)


def _request(
    protocol: str,
    config: Mapping[str, str],
    prompt: str,
    secret: str,
) -> dict[str, Any]:
    if protocol == "anthropic":
        payload = {
            "model": config["model"],
            "max_tokens": 4096,
            "output_config": {"effort": config["reasoning_effort"]},
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": secret,
            "anthropic-version": "2023-06-01",
        }
    else:
        payload = {
            "model": config["model"],
            "input": prompt,
            "reasoning": {"effort": config["reasoning_effort"]},
            "max_output_tokens": 4096,
        }
        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        }
    request = urllib.request.Request(
        _endpoint(config["base_url"], protocol),
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            loaded = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"provider returned HTTP {exc.code}: {body[:500]}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("provider response must be a JSON object")
    return loaded


def _endpoint(base_url: str, protocol: str) -> str:
    base = base_url.rstrip("/")
    suffix = "/v1/messages" if protocol == "anthropic" else "/v1/responses"
    if base.endswith(suffix) or base.endswith(suffix.removeprefix("/v1")):
        return base
    return base + (suffix.removeprefix("/v1") if base.endswith("/v1") else suffix)


def _prompt(
    name: str,
    prompt: str,
    rubric: Mapping[str, ScoredChoice],
    evidence: Sequence[tuple[str, EvidenceReference]],
) -> str:
    choices = "|".join(rubric)
    sections = [
        "Return JSON only. Do not include Markdown or additional keys.",
        f'Exact response: {{"choice":"{choices}","reasoning":"..."}}',
        f"Criterion: {name}",
        f"Task: {prompt}",
        f"Rubric: {_rubric_json(rubric)}",
    ]
    if evidence:
        sections.append(
            "Evidence:\n"
            + "\n\n".join(
                f"{reference.kind.upper()} {reference.location or ''} ({reference.sha256})\n{text}"
                for text, reference in evidence
            )
        )
    return "\n\n".join(sections)


def _repair_prompt(
    original: str,
    rubric: Mapping[str, ScoredChoice],
    error: str,
) -> str:
    choices = "|".join(rubric)
    return (
        f"{original}\n\nYour previous verdict was rejected:\n{error}\n\n"
        f'Submit exactly:\n{{"choice":"{choices}","reasoning":"..."}}\n'
        "Do not include Markdown or additional keys."
    )


def _rubric_json(rubric: Mapping[str, ScoredChoice]) -> str:
    return json.dumps(
        {
            label: {"score": choice.score, "description": choice.description}
            for label, choice in rubric.items()
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _response_text(protocol: str, payload: Mapping[str, Any]) -> str:
    if protocol == "anthropic":
        if payload.get("stop_reason") == "refusal":
            raise PermissionError("provider refused the Judge request")
        text = "".join(
            item.get("text", "")
            for item in payload.get("content", ())
            if isinstance(item, dict) and item.get("type") == "text"
        )
    else:
        if payload.get("status") == "refused":
            raise PermissionError("provider refused the Judge request")
        text = payload.get("output_text", "")
        if not text:
            text = "".join(
                part.get("text", "")
                for item in payload.get("output", ())
                if isinstance(item, dict)
                for part in item.get("content", ())
                if isinstance(part, dict) and part.get("type") == "output_text"
            )
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Judge response contains no verdict text")
    return text


def _verdict(text: str, rubric: Mapping[str, ScoredChoice]) -> tuple[str, str]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"verdict is not valid JSON: {exc.msg}") from exc
    if not isinstance(loaded, dict) or set(loaded) != {"choice", "reasoning"}:
        raise ValueError("verdict must contain exactly choice and reasoning")
    choice = loaded["choice"]
    reasoning = loaded["reasoning"]
    if choice not in rubric:
        raise ValueError(f"unknown choice {choice!r}; expected one of {list(rubric)}")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("verdict reasoning must be a non-empty string")
    return choice, reasoning


def _usage(payload: Mapping[str, Any]) -> dict[str, int] | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    normalized = {
        str(key): int(value)
        for key, value in usage.items()
        if isinstance(value, int) and value >= 0
    }
    return normalized or None


def _request_id(payload: Mapping[str, Any], secret: str) -> str | None:
    value = payload.get("id")
    return sanitize(value, (secret,)) if value else None


def _outcome(error: Exception) -> tuple[str, bool]:
    if isinstance(error, PermissionError):
        return "refused", False
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timed_out", False
    if isinstance(error, (ValueError, json.JSONDecodeError)):
        return "invalid", True
    return "failed", False


def _failed_preflight(
    invocation_id: str,
    name: str,
    config: Mapping[str, str],
    endpoint: str,
    prompt_hash: str,
    rubric_hash: str,
    failure: str,
) -> JudgeInvocation:
    now = _now()
    return JudgeInvocation(
        id=invocation_id,
        kind="llm",
        criterion_name=name,
        status="failed",
        attempts=(
            JudgeAttempt(
                index=1,
                mode="initial",
                started_at=now,
                finished_at=now,
                outcome="failed",
                model=config["model"],
                reasoning_effort=config["reasoning_effort"],
                endpoint_identity=endpoint,
                prompt_hash=prompt_hash,
                rubric_hash=rubric_hash,
                error=failure,
            ),
        ),
        failure=failure,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
