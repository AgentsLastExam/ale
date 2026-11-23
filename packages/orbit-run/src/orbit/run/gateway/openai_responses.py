"""OpenAI Responses dialect helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from orbit.run.gateway.session import CallUsage

__all__ = [
    "extract_usage",
    "policy_body",
    "quota",
    "refusal_body",
    "stream_completed",
    "stream_error",
    "usage_from_stream",
    "web_tools",
]

#: Hosted tool types that reach the network from the provider's side: web search in every
#: version and preview spelling, which all start with ``web_search``; remote MCP servers
#: and connectors; xAI's X search; and Qwen's page extractor and image search.
_WEB_TOOL_PREFIX = "web_search"
_WEB_TOOL_TYPES = frozenset({"mcp", "x_search", "web_extractor", "image_search"})
#: Hosted tools that run code in a provider container, which reaches the network only
#: under a ``network_policy`` that allows it.
_CONTAINER_TOOLS = {"shell": "environment", "code_interpreter": "container"}


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


def stream_error(events: list[bytes]) -> dict[str, Any] | None:
    """The error a stream failed with, in an ``error`` event or a ``response.failed``, if it
    did."""
    for line in b"".join(events).splitlines():
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:].strip() or b"{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            return event
        if event.get("type") == "response.failed":
            response = event.get("response")
            error = response.get("error") if isinstance(response, dict) else None
            return error if isinstance(error, dict) else event
    return None


#: The error codes and types with which OpenAI's API and ChatGPT's backend refuse a call for
#: the account's quota or usage limit.
_QUOTA = frozenset({"insufficient_quota", "usage_limit_reached", "usage_not_included"})


def quota(error: dict[str, Any], headers: Mapping[str, str]) -> bool:
    """Whether the provider failed a call for the account's own limit, which lifts in hours
    or with billing rather than within a retry."""
    detail = error.get("error") if isinstance(error.get("error"), dict) else error
    return bool({detail.get("code"), detail.get("type")} & _QUOTA)


def web_tools(payload: dict[str, Any]) -> list[str]:
    """What a request asks the provider to reach on the network: a hosted tool that does,
    in the request's tools or in tools an input item adds, or an image or file given by
    URL, which the provider downloads."""
    items = _dicts(payload.get("input"))
    tools = [
        *_dicts(payload.get("tools")),
        *(t for item in items for t in _dicts(item.get("tools"))),
    ]
    named: set[str] = set()
    for tool in tools:
        kind = tool.get("type")
        if not isinstance(kind, str):
            continue
        if kind.startswith(_WEB_TOOL_PREFIX) or kind in _WEB_TOOL_TYPES:
            named.add(kind)
        elif kind in _CONTAINER_TOOLS:
            place = tool.get(_CONTAINER_TOOLS[kind])
            policy = place.get("network_policy") if isinstance(place, dict) else None
            if isinstance(policy, dict) and policy.get("type") not in (None, "disabled"):
                named.add(f"{kind} network_policy {policy.get('type')}")
    for item in items:
        for part in (item, *_dicts(item.get("content")), *_dicts(item.get("output"))):
            if part.get("type") == "input_image" and _remote(part.get("image_url")):
                named.add("input_image url")
            elif part.get("type") == "input_file" and part.get("file_url"):
                named.add("input_file url")
    return sorted(named)


def _remote(url: object) -> bool:
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def _dicts(value: object) -> list[dict[str, Any]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def policy_body(policy: str, message: str) -> dict[str, Any]:
    """A request policy's refusal, as the invalid request it is to an OpenAI client."""
    return {
        "error": {
            "type": "invalid_request_error",
            "code": f"orbit_{policy}",
            "message": message,
            "orbit": {"layer": "gateway", "policy": policy},
        }
    }


def refusal_body(
    limit: str,
    value: float,
    observed_value: float | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "type": "orbit_limit_reached",
            "message": f"{limit} limit reached ({value:g})",
            "orbit": {
                "layer": "gateway",
                "limit": limit,
                "value": value,
                "observed_value": observed_value,
            },
        }
    }
