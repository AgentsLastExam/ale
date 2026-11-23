"""The Gemini API dialect: ``generateContent`` and ``streamGenerateContent``.

Gemini names the model and the method in the request path, not the body, so the server
routes on the path and imposes the model there. What is Gemini-specific about a request
body, a response or an error lives here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from orbit.run.gateway.session import CallUsage

__all__ = [
    "METHODS",
    "PolicyRefusal",
    "error_body",
    "extract_usage",
    "policy_body",
    "policy_refusal",
    "quota",
    "refusal_body",
    "stream_completed",
    "stream_error",
    "usage_from_stream",
    "web_tools",
]

#: The methods the Gateway forwards, each mapped to whether it streams. Each runs on the
#: run's model and leaves nothing stored with Google; any other method is refused.
METHODS = {"generateContent": False, "streamGenerateContent": True, "countTokens": False}

#: Tools Google runs on its own side that reach the network: grounding, which is also
#: billed apart from tokens, and remote MCP servers. The API accepts both JSON spellings
#: of a field, so both are matched.
_WEB_TOOLS = frozenset(
    {
        "googleSearch",
        "google_search",
        "googleSearchRetrieval",
        "google_search_retrieval",
        "urlContext",
        "url_context",
        "googleMaps",
        "google_maps",
        "mcpServers",
        "mcp_servers",
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
        return error_body(self.code, self.status, self.message, f"ORBIT_{self.policy.upper()}")


def policy_refusal(http_method: str, target: str, alt: str | None) -> PolicyRefusal | None:
    """Why the request for ``v1beta/{target}`` must not be forwarded, if it must not."""
    resource, _, method = target.rpartition(":")
    model = resource.removeprefix("models/")
    if http_method != "POST" or method not in METHODS or model in {"", resource} or "/" in model:
        return PolicyRefusal(
            "unsupported_method",
            404,
            "NOT_FOUND",
            f"the Orbit Gateway forwards only {', '.join(f'models/*:{name}' for name in METHODS)}, "
            f"not {http_method} v1beta/{target}",
        )
    if METHODS[method] and alt != "sse":
        # Usage is read from server-sent events; a JSON-array stream would go unaccounted.
        return PolicyRefusal(
            "unsupported_method",
            400,
            "INVALID_ARGUMENT",
            "the Orbit Gateway streams only server-sent events; send alt=sse",
        )
    return None


def web_tools(payload: dict[str, Any]) -> list[str]:
    """What a request asks Google to reach on the network: a grounding or remote MCP tool,
    or a file given by URL, which Google downloads."""
    tools = payload.get("tools")
    entries = tools if isinstance(tools, list) else [tools]
    named = {name for tool in entries if isinstance(tool, dict) for name in tool} & _WEB_TOOLS
    for content in _dicts(payload.get("contents")):
        for part in _dicts(content.get("parts")):
            response = part.get("functionResponse") or part.get("function_response")
            inner = _dicts(response.get("parts")) if isinstance(response, dict) else []
            for each in (part, *inner):
                data = each.get("fileData") or each.get("file_data")
                uri = (
                    (data.get("fileUri") or data.get("file_uri"))
                    if isinstance(data, dict)
                    else None
                )
                if isinstance(uri, str) and uri.lower().startswith(("http://", "https://")):
                    named.add("fileData url")
    return sorted(named)


def _dicts(value: object) -> list[dict[str, Any]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def policy_body(policy: str, message: str) -> dict[str, Any]:
    """A request policy's refusal, as the Google error a Gemini client reports at once."""
    return error_body(REFUSAL_STATUS, "FAILED_PRECONDITION", message, f"ORBIT_{policy.upper()}")


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


def stream_error(events: list[bytes]) -> dict[str, Any] | None:
    """The error object a stream failed with, if it did."""
    return next((event for event in _events(events) if "error" in event), None)


def quota(error: dict[str, Any], headers: Mapping[str, str]) -> bool:
    """Whether the provider failed a call for a quota the project used up for the day, which
    a ``QuotaFailure`` names per day and which lifts tomorrow; a per-minute quota is a rate
    limit."""
    detail = error.get("error") if isinstance(error.get("error"), dict) else error
    return any(
        "PerDay" in str(violation.get("quotaId", ""))
        for entry in _dicts(detail.get("details"))
        if str(entry.get("@type", "")).endswith("google.rpc.QuotaFailure")
        for violation in _dicts(entry.get("violations"))
    )


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
        "domain": "orbit.gateway",
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
        "ORBIT_LIMIT_REACHED",
        {
            "layer": "gateway",
            "limit": limit,
            "value": f"{value:g}",
            "observed_value": f"{observed:g}",
        },
    )
