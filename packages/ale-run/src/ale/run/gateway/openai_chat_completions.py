"""OpenAI Chat Completions dialect helpers."""

from __future__ import annotations

import json
from typing import Any

from ale.run.gateway.openai_responses import refusal_body
from ale.run.gateway.session import CallUsage

__all__ = [
    "extract_usage",
    "refusal_body",
    "stream_completed",
    "usage_from_stream",
]

_USAGE_FIELDS = ("prompt_tokens", "cached_tokens", "completion_tokens")


def _fields(usage: object) -> dict[str, int]:
    values = usage if isinstance(usage, dict) else {}
    details = values.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": int(values.get("prompt_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or 0),
        "completion_tokens": int(values.get("completion_tokens") or 0),
    }


def _call_usage(fields: dict[str, int]) -> CallUsage:
    """Chat Completions ``prompt_tokens`` already include the cached ones."""
    return CallUsage(
        input_tokens=fields["prompt_tokens"],
        cache_read_tokens=fields["cached_tokens"],
        output_tokens=fields["completion_tokens"],
    )


def extract_usage(payload: dict[str, Any]) -> tuple[CallUsage, str | None]:
    choices = payload.get("choices") or ()
    finish_reason = choices[0].get("finish_reason") if choices else None
    return _call_usage(_fields(payload.get("usage"))), finish_reason


def stream_completed(events: list[bytes]) -> bool:
    """Whether a stream reached ``[DONE]`` without an in-band ``error`` object."""
    done = False
    for line in b"".join(events).splitlines():
        if not line.startswith(b"data:"):
            continue
        raw = line[5:].strip()
        if raw == b"[DONE]":
            done = True
            continue
        try:
            event = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("error"):
            return False
    return done


def usage_from_stream(events: list[bytes]) -> tuple[CallUsage, str | None]:
    observed = dict.fromkeys(_USAGE_FIELDS, 0)
    finish_reason: str | None = None
    for line in b"".join(events).splitlines():
        if not line.startswith(b"data:"):
            continue
        raw = line[5:].strip()
        if raw == b"[DONE]":
            continue
        try:
            event = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            continue
        for name, value in _fields(event.get("usage")).items():
            observed[name] = max(observed[name], value)
        choices = event.get("choices") or ()
        if choices:
            finish_reason = choices[0].get("finish_reason") or finish_reason
    return _call_usage(observed), finish_reason
