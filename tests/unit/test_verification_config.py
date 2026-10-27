from __future__ import annotations

import pytest
from pydantic import ValidationError

from ale.core.config import RunConfig

pytestmark = pytest.mark.unit


def test_verification_sections_are_optional_and_strict() -> None:
    assert RunConfig().verification.llm is None
    assert RunConfig().verification.agent is None
    configured = RunConfig.model_validate(
        {
            "verification": {
                "llm": {
                    "provider": "anthropic",
                    "dialect": "anthropic",
                    "model": "judge-model",
                    "reasoning_effort": "medium",
                },
                "agent": {
                    "provider": "openai",
                    "adapter": "codex-cli",
                    "version": "1.2.3",
                    "model": "agent-model",
                    "reasoning_effort": "high",
                },
            }
        }
    )
    assert configured.verification.agent is not None
    assert configured.verification.agent.adapter == "codex-cli"


_LLM = {
    "provider": "openai",
    "dialect": "openai-responses",
    "model": "m",
    "reasoning_effort": "low",
}


@pytest.mark.parametrize(
    "llm",
    [
        {**_LLM, "model": ""},
        {**_LLM, "reasoning_effort": " "},
        {**_LLM, "provider": "meta"},
        {**_LLM, "dialect": "gemini"},
        {key: value for key, value in _LLM.items() if key != "provider"},
        {key: value for key, value in _LLM.items() if key != "dialect"},
    ],
)
def test_invalid_judge_configuration_is_rejected(llm: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"verification": {"llm": llm}})


def test_judges_name_a_provider_never_a_key_or_endpoint() -> None:
    config = RunConfig.model_validate({"verification": {"llm": _LLM}})
    dumped = config.model_dump(mode="json")
    assert dumped["verification"]["llm"] == _LLM


@pytest.mark.parametrize("adapter", ["codex", "claude", "openclaw-cli", ""])
def test_agent_adapter_is_closed(adapter: str) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {
                "verification": {
                    "agent": {
                        "provider": "openai",
                        "adapter": adapter,
                        "version": "1.2.3",
                        "model": "m",
                        "reasoning_effort": "medium",
                    }
                }
            }
        )


@pytest.mark.parametrize(
    "field", ["base_url", "api_key_env", "profile", "gateway", "harness", "retry_count"]
)
def test_legacy_verification_fields_are_forbidden(field: str) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"verification": {"llm": {**_LLM, field: "legacy"}}})


@pytest.mark.parametrize("version", [None, "latest", "1", "1.2", "^1.2.3", "1.2.x"])
def test_agent_judge_requires_exact_numeric_semver(version: str | None) -> None:
    agent = {
        "provider": "openai",
        "adapter": "codex-cli",
        "model": "m",
        "reasoning_effort": "medium",
    }
    if version is not None:
        agent["version"] = version
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"verification": {"agent": agent}})


def test_sandbox_retention_is_run_owned_and_defaults_to_destroy() -> None:
    assert RunConfig().sandbox_retention.model_dump() == {
        "solver": "destroy",
        "verifier": "destroy",
    }
    configured = RunConfig.model_validate(
        {"sandbox_retention": {"solver": "keep", "verifier": "destroy"}}
    )
    assert configured.sandbox_retention.solver == "keep"
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"sandbox_retention": {"solver": "sometimes"}})
