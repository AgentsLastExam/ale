"""The Anthropic Messages dialect.

Everything dialect-specific lives here so the server stays about sessions, limits and
tracing. Adding a second dialect means adding a module beside this one, not editing the
server.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["USD_PER_MTOK", "estimate_cost", "extract_usage", "refusal_body", "usage_from_stream"]

#: Rough per-million-token prices, used for budget ceilings rather than billing.
#: Being approximately right stops a runaway run; being exactly right is the invoice's
#: job, and pretending otherwise would mean tracking a price list we do not own.
USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "default": (5.0, 25.0),
    "haiku": (1.0, 5.0),
    "sonnet": (3.0, 15.0),
    "opus": (5.0, 25.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Approximate the dollar cost of one call."""
    lowered = model.lower()
    rates = next(
        (value for key, value in USD_PER_MTOK.items() if key != "default" and key in lowered),
        USD_PER_MTOK["default"],
    )
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


def extract_usage(payload: dict[str, Any]) -> tuple[int, int, str | None]:
    """Read token counts and stop reason from a non-streaming response."""
    usage = payload.get("usage") or {}
    return (
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        payload.get("stop_reason"),
    )


def usage_from_stream(events: list[bytes]) -> tuple[int, int, str | None]:
    """Read token counts from a server-sent event stream.

    Anthropic reports input tokens in ``message_start`` and the output total in
    ``message_delta``, so both have to be seen; a stream cut short still yields whatever
    was counted before it stopped.

    Not every Anthropic-compatible provider agrees on which event carries what. GLM sends
    zero in ``message_start`` and the real input count in ``message_delta``, so both
    events are read for both numbers and the larger wins. Trusting one event meant
    recording zero input tokens for every streamed call — which reads as a free request
    rather than an unmeasured one.
    """
    input_tokens = output_tokens = 0
    stop_reason: str | None = None
    for chunk in events:
        for line in chunk.splitlines():
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:].strip() or b"{}")
            except json.JSONDecodeError:
                continue
            if event.get("type") == "message_start":
                usage = (event.get("message") or {}).get("usage") or {}
            elif event.get("type") == "message_delta":
                usage = event.get("usage") or {}
                stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
            else:
                continue

            input_tokens = max(input_tokens, int(usage.get("input_tokens") or 0))
            output_tokens = max(output_tokens, int(usage.get("output_tokens") or 0))
    return input_tokens, output_tokens, stop_reason


def refusal_body(limit: str, value: float) -> dict[str, Any]:
    """The error an agent sees when a ceiling is reached.

    Shaped like a provider error on purpose: harnesses already know how to stop on one,
    so no agent needs to learn about ALE to respect a budget.
    """
    return {
        "type": "error",
        "error": {
            "type": "ale_limit_reached",
            "message": f"{limit} limit reached ({value:g})",
            "ale": {"limit": limit, "value": value},
        },
    }
