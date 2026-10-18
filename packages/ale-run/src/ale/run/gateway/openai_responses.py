"""OpenAI Responses dialect helpers."""

from __future__ import annotations

import json
from typing import Any

from ale.run.gateway.session import CallUsage

__all__ = [
    "extract_usage",
    "refusal_body",
    "stream_completed",
    "usage_from_stream",
]


def _call_usage(usage: object) -> CallUsage:
    """Responses ``input_tokens`` already include the cached ones."""
    values = usage if isinstance(usage, dict) else {}
    details = values.get("input_tokens_details") or {}
    return CallUsage(
        input_tokens=int(values.get("input_tokens") or 0),
        cache_read_tokens=int(details.get("cached_tokens") or 0),
        output_tokens=int(values.get("output_tokens") or 0),
    )


def extract_usage(payload: dict[str, Any]) -> tuple[CallUsage, str | None]:
    incomplete = payload.get("incomplete_details") or {}
    return (
        _call_usage(payload.get("usage")),
        incomplete.get("reason") or payload.get("status"),
    )


def usage_from_stream(events: list[bytes]) -> tuple[CallUsage, str | None]:
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
    return CallUsage(), None


def stream_completed(events: list[bytes]) -> bool:
    """Whether a stream reached a terminal response without an error or failure event."""
    terminal = False
    for line in b"".join(events).splitlines():
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:].strip() or b"{}")
        except json.JSONDecodeError:
            continue
        if event.get("type") in {"error", "response.failed"}:
            return False
        terminal = terminal or event.get("type") in {"response.completed", "response.incomplete"}
    return terminal


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
