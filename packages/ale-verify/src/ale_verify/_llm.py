"""Direct synchronous LLM Judge adapters."""

from __future__ import annotations

import base64
import email.utils
import http.client
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._io import (
    ConfigurationError,
    EvidenceError,
    JudgeError,
    digest,
    endpoint_identity,
    media_type,
    redact,
    sanitize,
)
from ._records import (
    MAX_RAW_JSON_BYTES,
    EvidenceReference,
    JudgeAttempt,
    JudgeInvocation,
    ScoredChoice,
)

#: Invalid or refused verdicts get at most this many further attempts. Transient provider
#: failures are retried without a count: a Judge decides a score and must finish, so only
#: verification's own deadline (ALE_VERIFY_DEADLINE_SECONDS) stops them. Without one, as
#: when an author runs verify.py directly, they are retried this many times instead.
REPAIRS = 3
RETRIES_WITHOUT_DEADLINE = 3
#: Silence on an open response stream after which the connection is treated as dropped.
IDLE_TIMEOUT_SECONDS = 600
_DEADLINE_MARGIN_SECONDS = 30
_BACKOFF_CAP_SECONDS = 60
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
_ANTHROPIC_MAX_TOKENS = 128_000
_EFFORTS = {"low", "medium", "high"}
_STARTED = time.monotonic()
_max_output: dict[tuple[str, str], int] = {}


class _Transient(Exception):
    """A provider or network failure worth retrying with the same request."""

    def __init__(
        self, message: str, retry_after: float | None = None, *, timed_out: bool = False
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.timed_out = timed_out


def resolve_protocol(config: Mapping[str, str]) -> str:
    parsed = urlsplit(config["base_url"])
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    model = config["model"].lower()
    if path.endswith("/chat/completions"):
        return "openai-chat-completions"
    if path.endswith("/responses"):
        return "openai-responses"
    if path.endswith("/messages"):
        return "anthropic"
    if "anthropic" in host or "anthropic" in path or model.startswith("claude"):
        if "openai" in host or model.startswith(("gpt-", "o1", "o3", "o4")):
            raise ValueError("cannot safely infer the LLM Judge protocol")
        return "anthropic"
    if "openai" in host or model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai-responses"
    raise ValueError("cannot safely infer the LLM Judge protocol from base_url and model")


def run(
    *,
    invocation_id: str,
    name: str,
    prompt: str,
    rubric: Mapping[str, ScoredChoice],
    evidence: Sequence[tuple[str | bytes, EvidenceReference]],
    config: Mapping[str, str],
) -> tuple[str, str, JudgeInvocation]:
    result, invocation = _run_response(
        invocation_id=invocation_id,
        name=name,
        rendered=_prompt(name, prompt, rubric, evidence),
        rubric_hash=digest(_rubric_json(rubric)),
        evidence=evidence,
        config=config,
        parse=lambda text: _verdict(text, rubric),
        repair_prompt=lambda rendered, error: _repair_prompt(rendered, rubric, error),
        reject_secret=False,
    )
    choice, reasoning = result
    return choice, sanitize(reasoning, (os.environ.get(config["api_key_env"], ""),)), invocation


def run_json(
    *,
    invocation_id: str,
    name: str,
    prompt: str,
    evidence: Sequence[tuple[str | bytes, EvidenceReference]],
    config: Mapping[str, str],
) -> tuple[dict[str, Any], JudgeInvocation]:
    """Request one task-defined JSON object through the normal LLM Judge transport."""
    rendered = _json_prompt(prompt, evidence)
    return _run_response(
        invocation_id=invocation_id,
        name=name,
        rendered=rendered,
        rubric_hash=digest('{"format":"json_object"}'),
        evidence=evidence,
        config=config,
        parse=_json_object,
        repair_prompt=lambda original, error: (
            f"{original}\n\nYour previous response was rejected: {error}\n"
            "Return one valid JSON object, with no Markdown."
        ),
        reject_secret=True,
    )


def _run_response(
    *,
    invocation_id: str,
    name: str,
    rendered: str,
    rubric_hash: str,
    evidence: Sequence[tuple[str | bytes, EvidenceReference]],
    config: Mapping[str, str],
    parse: Callable[[str], Any],
    repair_prompt: Callable[[str, str], str],
    reject_secret: bool,
) -> tuple[Any, JudgeInvocation]:
    protocol = resolve_protocol(config)
    effort = config["reasoning_effort"]
    if effort not in _EFFORTS:
        raise ValueError(f"reasoning_effort {effort!r} is unsupported")
    prompt_hash = digest(rendered)
    endpoint = endpoint_identity(config["base_url"])
    secret = os.environ.get(config["api_key_env"], "")
    if not secret:
        invocation = _failed_preflight(
            invocation_id,
            name,
            config,
            endpoint,
            prompt_hash,
            rubric_hash,
            f"{config['api_key_env']} is not set for the LLM Judge",
        )
        raise JudgeError(invocation.failure or "LLM Judge configuration failed", invocation)

    attempts: list[JudgeAttempt] = []
    last_error = ""
    repair = False
    repairs = 0
    transient = 0
    index = 0
    while True:
        index += 1
        attempt_mode = "initial" if index == 1 else "schema_repair" if repair else "retry"
        request_prompt = repair_prompt(rendered, last_error) if repair else rendered
        started = _now()
        request_id = None
        usage = None
        try:
            payload = _request(protocol, config, request_prompt, secret, evidence)
            request_id = _request_id(payload, secret)
            usage = _usage(payload)
            text = _response_text(protocol, payload)
            result = parse(text)
            if reject_secret and secret and _contains_secret(result, secret):
                raise ValueError("Judge response contains a credential")
        except Exception as exc:
            last_error = sanitize(exc, (secret,))
            outcome = _outcome(exc)
            attempts.append(
                JudgeAttempt(
                    index=index,
                    mode=attempt_mode,
                    started_at=started,
                    finished_at=_now(),
                    outcome=outcome,
                    model=config["model"],
                    reasoning_effort=effort,
                    endpoint_identity=endpoint,
                    prompt_hash=prompt_hash,
                    rubric_hash=rubric_hash,
                    request_id=request_id,
                    usage=usage,
                    error=last_error,
                )
            )
            if isinstance(exc, _Transient):
                transient += 1
                if not _wait_to_retry(transient, exc.retry_after):
                    break
                continue
            if outcome in {"invalid", "refused"} and repairs < REPAIRS:
                repairs += 1
                repair = outcome == "invalid"
                continue
            break
        attempts.append(
            JudgeAttempt(
                index=index,
                mode=attempt_mode,
                started_at=started,
                finished_at=_now(),
                outcome="completed",
                model=config["model"],
                reasoning_effort=effort,
                endpoint_identity=endpoint,
                prompt_hash=prompt_hash,
                rubric_hash=rubric_hash,
                request_id=request_id,
                usage=usage,
            )
        )
        return (
            result,
            JudgeInvocation(
                id=invocation_id,
                kind="llm",
                criterion_name=name,
                status="completed",
                attempts=tuple(attempts),
            ),
        )

    failure = sanitize(f"LLM Judge produced no valid verdict after {index} attempts: {last_error}")
    invocation = JudgeInvocation(
        id=invocation_id,
        kind="llm",
        criterion_name=name,
        status="failed",
        attempts=tuple(attempts),
        failure=failure,
    )
    raise JudgeError(failure, invocation)


def _request(
    protocol: str,
    config: Mapping[str, str],
    prompt: str,
    secret: str,
    evidence: Sequence[tuple[str | bytes, EvidenceReference]] = (),
) -> dict[str, Any]:
    """One Judge request, streamed, without an output ceiling of ALE's choosing.

    Streaming keeps a long reasoning response alive on networks that drop idle
    connections; the stream is read back into the provider's non-streaming shape.
    Anthropic requires max_tokens, so it receives the model's own maximum.
    """
    content = _content(protocol, prompt, evidence)
    secrets = (
        secret,
        *(base64.b64encode(raw).decode("ascii") for raw, _ in evidence if isinstance(raw, bytes)),
    )
    if protocol == "anthropic":
        while True:
            maximum = _anthropic_max_tokens(config, secret)
            payload = {
                "model": config["model"],
                "max_tokens": maximum,
                "output_config": {"effort": config["reasoning_effort"]},
                "messages": [{"role": "user", "content": content}],
                "stream": True,
            }
            try:
                return _post(protocol, config, payload, _anthropic_headers(secret), secrets)
            except _MaxTokensRejected as rejected:
                _max_output[(config["base_url"], config["model"])] = rejected.supported(maximum)
    if protocol == "openai-chat-completions":
        payload = {
            "model": config["model"],
            "messages": [{"role": "user", "content": content}],
            "reasoning_effort": config["reasoning_effort"],
            "response_format": {"type": "json_object"},
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    else:
        payload = {
            "model": config["model"],
            "input": content
            if isinstance(content, str)
            else [{"role": "user", "content": content}],
            "reasoning": {"effort": config["reasoning_effort"]},
            "stream": True,
        }
    return _post(protocol, config, payload, _bearer_headers(secret), secrets)


class _MaxTokensRejected(Exception):
    """The provider refused max_tokens as above this model's output limit."""

    def __init__(self, body: str) -> None:
        super().__init__(body)
        found = re.search(r"max_tokens:\s*\d+\s*>\s*(\d+)", body)
        self.maximum = int(found.group(1)) if found else None

    def supported(self, tried: int) -> int:
        if self.maximum is not None and self.maximum < tried:
            return self.maximum
        if tried <= 4096:
            raise RuntimeError(f"provider rejected max_tokens: {self.args[0][:500]}")
        return max(4096, tried // 2)


def _anthropic_headers(secret: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-api-key": secret,
        "anthropic-version": "2023-06-01",
    }


def _bearer_headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}


def _anthropic_max_tokens(config: Mapping[str, str], secret: str) -> int:
    """The model's own output limit, from the Models API when the endpoint offers it."""
    key = (config["base_url"], config["model"])
    if key not in _max_output:
        url = _endpoint(config["base_url"], "anthropic").removesuffix("/messages")
        request = urllib.request.Request(
            f"{url}/models/{config['model']}", headers=_anthropic_headers(secret), method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                reported = json.load(response).get("max_tokens")
        except Exception:
            reported = None
        _max_output[key] = (
            reported if isinstance(reported, int) and reported > 0 else _ANTHROPIC_MAX_TOKENS
        )
    return _max_output[key]


def _post(
    protocol: str,
    config: Mapping[str, str],
    payload: Mapping[str, object],
    headers: Mapping[str, str],
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    request = urllib.request.Request(
        _endpoint(config["base_url"], protocol),
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=IDLE_TIMEOUT_SECONDS) as response:
            content_type = getattr(response, "headers", {}).get("Content-Type", "") or ""
            if "text/event-stream" in content_type:
                loaded = _read_stream(protocol, response)
            else:
                loaded = json.load(response)
    except urllib.error.HTTPError as exc:
        body = redact(exc.read().decode("utf-8", errors="replace"), secrets)
        message = f"provider returned HTTP {exc.code}: {body[:500]}"
        if exc.code in _RETRYABLE_STATUS:
            raise _Transient(message, _retry_after(exc.headers)) from exc
        if protocol == "anthropic" and exc.code == 400 and "max_tokens" in body:
            raise _MaxTokensRejected(body) from exc
        raise RuntimeError(message) from exc
    except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as exc:
        timed_out = isinstance(exc, TimeoutError) or isinstance(
            getattr(exc, "reason", None), TimeoutError
        )
        raise _Transient(f"connection failed: {redact(exc, secrets)}", timed_out=timed_out) from exc
    except json.JSONDecodeError as exc:
        raise _Transient(f"provider sent a truncated or malformed response: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("provider response must be a JSON object")
    return loaded


def _events(lines: Iterable[bytes]) -> Iterable[dict[str, Any]]:
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _read_stream(protocol: str, lines: Iterable[bytes]) -> dict[str, Any]:
    """Rebuild the provider's non-streaming response from its event stream."""
    if protocol == "anthropic":
        message: dict[str, Any] = {"content": [], "usage": {}}
        blocks: dict[int, dict[str, Any]] = {}
        for event in _events(lines):
            kind = event.get("type")
            if kind == "error":
                raise _Transient(f"provider stream failed: {json.dumps(event.get('error'))[:500]}")
            if kind == "message_start":
                started = event.get("message") or {}
                message["id"] = started.get("id")
                message["usage"].update(started.get("usage") or {})
            elif kind == "content_block_start":
                block = dict(event.get("content_block") or {})
                blocks[int(event.get("index", len(blocks)))] = block
            elif kind == "content_block_delta":
                delta = event.get("delta") or {}
                block = blocks.setdefault(int(event.get("index", 0)), {"type": "text", "text": ""})
                if delta.get("type") == "text_delta":
                    block["text"] = block.get("text", "") + str(delta.get("text", ""))
            elif kind == "message_delta":
                message["stop_reason"] = (event.get("delta") or {}).get("stop_reason")
                message["usage"].update(event.get("usage") or {})
            elif kind == "message_stop":
                message["content"] = [blocks[index] for index in sorted(blocks)]
                return message
        raise _Transient("provider stream ended before message_stop")
    if protocol == "openai-chat-completions":
        text, refusal, usage, finish, identifier = [], [], None, None, None
        for event in _events(lines):
            if event.get("error"):
                raise _Transient(f"provider stream failed: {json.dumps(event['error'])[:500]}")
            identifier = event.get("id") or identifier
            usage = event.get("usage") or usage
            for choice in event.get("choices") or ():
                delta = choice.get("delta") or {}
                text.append(str(delta.get("content") or ""))
                refusal.append(str(delta.get("refusal") or ""))
                finish = choice.get("finish_reason") or finish
        if finish is None:
            raise _Transient("provider stream ended without a finish reason")
        message_body = {"role": "assistant", "content": "".join(text)}
        if "".join(refusal):
            message_body["refusal"] = "".join(refusal)
        return {
            "id": identifier,
            "choices": [{"message": message_body, "finish_reason": finish}],
            "usage": usage,
        }
    for event in _events(lines):
        kind = event.get("type")
        if kind == "error":
            raise _Transient(f"provider stream failed: {json.dumps(event)[:500]}")
        if kind == "response.failed":
            error = (event.get("response") or {}).get("error")
            raise _Transient(f"provider response failed: {json.dumps(error)[:500]}")
        if kind in {"response.completed", "response.incomplete"}:
            return dict(event.get("response") or {})
    raise _Transient("provider stream ended before the response completed")


def _retry_after(headers: object) -> float | None:
    if headers is None:
        return None
    milliseconds = headers.get("retry-after-ms")  # type: ignore[attr-defined]
    if milliseconds:
        try:
            return float(milliseconds) / 1000
        except ValueError:
            pass
    value = headers.get("retry-after")  # type: ignore[attr-defined]
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None  # unreadable: the ordinary backoff applies
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


def _backoff(failures: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, 600.0)
    return random.uniform(0.5, 1.0) * min(_BACKOFF_CAP_SECONDS, 2 ** min(failures, 6))


def _wait_to_retry(failures: int, retry_after: float | None = None) -> bool:
    """Sleep before another attempt; False when verification's budget cannot cover it."""
    delay = _backoff(failures, retry_after)
    left = _time_left()
    if left is None and failures > RETRIES_WITHOUT_DEADLINE:
        return False
    if left is not None and left < delay:
        return False
    time.sleep(delay)
    return True


def _time_left() -> float | None:
    """Seconds verification may still spend on retries, or None without a deadline."""
    budget = os.environ.get("ALE_VERIFY_DEADLINE_SECONDS")
    if not budget:
        return None
    elapsed = time.monotonic() - _STARTED
    return float(budget) - elapsed - _DEADLINE_MARGIN_SECONDS


def probe(config: Mapping[str, str], protocol: str | None = None) -> None:
    """Prove a Judge endpoint, key and model answer one tiny request.

    The reasoning effort is sent only when the config names one. Raises ConfigurationError
    with a sanitized reason. Transient failures get a few retries: a probe is a gate run
    before agents start, not a Judge that must finish.
    """
    protocol = protocol or resolve_protocol(config)
    secret = os.environ.get(config["api_key_env"], "")
    if not secret:
        raise ConfigurationError(f"{config['api_key_env']} is not set for the Judge")
    prompt = "Reply with the single word OK."
    effort = config.get("reasoning_effort")
    if protocol == "anthropic":
        payload: dict[str, object] = {
            "model": config["model"],
            "max_tokens": 64,
            "messages": [{"role": "user", "content": prompt}],
        }
        if effort:
            payload["output_config"] = {"effort": effort}
        headers = _anthropic_headers(secret)
    elif protocol == "openai-chat-completions":
        payload = {
            "model": config["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": 64,
        }
        if effort:
            payload["reasoning_effort"] = effort
        headers = _bearer_headers(secret)
    else:
        payload = {"model": config["model"], "input": prompt, "max_output_tokens": 64}
        if effort:
            payload["reasoning"] = {"effort": effort}
        headers = _bearer_headers(secret)
    where = endpoint_identity(config["base_url"])
    for attempt in range(1, 4):
        try:
            _post(protocol, config, payload, headers, (secret,))
            return
        except _Transient as exc:
            if attempt == 3:
                raise ConfigurationError(
                    f"Judge endpoint {where} is unavailable: {sanitize(exc, (secret,))}"
                ) from exc
            time.sleep(_backoff(attempt, exc.retry_after))
        except Exception as exc:
            raise ConfigurationError(
                f"Judge endpoint {where} rejected model {config['model']!r}: "
                f"{sanitize(exc, (secret,))}"
            ) from exc


def _content(
    protocol: str,
    prompt: str,
    evidence: Sequence[tuple[str | bytes, EvidenceReference]],
) -> str | list[dict[str, Any]]:
    attachments = [(raw, item) for raw, item in evidence if isinstance(raw, bytes)]
    if not attachments:
        return prompt
    text_type = "input_text" if protocol == "openai-responses" else "text"
    parts: list[dict[str, Any]] = [{"type": text_type, "text": prompt}]
    for raw, item in attachments:
        mime = media_type(raw)
        if mime is None:
            raise EvidenceError(f"unsupported binary LLM evidence: {item.location}")
        encoded = base64.b64encode(raw).decode("ascii")
        url = f"data:{mime};base64,{encoded}"
        filename = Path(item.location or "evidence").name
        parts.append({"type": text_type, "text": f"Evidence: {item.location} ({item.sha256})"})
        if protocol == "anthropic":
            parts.append(
                {
                    "type": "document" if mime == "application/pdf" else "image",
                    "source": {"type": "base64", "media_type": mime, "data": encoded},
                }
            )
        elif protocol == "openai-chat-completions":
            parts.append(
                {"type": "file", "file": {"filename": filename, "file_data": url}}
                if mime == "application/pdf"
                else {"type": "image_url", "image_url": {"url": url}}
            )
        else:
            parts.append(
                {"type": "input_file", "filename": filename, "file_data": url}
                if mime == "application/pdf"
                else {"type": "input_image", "image_url": url}
            )
    return parts


def _endpoint(base_url: str, protocol: str) -> str:
    base = base_url.rstrip("/")
    suffix = {
        "anthropic": "/v1/messages",
        "openai-chat-completions": "/v1/chat/completions",
        "openai-responses": "/v1/responses",
    }[protocol]
    if base.endswith(suffix) or base.endswith(suffix.removeprefix("/v1")):
        return base
    return base + (suffix.removeprefix("/v1") if base.endswith("/v1") else suffix)


def _prompt(
    name: str,
    prompt: str,
    rubric: Mapping[str, ScoredChoice],
    evidence: Sequence[tuple[str | bytes, EvidenceReference]],
) -> str:
    choices = "|".join(rubric)
    sections = [
        "Return JSON only. Do not include Markdown or additional keys.",
        f'Exact response: {{"choice":"{choices}","reasoning":"..."}}',
        f"Criterion: {name}",
        f"Task: {prompt}",
        f"Rubric: {_rubric_json(rubric)}",
    ]
    if evidence:
        sections.append(
            "Evidence:\n"
            + "\n\n".join(
                f"{reference.kind.upper()} {reference.location or ''} ({reference.sha256})\n"
                + (text if isinstance(text, str) else "Binary content is attached to this request.")
                for text, reference in evidence
            )
        )
    return "\n\n".join(sections)


def _json_prompt(
    prompt: str,
    evidence: Sequence[tuple[str | bytes, EvidenceReference]],
) -> str:
    sections = [prompt]
    if evidence:
        sections.append(
            "Evidence:\n"
            + "\n\n".join(
                f"{reference.kind.upper()} {reference.location or ''} ({reference.sha256})\n"
                + (text if isinstance(text, str) else "Binary content is attached to this request.")
                for text, reference in evidence
            )
        )
    return "\n\n".join(sections)


def _json_object(text: str) -> dict[str, Any]:
    fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, flags=re.S)
    try:
        loaded = json.loads(fenced.group(1) if fenced else text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response is not valid JSON: {exc.msg}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("response must be a JSON object")
    try:
        encoded = json.dumps(loaded, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("response must contain finite JSON values") from exc
    if len(encoded) > MAX_RAW_JSON_BYTES:
        raise ValueError(f"response exceeds {MAX_RAW_JSON_BYTES} bytes")
    return loaded


def _contains_secret(value: Any, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(
            _contains_secret(key, secret) or _contains_secret(item, secret)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item, secret) for item in value)
    return False


def _repair_prompt(
    original: str,
    rubric: Mapping[str, ScoredChoice],
    error: str,
) -> str:
    choices = "|".join(rubric)
    return (
        f"{original}\n\nYour previous verdict was rejected:\n{error}\n\n"
        f'Submit exactly:\n{{"choice":"{choices}","reasoning":"..."}}\n'
        "Do not include Markdown or additional keys."
    )


def _rubric_json(rubric: Mapping[str, ScoredChoice]) -> str:
    return json.dumps(
        {
            label: {"score": choice.score, "description": choice.description}
            for label, choice in rubric.items()
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _response_text(protocol: str, payload: Mapping[str, Any]) -> str:
    if protocol == "anthropic":
        if payload.get("stop_reason") == "refusal":
            raise PermissionError("provider refused the Judge request")
        text = "".join(
            item.get("text", "")
            for item in payload.get("content", ())
            if isinstance(item, dict) and item.get("type") == "text"
        )
    elif protocol == "openai-chat-completions":
        choices = payload.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else {}
        message = first.get("message", {}) if isinstance(first, dict) else {}
        if isinstance(message, dict) and message.get("refusal"):
            raise PermissionError("provider refused the Judge request")
        text = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(text, list):
            text = "".join(
                item.get("text", "")
                for item in text
                if isinstance(item, dict) and item.get("type") == "text"
            )
    else:
        parts = [
            part
            for item in payload.get("output", ())
            if isinstance(item, dict)
            for part in item.get("content", ())
            if isinstance(part, dict)
        ]
        if any(part.get("type") == "refusal" for part in parts):
            raise PermissionError("provider refused the Judge request")
        text = payload.get("output_text", "") or "".join(
            part.get("text", "") for part in parts if part.get("type") == "output_text"
        )
        if not text and payload.get("status") == "incomplete":
            reason = (payload.get("incomplete_details") or {}).get("reason")
            raise ValueError(f"Judge response is incomplete ({reason})")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Judge response contains no verdict text")
    return text


def _verdict(text: str, rubric: Mapping[str, ScoredChoice]) -> tuple[str, str]:
    fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, flags=re.S)
    try:
        loaded = json.loads(fenced.group(1) if fenced else text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"verdict is not valid JSON: {exc.msg}") from exc
    if not isinstance(loaded, dict) or set(loaded) != {"choice", "reasoning"}:
        raise ValueError("verdict must contain exactly choice and reasoning")
    choice = loaded["choice"]
    reasoning = loaded["reasoning"]
    if choice not in rubric:
        raise ValueError(f"unknown choice {choice!r}; expected one of {list(rubric)}")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("verdict reasoning must be a non-empty string")
    return choice, reasoning


def _usage(payload: Mapping[str, Any]) -> dict[str, int] | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    normalized = {
        str(key): int(value)
        for key, value in usage.items()
        if isinstance(value, int) and value >= 0
    }
    return normalized or None


def _request_id(payload: Mapping[str, Any], secret: str) -> str | None:
    value = payload.get("id")
    return sanitize(value, (secret,)) if value else None


def _outcome(error: Exception) -> str:
    if isinstance(error, PermissionError):
        return "refused"
    if isinstance(error, _Transient) and error.timed_out:
        return "timed_out"
    if isinstance(error, ValueError):
        return "invalid"
    return "failed"


def _failed_preflight(
    invocation_id: str,
    name: str,
    config: Mapping[str, str],
    endpoint: str,
    prompt_hash: str,
    rubric_hash: str,
    failure: str,
) -> JudgeInvocation:
    now = _now()
    return JudgeInvocation(
        id=invocation_id,
        kind="llm",
        criterion_name=name,
        status="failed",
        attempts=(
            JudgeAttempt(
                index=1,
                mode="initial",
                started_at=now,
                finished_at=now,
                outcome="failed",
                model=config["model"],
                reasoning_effort=config["reasoning_effort"],
                endpoint_identity=endpoint,
                prompt_hash=prompt_hash,
                rubric_hash=rubric_hash,
                error=failure,
            ),
        ),
        failure=failure,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
