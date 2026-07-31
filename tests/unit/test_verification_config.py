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
                    "model": "judge-model",
                    "reasoning_effort": "medium",
                    "base_url": "https://example.test",
                    "api_key_env": "JUDGE_API_KEY",
                },
                "agent": {
                    "adapter": "codex-cli",
                    "model": "agent-model",
                    "reasoning_effort": "high",
                    "base_url": "https://example.test",
                    "api_key_env": "AGENT_JUDGE_API_KEY",
                },
            }
        }
    )
    assert configured.verification.agent is not None
    assert configured.verification.agent.adapter == "codex-cli"


@pytest.mark.parametrize(
    "llm",
    [
        {"model": "", "reasoning_effort": "medium", "base_url": "https://x", "api_key_env": "K"},
        {"model": "m", "reasoning_effort": "", "base_url": "https://x", "api_key_env": "K"},
        {"model": "m", "reasoning_effort": "medium", "base_url": "not-a-url", "api_key_env": "K"},
        {
            "model": "m",
            "reasoning_effort": "medium",
            "base_url": "https://x",
            "api_key_env": "not valid",
        },
    ],
)
def test_invalid_judge_configuration_is_rejected(llm: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"verification": {"llm": llm}})


def test_config_hash_contains_only_the_credential_variable_name() -> None:
    config = RunConfig.model_validate(
        {
            "verification": {
                "llm": {
                    "model": "m",
                    "reasoning_effort": "low",
                    "base_url": "https://example.test",
                    "api_key_env": "SECRET_NAME",
                }
            }
        }
    )
    dumped = config.model_dump(mode="json")
    assert dumped["verification"]["llm"]["api_key_env"] == "SECRET_NAME"
    assert "credential-value" not in str(dumped)


@pytest.mark.parametrize("adapter", ["codex", "claude", "openclaw-cli", ""])
def test_agent_adapter_is_closed(adapter: str) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {
                "verification": {
                    "agent": {
                        "adapter": adapter,
                        "model": "m",
                        "reasoning_effort": "medium",
                        "base_url": "https://example.test",
                        "api_key_env": "KEY",
                    }
                }
            }
        )


@pytest.mark.parametrize("field", ["profile", "dialect", "gateway", "harness", "retry_count"])
def test_legacy_verification_fields_are_forbidden(field: str) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {
                "verification": {
                    "llm": {
                        "model": "m",
                        "reasoning_effort": "medium",
                        "base_url": "https://example.test",
                        "api_key_env": "KEY",
                        field: "legacy",
                    }
                }
            }
        )
