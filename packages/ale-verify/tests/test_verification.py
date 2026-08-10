from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale_verify import (
    CheckResult,
    JudgeAttempt,
    JudgeInvocation,
    Verification,
    VerificationRecord,
)

HASH0 = "sha256:" + "0" * 64
HASH1 = "sha256:" + "1" * 64


@pytest.fixture
def verify_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    record = tmp_path / "verification.json"
    verdict = tmp_path / "rewards.json"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "llm": {
                    "model": "judge-model",
                    "reasoning_effort": "medium",
                    "base_url": "https://example.test",
                    "api_key_env": "JUDGE_KEY",
                }
            }
        )
    )
    monkeypatch.setenv("ALE_VERIFICATION_PATH", str(record))
    monkeypatch.setenv("ALE_VERDICT_PATH", str(verdict))
    monkeypatch.setenv("ALE_VERIFY_CONFIG_PATH", str(config))
    monkeypatch.setenv("JUDGE_KEY", "secret")
    return record, verdict


def test_one_state_holds_checks_stats_judge_aggregate_and_final_write(
    monkeypatch: pytest.MonkeyPatch, verify_env: tuple[Path, Path]
) -> None:
    record_path, verdict_path = verify_env

    def fake_run(**kwargs):  # type: ignore[no-untyped-def]
        return (
            "yes",
            "Observed correct output.",
            JudgeInvocation(
                id=kwargs["invocation_id"],
                kind="llm",
                criterion_name=kwargs["name"],
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
                        endpoint_identity="https://example.test",
                        prompt_hash=HASH0,
                        rubric_hash=HASH1,
                    ),
                ),
            ),
        )

    monkeypatch.setattr("ale_verify._llm.run", fake_run)
    verification = Verification()
    assert verification.check("format", CheckResult(1, "valid")) == 1
    assert verification.check("content", CheckResult(0.5), weight=2) == 0.5
    assert verification.stat("files", 2) == 2
    assert (
        verification.judge(
            "llm",
            "correctness",
            prompt="Judge correctness.",
            rubric={
                "no": {"score": 0.0, "description": "Wrong."},
                "yes": {"score": 1.0, "description": "Correct."},
            },
        )
        == 1
    )
    assert verification.aggregate("overall") == pytest.approx(0.75)
    verification.write()

    record = VerificationRecord.from_json(record_path.read_bytes())
    envelope = json.loads(verdict_path.read_text())
    assert record.status == "completed"
    assert record.aggregates[0].method == "weighted_mean"
    assert envelope == {"rewards": record.rewards, "metrics": record.metrics}
    assert not list(record_path.parent.glob("*.tmp"))


def test_verification_has_no_stage_asset_resolver() -> None:
    assert not hasattr(Verification, "asset_path")


def test_duplicate_invalid_and_terminal_mutations_fail(verify_env: tuple[Path, Path]) -> None:
    verification = Verification()
    verification.check("format", CheckResult(1))
    with pytest.raises(ValueError, match="duplicate"):
        verification.check("format", CheckResult(1))
    with pytest.raises(ValueError, match="between 0 and 1"):
        verification.check("bad", CheckResult(2))
    with pytest.raises(ValueError, match="positive"):
        verification.aggregate("bad-weight", weights={"format": 0})
    verification.write()
    with pytest.raises(RuntimeError, match="terminal"):
        verification.stat("late", 1)


def test_agent_judge_is_the_last_mutating_operation(
    monkeypatch: pytest.MonkeyPatch, verify_env: tuple[Path, Path]
) -> None:
    config = Path(__import__("os").environ["ALE_VERIFY_CONFIG_PATH"])
    config.write_text(
        json.dumps(
            {
                "agent": {
                    "adapter": "codex-cli",
                    "version": "1.2.3",
                    "model": "judge-model",
                    "reasoning_effort": "high",
                    "base_url": "https://example.test",
                    "api_key_env": "JUDGE_KEY",
                }
            }
        )
    )

    def fake_run(**kwargs):  # type: ignore[no-untyped-def]
        return (
            "yes",
            "Works.",
            JudgeInvocation(
                id=kwargs["invocation_id"],
                kind="agent",
                criterion_name=kwargs["name"],
                adapter="codex-cli",
                adapter_version="1",
                status="completed",
                attempts=(
                    JudgeAttempt(
                        index=1,
                        mode="initial",
                        started_at="2026-07-31T00:00:00Z",
                        finished_at="2026-07-31T00:00:01Z",
                        outcome="completed",
                        model="judge-model",
                        reasoning_effort="high",
                        endpoint_identity="https://example.test",
                        prompt_hash=HASH0,
                        rubric_hash=HASH1,
                    ),
                ),
            ),
        )

    monkeypatch.setattr("ale_verify._agents.run", fake_run)
    verification = Verification()
    verification.judge(
        "agent",
        "functional",
        prompt="Test it.",
        rubric={
            "no": {"score": 0, "description": "Fails."},
            "yes": {"score": 1, "description": "Works."},
        },
    )
    with pytest.raises(RuntimeError, match="after an Agent Judge"):
        verification.check("late", CheckResult(1))
    verification.aggregate("overall")
    verification.write()


def test_judge_failure_persists_failed_state_and_cannot_be_caught_into_a_score(
    monkeypatch: pytest.MonkeyPatch, verify_env: tuple[Path, Path]
) -> None:
    def fail(**kwargs):  # type: ignore[no-untyped-def]
        attempt = JudgeAttempt(
            index=1,
            mode="initial",
            started_at="2026-07-31T00:00:00Z",
            finished_at="2026-07-31T00:00:01Z",
            outcome="failed",
            model="judge-model",
            reasoning_effort="medium",
            endpoint_identity="https://example.test",
            prompt_hash=HASH0,
            rubric_hash=HASH1,
            error="endpoint failed",
        )
        invocation = JudgeInvocation(
            id=kwargs["invocation_id"],
            kind="llm",
            criterion_name=kwargs["name"],
            status="failed",
            attempts=(attempt,),
            failure="endpoint failed",
        )
        raise RuntimeError("endpoint failed", invocation)

    monkeypatch.setattr("ale_verify._llm.run", fail)
    verification = Verification()
    with pytest.raises(RuntimeError, match="endpoint failed"):
        verification.judge(
            "llm",
            "correctness",
            prompt="Judge.",
            rubric={
                "no": {"score": 0, "description": "Wrong."},
                "yes": {"score": 1, "description": "Right."},
            },
        )
    record = VerificationRecord.from_json(verify_env[0].read_bytes())
    assert record.status == "failed"
    assert record.rewards == {}
    with pytest.raises(RuntimeError, match="terminal"):
        verification.check("fallback", CheckResult(1))
