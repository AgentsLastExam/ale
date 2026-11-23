"""OpenAI Chat Completions dialect helpers."""

from __future__ import annotations

import json
from typing import Any

from ale.run.gateway.openai_responses import policy_body, quota, refusal_body
from ale.run.gateway.session import CallUsage

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

#: Tools a provider runs on its own side: Z.ai's ``web_search``, and Moonshot's built-in
#: functions, such as ``$web_search``.
_WEB_TOOL_TYPES = frozenset({"web_search", "builtin_function"})
#: Content parts the provider downloads when they name a URL rather than carry data.
_URL_PARTS = ("image_url", "video_url")

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


def web_tools(payload: dict[str, Any]) -> list[str]:
    """What a request asks the provider to reach on the network: a search field or tool,
    or an image or video given by URL, which the provider downloads."""
    named: set[str] = set()
    # OpenAI's search options, DashScope's search switch, and xAI's Live Search unless off.
    if payload.get("web_search_options") is not None:
        named.add("web_search_options")
    if payload.get("enable_search"):
        named.add("enable_search")
    search = payload.get("search_parameters")
    if search is not None and not (isinstance(search, dict) and search.get("mode") == "off"):
        named.add("search_parameters")
    for tool in _dicts(payload.get("tools")):
        if tool.get("type") in _WEB_TOOL_TYPES:
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            named.add(" ".join(str(part) for part in (tool["type"], name) if part))
    for message in _dicts(payload.get("messages")):
        for part in _dicts(message.get("content")):
            kind = part.get("type")
            if kind in _URL_PARTS and _remote(part.get(kind)):
                named.add(f"{kind} url")
    return sorted(named)


def _remote(part: object) -> bool:
    url = part.get("url") if isinstance(part, dict) else part
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def _dicts(value: object) -> list[dict[str, Any]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


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


def stream_error(events: list[bytes]) -> dict[str, Any] | None:
    """The in-band ``error`` object a stream failed with, if it did."""
    for line in b"".join(events).splitlines():
        if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
            continue
        try:
            event = json.loads(line[5:].strip() or b"{}")
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("error"):
            return event
    return None


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
