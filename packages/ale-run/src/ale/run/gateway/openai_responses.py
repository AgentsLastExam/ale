"""OpenAI Responses dialect helpers."""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "estimate_cost",
    "extract_usage",
    "pricing_for",
    "refusal_body",
    "usage_from_stream",
]

USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "grok-4.5": (2.0, 6.0),
}


def pricing_for(model: str) -> tuple[float, float] | None:
    lowered = model.lower()
    return next((rates for name, rates in USD_PER_MTOK.items() if name in lowered), None)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = pricing_for(model)
    if rates is None:
        return 0.0
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


def extract_usage(payload: dict[str, Any]) -> tuple[int, int, str | None]:
    usage = payload.get("usage") or {}
    incomplete = payload.get("incomplete_details") or {}
    return (
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        incomplete.get("reason") or payload.get("status"),
    )


def usage_from_stream(events: list[bytes]) -> tuple[int, int, str | None]:
    for line in reversed(b"".join(events).splitlines()):
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:].strip() or b"{}")
        except json.JSONDecodeError:
            continue
        if event.get("type") not in {
            "response.completed",
            "response.failed",
            "response.incomplete",
        }:
            continue
        return extract_usage(event.get("response") or {})
    return 0, 0, None


def refusal_body(
    limit: str,
    value: float,
    observed_value: float | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "type": "ale_limit_reached",
            "message": f"{limit} limit reached ({value:g})",
            "ale": {
                "layer": "gateway",
                "limit": limit,
                "value": value,
                "observed_value": observed_value,
            },
        }
    }
