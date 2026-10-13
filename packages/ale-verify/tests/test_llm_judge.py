from __future__ import annotations

import base64
import io
import json
import urllib.error
from collections import OrderedDict
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ale_verify import ConfigurationError, JudgeError, ScoredChoice, _llm, probe_judge
from ale_verify._io import EvidenceError, inline_evidence, read_file_evidence, read_text_evidence


class Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return io.BytesIO(self.payload)

    def __exit__(self, *_args) -> None:  # type: ignore[no-untyped-def]
        return None


class Body(io.BytesIO):
    def __init__(self, raw: bytes, content_type: str) -> None:
        super().__init__(raw)
        self.headers = {"Content-Type": content_type}


class Stream(Response):
    """A server-sent event stream, as providers answer a streamed request."""

    def __init__(self, *events: object) -> None:
        self.payload = (
            b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)
            + b"data: [DONE]\n\n"
        )

    def __enter__(self):
        return Body(self.payload, "text/event-stream; charset=utf-8")


def http_error(code: int, body: str = "{}", headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    return urllib.error.HTTPError(
        "https://provider.test", code, "error", headers or {}, io.BytesIO(body.encode())
    )


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_llm, "_max_output", {})
    monkeypatch.delenv("ALE_VERIFY_DEADLINE_SECONDS", raising=False)


def rubric() -> OrderedDict[str, ScoredChoice]:
    return OrderedDict(
        no=ScoredChoice(0, "Incorrect."),
        partial=ScoredChoice(0.5, "Partially correct."),
        yes=ScoredChoice(1, "Correct."),
    )


def config(**overrides: str) -> dict[str, str]:
    return {
        "model": "gpt-5-mini",
        "reasoning_effort": "medium",
        "base_url": "https://api.openai.com",
        "api_key_env": "JUDGE_KEY",
        **overrides,
    }


def call(**overrides):  # type: ignore[no-untyped-def]
    selected_config = overrides.pop("config", config())
    return _llm.run(
        invocation_id="judge-1",
        name="correctness",
        prompt="Judge the answer.",
        rubric=rubric(),
        evidence=overrides.pop("evidence", ()),
        config=selected_config,
        **overrides,
    )


def test_protocol_resolution_is_closed() -> None:
    assert _llm.resolve_protocol(config()) == "openai-responses"
    assert (
        _llm.resolve_protocol(config(base_url="https://api.openai.com/v1/chat/completions"))
        == "openai-chat-completions"
    )
    assert (
        _llm.resolve_protocol(config(model="claude-sonnet-4", base_url="https://api.anthropic.com"))
        == "anthropic"
    )
    assert (
        _llm.resolve_protocol(
            config(
                model="qwen-compatible",
                base_url="https://dashscope.aliyuncs.com/apps/anthropic",
            )
        )
        == "anthropic"
    )
    with pytest.raises(ValueError, match="infer"):
        _llm.resolve_protocol(config(model="unknown", base_url="https://example.test"))


def test_openai_request_and_strict_choice_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_KEY", "provider-secret")
    observed = {}

    def open_(request, timeout):  # type: ignore[no-untyped-def]
        observed["url"] = request.full_url
        observed["headers"] = dict(request.headers)
        observed["payload"] = json.loads(request.data)
        observed["timeout"] = timeout
        return Response(
            {
                "id": "response-1",
                "output_text": '{"choice":"yes","reasoning":"It is correct."}',
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    choice, reasoning, invocation = call()
    assert (choice, reasoning) == ("yes", "It is correct.")
    assert observed["url"] == "https://api.openai.com/v1/responses"
    assert observed["payload"]["reasoning"] == {"effort": "medium"}
    assert observed["payload"]["stream"] is True
    assert "max_output_tokens" not in observed["payload"]
    assert observed["timeout"] == _llm.IDLE_TIMEOUT_SECONDS
    assert "provider-secret" in observed["headers"]["Authorization"]
    assert invocation.attempts[0].usage == {"input_tokens": 10, "output_tokens": 5}
    assert "provider-secret" not in json.dumps(asdict(invocation), default=str)


def test_anthropic_request_is_rendered_without_a_dialect_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_KEY", "provider-secret")
    observed = {}

    def open_(request, timeout):  # type: ignore[no-untyped-def]
        if request.get_method() == "GET":
            observed["models"] = request.full_url
            return Response({"id": "claude-sonnet-4", "max_tokens": 64000})
        observed["url"] = request.full_url
        observed["headers"] = dict(request.headers)
        observed["payload"] = json.loads(request.data)
        return Response(
            {
                "id": "message-1",
                "content": [
                    {
                        "type": "text",
                        "text": '{"choice":"partial","reasoning":"Incomplete."}',
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }
        )

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    choice, _, invocation = call(
        config=config(model="claude-sonnet-4", base_url="https://api.anthropic.com")
    )
    assert choice == "partial"
    assert observed["url"] == "https://api.anthropic.com/v1/messages"
    assert observed["headers"]["X-api-key"] == "provider-secret"
    assert observed["payload"]["output_config"] == {"effort": "medium"}
    assert observed["models"] == "https://api.anthropic.com/v1/models/claude-sonnet-4"
    assert observed["payload"]["max_tokens"] == 64000
    assert invocation.attempts[0].request_id == "message-1"


def test_chat_completions_request_and_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_KEY", "provider-secret")
    observed = {}

    def open_(request, timeout):  # type: ignore[no-untyped-def]
        observed["url"] = request.full_url
        observed["payload"] = json.loads(request.data)
        return Response(
            {
                "id": "chatcmpl-1",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"choice":"yes","reasoning":"Correct."}',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        )

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    choice, reasoning, invocation = call(
        config=config(base_url="https://api.openai.com/v1/chat/completions")
    )
    assert (choice, reasoning) == ("yes", "Correct.")
    assert observed["url"] == "https://api.openai.com/v1/chat/completions"
    assert observed["payload"]["messages"][0]["role"] == "user"
    assert observed["payload"]["reasoning_effort"] == "medium"
    assert observed["payload"]["response_format"] == {"type": "json_object"}
    assert "max_completion_tokens" not in observed["payload"]
    assert invocation.attempts[0].usage == {
        "prompt_tokens": 4,
        "completion_tokens": 2,
    }


def test_schema_repair_includes_the_concrete_error_and_exact_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_KEY", "secret")
    prompts = []
    payloads = iter(
        [
            {"id": "1", "output_text": '{"choice":"mostly","reasoning":"Maybe."}'},
            {"id": "2", "output_text": '{"choice":"yes","reasoning":"Fixed."}'},
        ]
    )

    def open_(request, timeout):  # type: ignore[no-untyped-def]
        prompts.append(json.loads(request.data)["input"])
        return Response(next(payloads))

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    monkeypatch.setattr(_llm.time, "sleep", lambda _seconds: None)
    _, _, invocation = call()
    assert len(invocation.attempts) == 2
    assert invocation.attempts[0].outcome == "invalid"
    assert invocation.attempts[1].mode == "schema_repair"
    assert "unknown choice 'mostly'" in prompts[1]
    assert '{"choice":"no|partial|yes","reasoning":"..."}' in prompts[1]


def test_three_schema_repairs_are_the_limit_for_invalid_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_KEY", "secret")
    monkeypatch.setattr(_llm.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        _llm.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response({"id": "bad", "output_text": "{}"}),
    )
    with pytest.raises(JudgeError) as caught:
        call()
    invocation = caught.value.invocation
    assert invocation.status == "failed"
    assert len(invocation.attempts) == 4
    assert all(attempt.outcome == "invalid" for attempt in invocation.attempts)


def test_attempt_mode_describes_the_prompt_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_KEY", "secret")
    monkeypatch.setattr(_llm.time, "sleep", lambda _seconds: None)
    responses = iter(
        [
            Response({"id": "bad", "output_text": "{}"}),
            urllib.error.URLError("offline"),
            Response({"id": "ok", "output_text": '{"choice":"yes","reasoning":"Fixed."}'}),
        ]
    )

    def open_(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    _, _, invocation = call()
    assert [attempt.mode for attempt in invocation.attempts] == [
        "initial",
        "schema_repair",
        "schema_repair",
    ]


def test_provider_error_and_missing_credential_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_KEY", "provider-secret")
    monkeypatch.setattr(_llm.time, "sleep", lambda _seconds: None)

    def fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("provider-secret endpoint failed")

    monkeypatch.setattr(_llm.urllib.request, "urlopen", fail)
    monkeypatch.setenv("ALE_VERIFY_DEADLINE_SECONDS", "0")
    with pytest.raises(JudgeError) as caught:
        call()
    assert "provider-secret" not in str(caught.value)
    assert "provider-secret" not in str(caught.value.invocation)

    monkeypatch.delenv("JUDGE_KEY")
    with pytest.raises(JudgeError, match="JUDGE_KEY"):
        call()


def test_successful_provider_fields_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_KEY", "provider-secret")
    monkeypatch.setattr(
        _llm.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(
            {
                "id": "provider-secret",
                "output_text": (
                    '{"choice":"yes","reasoning":"provider-secret must not be retained"}'
                ),
            }
        ),
    )
    _, reasoning, invocation = call()
    assert "provider-secret" not in reasoning
    assert "provider-secret" not in (invocation.attempts[0].request_id or "")


def test_evidence_requires_absolute_bounded_regular_utf8_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    regular = tmp_path / "answer.txt"
    regular.write_text("answer")
    assert read_text_evidence(regular)[0] == "answer"
    with pytest.raises(EvidenceError, match="absolute"):
        read_text_evidence("answer.txt")
    link = tmp_path / "link"
    link.symlink_to(regular)
    with pytest.raises(EvidenceError):
        read_text_evidence(link)
    large = tmp_path / "large"
    large.write_bytes(b"x" * 10)
    with pytest.raises(EvidenceError, match="exceeds"):
        read_text_evidence(large, max_bytes=5)


@pytest.mark.parametrize(
    ("protocol", "endpoint"),
    [
        ("openai-responses", "https://example.test/v1/responses"),
        ("openai-chat-completions", "https://example.test/v1/chat/completions"),
        ("anthropic", "https://example.test/v1/messages"),
    ],
)
def test_mixed_evidence_reaches_each_protocol_and_survives_repair(
    monkeypatch, tmp_path, protocol, endpoint
) -> None:  # type: ignore[no-untyped-def]
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1sAAAAASUVORK5CYII="
    )
    pdf = b"%PDF-1.4\n%binary \xff\n%%EOF"
    files = [tmp_path / "image.png", tmp_path / "document.pdf"]
    for path, data in zip(files, (png, pdf), strict=True):
        path.write_bytes(data)
    evidence = (
        inline_evidence("The reference fact is 47.", kind="reference"),
        inline_evidence("Observed action was inspect.", kind="solver_trajectory"),
        *(read_file_evidence(path) for path in files),
    )
    observed = []

    def open_(request, timeout):  # type: ignore[no-untyped-def]
        observed.append(json.loads(request.data))
        text = "{}" if len(observed) == 1 else '{"choice":"yes","reasoning":"Observed."}'
        if protocol == "anthropic":
            return Response({"content": [{"type": "text", "text": text}]})
        if protocol == "openai-chat-completions":
            return Response({"choices": [{"message": {"content": text}}]})
        return Response({"output_text": text})

    monkeypatch.setenv("JUDGE_KEY", "provider-secret")
    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    monkeypatch.setattr(_llm.time, "sleep", lambda _: None)
    choice, _, invocation = call(config=config(base_url=endpoint), evidence=evidence)
    assert choice == "yes"
    assert [attempt.mode for attempt in invocation.attempts] == ["initial", "schema_repair"]
    for payload in observed:
        parts = payload.get("input", payload.get("messages"))[0]["content"]
        prompt = parts[0]["text"]
        assert "The reference fact is 47." in prompt
        assert "Observed action was inspect." in prompt
        assert "provider-secret" not in prompt
        if protocol == "anthropic":
            image, document = parts[2], parts[4]
            assert (image["type"], document["type"]) == ("image", "document")
            assert image["source"]["media_type"] == "image/png"
            assert document["source"]["media_type"] == "application/pdf"
            assert base64.b64decode(image["source"]["data"]) == png
            assert base64.b64decode(document["source"]["data"]) == pdf
        else:
            image, document = parts[2], parts[4]
            if protocol == "openai-responses":
                assert (image["type"], document["type"]) == ("input_image", "input_file")
                image_url = image["image_url"]
                file = document
            else:
                assert (image["type"], document["type"]) == ("image_url", "file")
                image_url = image["image_url"]["url"]
                file = document["file"]
            assert image_url == "data:image/png;base64," + base64.b64encode(png).decode()
            assert file["filename"] == "document.pdf"
            assert (
                file["file_data"] == "data:application/pdf;base64," + base64.b64encode(pdf).decode()
            )
    assert observed[0] != observed[1]
    assert "Your previous verdict was rejected" in parts[0]["text"]
    assert base64.b64encode(png).decode() not in json.dumps(asdict(invocation))


@pytest.mark.parametrize(
    ("raw", "mime"),
    [
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF89a", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
        (b"%PDF-1.7\n", "application/pdf"),
    ],
)
def test_public_judge_sends_media_bytes_and_records_original_evidence(
    monkeypatch, tmp_path, raw, mime
) -> None:  # type: ignore[no-untyped-def]
    from ale_verify import Verification, VerificationRecord
    from ale_verify._io import digest

    path = tmp_path / "artifact"
    path.write_bytes(raw)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"llm": config()}))
    record_path = tmp_path / "verification.json"
    monkeypatch.setenv("JUDGE_KEY", "secret")
    monkeypatch.setenv("ALE_VERIFY_CONFIG_PATH", str(settings))
    monkeypatch.setenv("ALE_VERIFICATION_PATH", str(record_path))
    monkeypatch.setenv("ALE_VERDICT_PATH", str(tmp_path / "rewards.json"))

    def open_(request, timeout):  # type: ignore[no-untyped-def]
        parts = json.loads(request.data)["input"][0]["content"]
        assert "reference value" in parts[0]["text"]
        attachment = parts[2]
        url = attachment.get("file_data", attachment.get("image_url"))
        assert url == f"data:{mime};base64," + base64.b64encode(raw).decode()
        return Response({"output_text": '{"choice":"yes","reasoning":"Checked evidence."}'})

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    verification = Verification()
    assert (
        verification.judge(
            "llm",
            "correctness",
            prompt="Inspect the attached artifact.",
            rubric=rubric(),
            files=[str(path)],
            reference="reference value",
        )
        == 1
    )
    verification.aggregate("overall")
    verification.write()
    record = VerificationRecord.from_json(record_path.read_bytes())
    assert record.criteria[0].evidence[0].sha256 == digest(raw)
    assert record.criteria[0].evidence[0].size_bytes == len(raw)
    assert record.criteria[0].evidence[1].kind == "reference"
    assert base64.b64encode(raw).decode() not in record_path.read_text()


def test_provider_error_cannot_echo_media_into_failure_records(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "image.png"
    raw = b"\x89PNG\r\n\x1a\n" + b"private media contents" * 50
    path.write_bytes(raw)
    encoded = base64.b64encode(raw).decode()

    def fail(request, timeout):  # type: ignore[no-untyped-def]
        body = json.dumps({"error": {"message": f"Invalid image: data:image/png;base64,{encoded}"}})
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {}, io.BytesIO(body.encode())
        )

    monkeypatch.setenv("JUDGE_KEY", "secret")
    monkeypatch.setattr(_llm.urllib.request, "urlopen", fail)
    monkeypatch.setattr(_llm.time, "sleep", lambda _: None)
    with pytest.raises(JudgeError) as caught:
        call(evidence=(read_file_evidence(path),))
    assert "HTTP 400" in str(caught.value)
    assert encoded[:40] not in str(caught.value)
    assert encoded[:40] not in json.dumps(asdict(caught.value.invocation))


@pytest.mark.parametrize(
    ("endpoint", "events"),
    [
        (
            "https://api.anthropic.com",
            [
                {"type": "message_start", "message": {"id": "msg-1", "usage": {"input_tokens": 9}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta"}},
                {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": '{"choice":"yes",'},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": '"reasoning":"Streamed."}'},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 4},
                },
                {"type": "message_stop"},
            ],
        ),
        (
            "https://api.openai.com/v1/chat/completions",
            [
                {"id": "chat-1", "choices": [{"delta": {"content": '{"choice":"yes",'}}]},
                {
                    "id": "chat-1",
                    "choices": [
                        {"delta": {"content": '"reasoning":"Streamed."}'}, "finish_reason": "stop"}
                    ],
                },
                {
                    "id": "chat-1",
                    "choices": [],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 4},
                },
            ],
        ),
        (
            "https://api.openai.com",
            [
                {"type": "response.output_text.delta", "delta": "ignored"},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-1",
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": '{"choice":"yes","reasoning":"Streamed."}',
                                    }
                                ],
                            }
                        ],
                        "usage": {"input_tokens": 9, "output_tokens": 4},
                    },
                },
            ],
        ),
    ],
    ids=["anthropic", "chat", "responses"],
)
def test_streamed_responses_are_rebuilt_for_each_protocol(monkeypatch, endpoint, events) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUDGE_KEY", "secret")

    def open_(request, timeout):  # type: ignore[no-untyped-def]
        if request.get_method() == "GET":
            raise urllib.error.URLError("no models API")
        return Stream(*events)

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    model = "claude-opus-4" if "anthropic" in endpoint else "gpt-5"
    choice, reasoning, invocation = call(config=config(base_url=endpoint, model=model))
    assert (choice, reasoning) == ("yes", "Streamed.")
    usage = invocation.attempts[0].usage or {}
    assert 4 in usage.values()


def test_a_stream_cut_short_is_retried_as_a_transient_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUDGE_KEY", "secret")
    monkeypatch.setattr(_llm.time, "sleep", lambda _: None)
    responses = iter(
        [
            Stream({"type": "response.output_text.delta", "delta": "half"}),
            Response({"id": "ok", "output_text": '{"choice":"yes","reasoning":"Done."}'}),
        ]
    )
    monkeypatch.setattr(_llm.urllib.request, "urlopen", lambda *_a, **_k: next(responses))
    choice, _, invocation = call()
    assert choice == "yes"
    assert [attempt.outcome for attempt in invocation.attempts] == ["failed", "completed"]
    assert invocation.attempts[1].mode == "retry"


def test_provider_overload_is_retried_without_a_count_and_honours_retry_after(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUDGE_KEY", "secret")
    slept: list[float] = []
    monkeypatch.setattr(_llm.time, "sleep", slept.append)
    failures = [http_error(529), http_error(429, headers={"retry-after": "7"})] * 4
    responses = iter(
        [*failures, Response({"id": "ok", "output_text": '{"choice":"no","reasoning":"Wrong."}'})]
    )

    def open_(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    choice, _, invocation = call()
    assert choice == "no"
    assert len(invocation.attempts) == 9
    assert slept[1::2] == [7.0] * 4
    assert all(0 < delay <= 60 for delay in slept)


def test_a_rejected_request_fails_at_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUDGE_KEY", "secret")
    calls = []

    def open_(request, timeout):  # type: ignore[no-untyped-def]
        calls.append(request)
        raise http_error(401, '{"error":"invalid key"}')

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    with pytest.raises(JudgeError, match="HTTP 401") as caught:
        call()
    assert len(calls) == 1
    assert caught.value.invocation.attempts[0].outcome == "failed"


def test_retries_stop_when_verification_budget_is_spent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUDGE_KEY", "secret")
    monkeypatch.setenv("ALE_VERIFY_DEADLINE_SECONDS", "40")
    clock = [0.0]
    monkeypatch.setattr(
        _llm,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0], sleep=lambda seconds: clock.append(clock.pop() + seconds)
        ),
    )
    monkeypatch.setattr(_llm, "_STARTED", 0.0)
    monkeypatch.setattr(_llm, "_backoff", lambda failures, _after: 4.0)

    def open_(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    with pytest.raises(JudgeError) as caught:
        call()
    # Forty seconds less the thirty-second margin covers two four-second waits.
    attempts = caught.value.invocation.attempts
    assert len(attempts) == 3
    assert all(attempt.outcome == "timed_out" for attempt in attempts)


def test_anthropic_max_tokens_follows_the_provider_limit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUDGE_KEY", "secret")
    sent: list[int] = []

    def open_(request, timeout):  # type: ignore[no-untyped-def]
        if request.get_method() == "GET":
            raise urllib.error.URLError("no models API")
        sent.append(json.loads(request.data)["max_tokens"])
        if len(sent) == 1:
            raise http_error(
                400,
                '{"error":{"message":"max_tokens: 128000 > 32000, which is the maximum '
                'allowed number of output tokens for claude-x"}}',
            )
        return Response(
            {"content": [{"type": "text", "text": '{"choice":"yes","reasoning":"Ok."}'}]}
        )

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    anthropic = config(model="claude-x", base_url="https://api.anthropic.com")
    assert call(config=anthropic)[0] == "yes"
    assert call(config=anthropic)[0] == "yes"
    assert sent == [128000, 32000, 32000]


def test_a_verdict_in_a_json_fence_is_accepted() -> None:
    choice, reasoning = _llm._verdict('```json\n{"choice":"yes","reasoning":"Ok."}\n```', rubric())
    assert (choice, reasoning) == ("yes", "Ok.")


def test_probe_sends_a_tiny_request_and_reports_a_rejection(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUDGE_KEY", "provider-secret")
    sent = []

    def open_(request, timeout):  # type: ignore[no-untyped-def]
        sent.append((request.full_url, json.loads(request.data)))
        if len(sent) > 2:
            raise http_error(404, '{"error":"model not found provider-secret"}')
        return Response({"id": "probe"})

    monkeypatch.setattr(_llm.urllib.request, "urlopen", open_)
    probe_judge("llm", config())
    assert sent[0][1]["max_output_tokens"] == 64
    assert sent[0][1]["reasoning"] == {"effort": "medium"}
    probe_judge(
        "agent",
        config(
            adapter="claude-code",
            version="2.0.0",
            model="claude-opus-4",
            base_url="https://api.anthropic.com",
            reasoning_effort="max",
        ),
    )
    assert sent[1][0] == "https://api.anthropic.com/v1/messages"
    assert "output_config" not in sent[1][1]
    with pytest.raises(ConfigurationError, match="HTTP 404") as caught:
        probe_judge("llm", config())
    assert "provider-secret" not in str(caught.value)

    monkeypatch.delenv("JUDGE_KEY")
    with pytest.raises(ConfigurationError, match="JUDGE_KEY"):
        probe_judge("llm", config())
    with pytest.raises(ConfigurationError, match="reasoning_effort"):
        probe_judge("llm", config(reasoning_effort="extreme"))
