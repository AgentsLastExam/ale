"""The task-admission rule has one exact reward policy."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ale.core.errors import VerificationInfrastructureError, VerifierOutputError
from ale.core.sandbox import PreparedTaskImage
from ale.core.taskspec import ImageKind
from ale.core.verdict import Status, Verdict
from ale.run.cli.main import app
from ale.run.cli.tasks import (
    _is_full_reward_map,
    _is_zero_reward_map,
    _validation_notices,
    _validation_outcome,
)
from ale.run.environments.standard import StandardEnvironment
from ale.run.harnesses.builtin import NopHarness
from ale.run.task_images import ImagePreparationResult, ImagePreparationStep

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
def test_full_oracle_detection(rewards: dict[str, float] | None, expected: bool) -> None:
    assert _is_full_reward_map(rewards) is expected


@pytest.mark.parametrize(
    ("rewards", "expected"),
    [
        ({"correctness": 0.0}, True),
        ({"correctness": 0.0, "format": 0.0}, True),
        ({"correctness": 1.0}, False),
        ({"correctness": 0.5}, False),
        ({}, False),
        (None, False),
    ],
)
def test_untouched_admission_requires_nonempty_all_zeroes(
    rewards: dict[str, float] | None, expected: bool
) -> None:
    assert _is_zero_reward_map(rewards) is expected


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


def test_validate_accepts_run_level_judge_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli = import_module("ale.run.cli.main")
    config = tmp_path / "verification.toml"
    config.write_text(
        "[verification.llm]\n"
        'model = "gpt-test"\n'
        'reasoning_effort = "medium"\n'
        'base_url = "https://api.openai.com"\n'
        'api_key_env = "TEST_JUDGE_KEY"\n'
    )
    observed = {}

    async def fake_validate(reference, settings, runs_dir):  # type: ignore[no-untyped-def]
        observed.update(reference=reference, settings=settings, runs_dir=runs_dir)
        return 0

    monkeypatch.setattr(cli, "_validate", fake_validate)
    result = CliRunner().invoke(
        app,
        [
            "validate",
            "task",
            "--config",
            str(config),
            "--set",
            'verification.llm.reasoning_effort="high"',
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert observed["reference"] == "task"
    assert observed["settings"].verification.llm.reasoning_effort == "high"
    assert observed["runs_dir"] == tmp_path / "runs"


def test_prepare_uses_selection_and_starts_no_runtime_services(
    monkeypatch: pytest.MonkeyPatch,
    write_task_repo,  # type: ignore[no-untyped-def]
) -> None:
    repository = write_task_repo("ale-tasks-prepare", tasks=("b", "a"))
    task_cli = import_module("ale.run.cli.tasks")
    observed: list[str] = []
    digest = "sha256:" + "a" * 64

    async def fake_prepare(tasks, _providers):  # type: ignore[no-untyped-def]
        observed.extend(str(task.spec.name) for task in tasks)
        image = PreparedTaskImage(
            kind=ImageKind.CONTAINER,
            source="solver-local",
            input_identity=digest,
            image_source_identity=digest,
            runtime_ref="ale-task:fixture",
            prepared_identity=digest,
        )
        return tuple(
            ImagePreparationResult(
                task=f"{task.spec.name}@{task.spec.variant}",
                role="solver",
                image=image,
                steps=(ImagePreparationStep("oci-build", "executed", digest),),
            )
            for task in tasks
        )

    def forbidden(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("prepare started a runtime service")

    monkeypatch.setattr(task_cli, "_prepare_images", fake_prepare)
    for name in ("Gateway", "Ledger", "StandardEnvironment"):
        monkeypatch.setattr(task_cli, name, forbidden)

    result = CliRunner().invoke(app, ["prepare", str(repository)])

    assert result.exit_code == 0, result.output
    assert observed == ["a", "b"]
    assert "a@base solver kind=container source=local" in result.output
    assert "oci-build" in result.output


def test_prepare_accepts_only_config_and_set_options() -> None:
    result = CliRunner().invoke(app, ["prepare", "--help"])
    assert result.exit_code == 0
    assert "--config" in result.output
    assert "--set" in result.output
    for forbidden in ("--runs-dir", "--agent", "--model", "--episodes", "--build"):
        assert forbidden not in result.output


def test_validation_failure_output_is_actionable() -> None:
    verdict = Verdict.failed(
        Status.ENV_ERROR,
        VerificationInfrastructureError("JUDGE_KEY is not set"),
        phase="verify",
    )
    assert _validation_outcome(verdict) == (
        "env_error (VerificationInfrastructureError: JUDGE_KEY is not set)"
    )


def test_partial_oracle_is_a_warning_not_a_failure() -> None:
    names, warnings, failures = _validation_notices(
        Verdict.completed({"correctness": 0.0}),
        Verdict.completed({"correctness": 0.5}),
    )
    assert names == ("correctness",)
    assert [notice.code for notice in warnings] == ["partial_oracle"]
    assert failures == ()


def test_nonzero_untouched_and_name_mismatch_are_hard_failures() -> None:
    _, _, failures = _validation_notices(
        Verdict.completed({"correctness": 0.1}),
        Verdict.completed({"other": 1.0}),
    )
    assert {notice.code for notice in failures} == {
        "untouched_nonzero",
        "reward_name_mismatch",
    }


def test_infrastructure_failure_remains_a_hard_failure() -> None:
    _, _, failures = _validation_notices(
        Verdict.failed(Status.ENV_ERROR, RuntimeError("no sandbox"), phase="provision"),
        Verdict.completed({"reward": 1.0}),
    )
    assert [notice.code for notice in failures] == ["untouched_not_completed"]
