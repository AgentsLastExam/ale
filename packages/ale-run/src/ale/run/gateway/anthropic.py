"""The Anthropic Messages dialect.

Everything dialect-specific lives here so the server stays about sessions, limits and
tracing. Adding a second dialect means adding a module beside this one, not editing the
server.
"""

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

_USAGE_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def _call_usage(fields: dict[str, int]) -> CallUsage:
    """Anthropic reports uncached input apart from cache reads and cache writes."""
    read = fields["cache_read_input_tokens"]
    write = fields["cache_creation_input_tokens"]
    return CallUsage(
        input_tokens=fields["input_tokens"] + read + write,
        cache_read_tokens=read,
        cache_write_tokens=write,
        output_tokens=fields["output_tokens"],
    )


def _fields(usage: object) -> dict[str, int]:
    values = usage if isinstance(usage, dict) else {}
    return {name: int(values.get(name) or 0) for name in _USAGE_FIELDS}


def extract_usage(payload: dict[str, Any]) -> tuple[CallUsage, str | None]:
    """Read token counts and stop reason from a non-streaming response."""
    return _call_usage(_fields(payload.get("usage"))), payload.get("stop_reason")


def usage_from_stream(events: list[bytes]) -> tuple[CallUsage, str | None]:
    """Read token counts from a server-sent event stream.

    Anthropic reports input and cache tokens in ``message_start`` and the output total in
    ``message_delta``, so both have to be seen; a stream cut short still yields whatever
    was counted before it stopped.

    Not every Anthropic-compatible provider agrees on which event carries what. GLM sends
    zero in ``message_start`` and the real input count in ``message_delta``, so both
    events are read for every field and the larger wins. Trusting one event meant
    recording zero input tokens for every streamed call — which reads as a free request
    rather than an unmeasured one.
    """
    observed = dict.fromkeys(_USAGE_FIELDS, 0)
    stop_reason: str | None = None
    for line in b"".join(events).splitlines():
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:].strip() or b"{}")
        except json.JSONDecodeError:
            continue
        if event.get("type") == "message_start":
            usage = (event.get("message") or {}).get("usage")
        elif event.get("type") == "message_delta":
            usage = event.get("usage")
            stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
        else:
            continue
        for name, value in _fields(usage).items():
            observed[name] = max(observed[name], value)
    return _call_usage(observed), stop_reason


def stream_completed(events: list[bytes]) -> bool:
    """Whether a stream reached ``message_stop`` without an in-band ``error`` event.

    A provider can fail inside an HTTP 200 stream (``overloaded_error``), and a stream can
    be cut short. Neither is a response a retry of the same request may be replayed.
    """
    completed = False
    for line in b"".join(events).splitlines():
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:].strip() or b"{}")
        except json.JSONDecodeError:
            continue
        if event.get("type") == "error":
            return False
        completed = completed or event.get("type") == "message_stop"
    return completed


def refusal_body(
    limit: str,
    value: float,
    observed_value: float | None = None,
) -> dict[str, Any]:
    """The error an agent sees when a ceiling is reached.

    Shaped like a provider error on purpose: harnesses already know how to stop on one,
    so no agent needs to learn about ALE to respect a budget.
    """
    return {
        "type": "error",
        "error": {
            "type": "ale_limit_reached",
            "message": f"{limit} limit reached ({value:g})",
            "ale": {
                "layer": "gateway",
                "limit": limit,
                "value": value,
                "observed_value": observed_value,
            },
        },
    }
