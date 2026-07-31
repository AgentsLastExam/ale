from __future__ import annotations

import pytest

from ale.core.errors import (
    PhaseTimeoutError,
    ProviderStartError,
    VerificationAdapterError,
    VerificationConfigurationError,
    VerificationProviderError,
    VerificationVerdictError,
    VerifierOutputError,
)
from ale.core.verdict import Status, Verdict
from ale.run.episode import status_for

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


def test_valid_zero_is_still_a_completed_measurement() -> None:
    verdict = Verdict.completed({"correctness": 0.0})
    assert verdict.status is Status.COMPLETED
    assert verdict.rewards == {"correctness": 0.0}
