from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_runtime_contracts import make_lock

from ale.core.errors import VerifierOutputError
from ale.core.lock import AleVerifyProvenance, JudgeProvenance
from ale.core.sandbox import SandboxFile
from ale.core.taskspec import OperatingSystem
from ale.run.harnesses.builtin import NopHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.provenance import judge_provenance
from ale_verify import (
    JudgeAttempt,
    JudgeInvocation,
)

pytestmark = pytest.mark.unit

HASH0 = "sha256:" + "0" * 64
HASH1 = "sha256:" + "1" * 64


def invocation() -> JudgeInvocation:
    return JudgeInvocation(
        id="judge-1",
        kind="llm",
        name="correctness",
        status="completed",
        attempts=(
            JudgeAttempt(
                index=1,
                mode="initial",
                started_at="2026-07-31T00:00:00Z",
                finished_at="2026-07-31T00:00:01Z",
                outcome="completed",
                model="judge-model",
                reasoning_effort="medium",
                endpoint_identity="https://example.test/v1",
                prompt_hash=HASH0,
                schema_hash=HASH1,
            ),
        ),
    )


def test_run_lock_separates_verify_runtime_and_direct_judges() -> None:
    judge = JudgeProvenance(
        invocation_id="judge-1",
        kind="llm",
        model="judge-model",
        reasoning_effort="medium",
        endpoint_identity="https://example.test/v1",
        prompt_hash=HASH0,
        schema_hash=HASH1,
        attempts=1,
    )
    lock = make_lock(
        ale_verify=AleVerifyProvenance(version="0.1.0", content_hash=HASH0),
        judges=(judge,),
    )
    dumped = lock.model_dump(mode="json")
    assert dumped["ale_verify"]["version"] == "0.1.0"
    assert "kits" not in dumped
    assert "dialect" not in dumped["judges"][0]
    assert "transport_call_ids" not in dumped["judges"][0]


def test_judge_provenance_is_derived_from_the_collected_record() -> None:
    record = (invocation(),)
    observed = judge_provenance(record)
    assert observed[0].invocation_id == "judge-1"
    assert observed[0].endpoint_identity == "https://example.test/v1"
    # The verifier saw the Gateway; the RunLock names the Judge's configured endpoint.
    real = judge_provenance(record, {"llm": "https://api.provider.test/v1"})
    assert real[0].endpoint_identity == "https://api.provider.test/v1"
    assert observed[0].attempts == 1
    assert "credential" not in json.dumps(observed[0].model_dump(mode="json"))


def test_a_lock_in_another_shape_is_rejected_not_reinterpreted() -> None:
    payload = make_lock().model_dump(mode="json")
    payload.pop("judges", None)
    payload["judge"] = {
        "model": "legacy-model",
        "prompt_hash": HASH0,
    }
    with pytest.raises(ValidationError):
        type(make_lock()).model_validate(payload)


class RewardsSandbox:
    """A verifier directory holding ``reward.json`` and, optionally, ``verification.json``."""

    def __init__(self, reward: object, verification: object = None) -> None:
        self.files = {
            name: value if isinstance(value, bytes) else json.dumps(value).encode()
            for name, value in (("reward.json", reward), ("verification.json", verification))
            if value is not None
        }

    async def list_files(self, path: str) -> tuple[SandboxFile, ...]:
        return tuple(
            SandboxFile(path=f"{path}/{name}", size=len(data)) for name, data in self.files.items()
        )

    async def read_file(self, path: str) -> bytes:
        return self.files[path.rsplit("/", 1)[1]]


def _context(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(os=OperatingSystem.LINUX),
        verification_secrets=(),
        secrets=(),
        run_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_verifier_accepts_nested_details(tmp_path: Path) -> None:
    value = {"reward": 1, "details": {"count": 10, "reason": "good"}}
    sandbox = RewardsSandbox({"reward": 1}, value)
    observed = await EpisodeProtocol(NopHarness())._read_result(_context(tmp_path), sandbox)
    assert observed == value
    assert json.loads((tmp_path / "verification.json").read_text()) == value


@pytest.mark.asyncio
async def test_a_reward_file_alone_is_the_complete_result(tmp_path: Path) -> None:
    sandbox = RewardsSandbox({"accuracy": 0.25, "reward": 0.5})
    observed = await EpisodeProtocol(NopHarness())._read_result(_context(tmp_path), sandbox)
    assert observed == {"accuracy": 0.25, "reward": 0.5}
    assert json.loads((tmp_path / "verification.json").read_text()) == observed


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw_score", "expected"), [(0.999, 1.0), (0.994, 0.99), (0.004, 0.0)])
async def test_the_saved_result_holds_the_rounded_reward(
    tmp_path: Path, raw_score: float, expected: float
) -> None:
    value = {"reward": raw_score, "details": {"raw": raw_score}}
    sandbox = RewardsSandbox({"reward": raw_score}, value)
    observed = await EpisodeProtocol(NopHarness())._read_result(_context(tmp_path), sandbox)
    assert observed == {"reward": expected, "details": {"raw": raw_score}}
    assert json.loads((tmp_path / "verification.json").read_text()) == observed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reward", "verification"),
    [
        ([], None),
        ({}, None),
        ({"overall": 1}, None),
        ({"reward": "1"}, None),
        ({"reward": True}, None),
        ({"reward": float("nan")}, None),
        ({"reward": 1.0001}, None),
        ({"reward": 1, "details": {"count": 1}}, None),
        (b"not-json", None),
        (None, None),
        (None, {"reward": 1}),
        ({"reward": 1}, {"reward": 0}),
        ({"reward": 1}, {"details": "no reward"}),
        ({"reward": 1}, b"not-json"),
    ],
)
async def test_verifier_rejects_bad_result(
    tmp_path: Path, reward: object, verification: object
) -> None:
    with pytest.raises(VerifierOutputError):
        await EpisodeProtocol(NopHarness())._read_result(
            _context(tmp_path), RewardsSandbox(reward, verification)
        )
    assert not (tmp_path / "verification.json").exists()
