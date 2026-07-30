"""OpenAI Chat Completions dialect helpers."""

from __future__ import annotations

import json
from typing import Any

from ale.run.gateway.openai_responses import estimate_cost, pricing_for, refusal_body

__all__ = [
    "estimate_cost",
    "extract_usage",
    "pricing_for",
    "refusal_body",
    "usage_from_stream",
]


def extract_usage(payload: dict[str, Any]) -> tuple[int, int, str | None]:
    usage = payload.get("usage") or {}
    choices = payload.get("choices") or ()
    finish_reason = choices[0].get("finish_reason") if choices else None
    return (
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        finish_reason,
    )


def usage_from_stream(events: list[bytes]) -> tuple[int, int, str | None]:
    input_tokens = output_tokens = 0
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
        usage = event.get("usage") or {}
        input_tokens = max(input_tokens, int(usage.get("prompt_tokens") or 0))
        output_tokens = max(output_tokens, int(usage.get("completion_tokens") or 0))
        choices = event.get("choices") or ()
        if choices:
            finish_reason = choices[0].get("finish_reason") or finish_reason
    return input_tokens, output_tokens, finish_reason
