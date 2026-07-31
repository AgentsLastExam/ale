from __future__ import annotations

import io
import json
import urllib.error
from collections import OrderedDict
from dataclasses import asdict

import pytest

from ale_verify import JudgeError, ScoredChoice, _llm
from ale_verify._io import EvidenceError, read_text_evidence


class Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return io.BytesIO(self.payload)

    def __exit__(self, *_args) -> None:  # type: ignore[no-untyped-def]
        return None


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
        evidence=(),
        reference=None,
        trajectory=None,
        config=selected_config,
        **overrides,
    )


def test_protocol_resolution_is_closed() -> None:
    assert _llm.resolve_protocol(config()) == "openai-responses"
    assert (
        _llm.resolve_protocol(config(model="claude-sonnet-4", base_url="https://api.anthropic.com"))
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
    assert "provider-secret" in observed["headers"]["Authorization"]
    assert invocation.attempts[0].usage == {"input_tokens": 10, "output_tokens": 5}
    assert "provider-secret" not in json.dumps(asdict(invocation), default=str)


def test_anthropic_request_is_rendered_without_a_dialect_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_KEY", "provider-secret")
    observed = {}

    def open_(request, timeout):  # type: ignore[no-untyped-def]
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
    assert invocation.attempts[0].request_id == "message-1"


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


def test_one_initial_plus_three_retries_fail_without_a_score(
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
