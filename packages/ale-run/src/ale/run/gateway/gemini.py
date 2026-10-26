"""The Gemini API dialect: ``generateContent`` and ``streamGenerateContent``.

Gemini names the model and the method in the request path, not the body, so the server
routes on the path and imposes the model there. What is Gemini-specific about a request
body, a response or an error lives here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ale.run.gateway.session import CallUsage

__all__ = [
    "METHODS",
    "PolicyRefusal",
    "error_body",
    "extract_usage",
    "policy_refusal",
    "refusal_body",
    "stream_completed",
    "usage_from_stream",
]

#: The methods the Gateway forwards, each mapped to whether it streams. Each runs on the
#: run's model and leaves nothing stored with Google; any other method is refused.
METHODS = {"generateContent": False, "streamGenerateContent": True, "countTokens": False}

#: Tools Google runs on its own side. They reach the web outside the Task's network
#: policy and are billed apart from tokens. The API accepts both JSON spellings of a
#: field, so both are matched.
_GROUNDING_TOOLS = frozenset(
    {
        "googleSearch",
        "google_search",
        "googleSearchRetrieval",
        "google_search_retrieval",
        "urlContext",
        "url_context",
        "googleMaps",
        "google_maps",
    }
)


#: Antigravity CLI retries a 429 for minutes but fails a 400 at once, and a Gateway
#: refusal does not lift by waiting, so Gemini refusals are 400 ``FAILED_PRECONDITION``.
REFUSAL_STATUS = 400


@dataclass(frozen=True)
class PolicyRefusal:
    """A request this dialect never forwards, and the Google error that says why."""

    policy: str
    """Recorded as the refused call's ``refusal_limit``."""
    code: int
    status: str
    message: str

    def body(self) -> dict[str, Any]:
        return error_body(self.code, self.status, self.message, f"ALE_{self.policy.upper()}")


def policy_refusal(
    http_method: str,
    target: str,
    alt: str | None,
    payload: dict[str, Any],
    *,
    web_tools: bool = False,
) -> PolicyRefusal | None:
    """Why the request for ``v1beta/{target}`` must not be forwarded, if it must not."""
    resource, _, method = target.rpartition(":")
    model = resource.removeprefix("models/")
    if http_method != "POST" or method not in METHODS or model in {"", resource} or "/" in model:
        return PolicyRefusal(
            "unsupported_method",
            404,
            "NOT_FOUND",
            f"the ALE Gateway forwards only {', '.join(f'models/*:{name}' for name in METHODS)}, "
            f"not {http_method} v1beta/{target}",
        )
    if METHODS[method] and alt != "sse":
        # Usage is read from server-sent events; a JSON-array stream would go unaccounted.
        return PolicyRefusal(
            "unsupported_method",
            400,
            "INVALID_ARGUMENT",
            "the ALE Gateway streams only server-sent events; send alt=sse",
        )
    tools = payload.get("tools")
    entries = tools if isinstance(tools, list) else [tools]
    named = {name for tool in entries if isinstance(tool, dict) for name in tool}
    if not web_tools and (grounding := sorted(named & _GROUNDING_TOOLS)):
        return PolicyRefusal(
            "web_tools",
            REFUSAL_STATUS,
            "FAILED_PRECONDITION",
            f"grounding needs a Task with open network; the ALE Gateway refuses "
            f"{', '.join(grounding)}",
        )
    return None


def _call_usage(metadata: object) -> CallUsage:
    """Gemini's prompt count already includes cached tokens; thoughts are counted apart."""
    values = metadata if isinstance(metadata, dict) else {}

    def count(name: str) -> int:
        return int(values.get(name) or 0)

    return CallUsage(
        input_tokens=count("promptTokenCount") + count("toolUsePromptTokenCount"),
        cache_read_tokens=count("cachedContentTokenCount"),
        output_tokens=count("candidatesTokenCount") + count("thoughtsTokenCount"),
    )


def _stop_reason(response: dict[str, Any]) -> str | None:
    for candidate in response.get("candidates") or ():
        if isinstance(candidate, dict) and candidate.get("finishReason"):
            return candidate["finishReason"]
    feedback = response.get("promptFeedback")
    return feedback.get("blockReason") if isinstance(feedback, dict) else None


def _events(chunks: list[bytes]) -> Iterator[dict[str, Any]]:
    """Each ``data:`` payload of a stream that may have been cut at any byte."""
    for line in b"".join(chunks).splitlines():
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:].strip() or b"{}")
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def extract_usage(payload: dict[str, Any]) -> tuple[CallUsage, str | None]:
    return _call_usage(payload.get("usageMetadata")), _stop_reason(payload)


def usage_from_stream(events: list[bytes]) -> tuple[CallUsage, str | None]:
    """Usage from the last chunk that reports it; each report is cumulative."""
    usage, stop_reason = CallUsage(), None
    for event in _events(events):
        if "usageMetadata" in event:
            usage = _call_usage(event["usageMetadata"])
        stop_reason = _stop_reason(event) or stop_reason
    return usage, stop_reason


def stream_completed(events: list[bytes]) -> bool:
    """Whether a stream reached a ``finishReason`` (or a blocked prompt) without an error.

    Gemini has no terminal event: the last chunk is the one whose candidate finished.
    """
    completed = False
    for event in _events(events):
        if "error" in event:
            return False
        completed = completed or _stop_reason(event) is not None
    return completed


def error_body(
    code: int,
    status: str,
    message: str,
    reason: str,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A Google API error, so a Gemini client reports a Gateway refusal as it would its own."""
    info = {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": reason,
        "domain": "ale.gateway",
        "metadata": metadata or {},
    }
    return {"error": {"code": code, "message": message, "status": status, "details": [info]}}


def refusal_body(
    limit: str,
    value: float,
    observed_value: float | None = None,
) -> dict[str, Any]:
    observed = value if observed_value is None else observed_value
    return error_body(
        REFUSAL_STATUS,
        "FAILED_PRECONDITION",
        f"{limit} limit reached ({value:g})",
        "ALE_LIMIT_REACHED",
        {
            "layer": "gateway",
            "limit": limit,
            "value": f"{value:g}",
            "observed_value": f"{observed:g}",
        },
    )
