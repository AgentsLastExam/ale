"""The task-admission rule has one exact reward policy."""

from __future__ import annotations

import json

import pytest

from ale.core.errors import VerifierOutputError
from ale.run.cli.main import _is_full_reward_map
from ale.run.environments.standard import StandardEnvironment
from ale.run.harnesses.builtin import NopHarness

pytestmark = pytest.mark.unit


class RewardsSandbox:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    async def read_file(self, _path) -> bytes:  # type: ignore[no-untyped-def]
        if isinstance(self.payload, BaseException):
            raise self.payload
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode()


@pytest.mark.parametrize(
    ("rewards", "expected"),
    [
        ({"correctness": 1.0}, True),
        ({"correctness": 1.0, "format": 1.0}, True),
        ({"correctness": 0.0}, False),
        ({"correctness": 0.5}, False),
        ({}, False),
        (None, False),
    ],
)
def test_admission_requires_nonempty_all_ones(
    rewards: dict[str, float] | None, expected: bool
) -> None:
    assert _is_full_reward_map(rewards) is expected


@pytest.mark.asyncio
async def test_verifier_accepts_finite_named_rewards() -> None:
    rewards = await StandardEnvironment(NopHarness())._read_rewards(
        RewardsSandbox({"rewards": {"correctness": 1, "format": 0.5}})  # type: ignore[arg-type]
    )
    assert rewards == {"correctness": 1.0, "format": 0.5}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"rewards": {}},
        {"rewards": {"score": "not-a-number"}},
        {"rewards": {"score": float("nan")}},
        {"rewards": {"score": float("inf")}},
        {"rewards": {"": 1.0}},
        {"not_rewards": {}},
        b"not-json",
        FileNotFoundError("missing"),
    ],
)
async def test_verifier_rejects_missing_malformed_or_nonfinite_rewards(
    payload: object,
) -> None:
    with pytest.raises(VerifierOutputError):
        await StandardEnvironment(NopHarness())._read_rewards(
            RewardsSandbox(payload)  # type: ignore[arg-type]
        )
