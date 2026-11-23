from __future__ import annotations

import pytest

from orbit.core.errors import (
    MemoryExhaustedError,
    PhaseTimeoutError,
    ProviderStartError,
    StorageExhaustedError,
    VerificationAdapterError,
    VerificationConfigurationError,
    VerificationProviderError,
    VerificationVerdictError,
    VerifierOutputError,
)
from orbit.core.verdict import Status, Verdict
from orbit.run.episode import status_for

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ProviderStartError("provider"), Status.ENV_ERROR),
        (VerificationConfigurationError("missing key"), Status.ENV_ERROR),
        (VerificationProviderError("endpoint"), Status.ENV_ERROR),
        (VerificationAdapterError("missing CLI"), Status.ENV_ERROR),
        (VerificationVerdictError("bad schema"), Status.ENV_ERROR),
        (VerifierOutputError("missing record"), Status.TASK_ERROR),
        (PhaseTimeoutError("verify", 1), Status.TIMEOUT),
    ],
)
def test_verification_failures_remain_distinct_terminal_failures(
    error: Exception, status: Status
) -> None:
    assert status_for(error) is status


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (
            MemoryExhaustedError("the sandbox itself died after an OOM kill in it"),
            Status.MEMORY_EXHAUSTED,
        ),
        (
            StorageExhaustedError("Orbit stopped the sandbox: it wrote 1025 MiB over its image"),
            Status.STORAGE_EXHAUSTED,
        ),
    ],
)
def test_a_sandbox_that_exhausts_a_resource_is_the_agents_result_or_a_task_defect(
    error: Exception, status: Status
) -> None:
    """The agent's where its code ran, the Task's in setup or a verifier of its own,
    separate or snapshot; never an environment error, which a retry would answer with fresh
    memory or storage."""
    assert status_for(error) is status
    assert status_for(error, agents=False) is Status.TASK_ERROR


def test_valid_zero_is_still_a_completed_measurement() -> None:
    verdict = Verdict.completed({"correctness": 0.0})
    assert verdict.status is Status.COMPLETED
    assert verdict.rewards == {"correctness": 0.0}
