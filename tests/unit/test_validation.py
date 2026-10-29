"""The task-admission rule has one exact reward policy."""

from __future__ import annotations

import json
import os
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ale.core.config import RunConfig
from ale.core.errors import VerificationInfrastructureError, VerifierOutputError
from ale.core.sandbox import PreparedTaskImage
from ale.core.taskspec import ImageKind, OperatingSystem
from ale.core.verdict import Status, Verdict
from ale.run.cli.main import app
from ale.run.cli.tasks import (
    _is_full_reward_map,
    _is_zero_reward_map,
    _validation_notices,
    _validation_outcome,
)
from ale.run.harnesses.builtin import NopHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
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
        ({"overall": 1.0}, True),
        ({"overall": 1.0, "format": 1.0}, True),
        ({"overall": 0.0}, False),
        ({"overall": 0.5}, False),
        ({}, False),
        (None, False),
    ],
)
def test_full_oracle_detection(rewards: dict[str, float] | None, expected: bool) -> None:
    assert _is_full_reward_map(rewards) is expected


@pytest.mark.parametrize(
    ("rewards", "expected"),
    [
        ({"overall": 0.0}, True),
        ({"overall": 0.0, "format": 0.0}, True),
        ({"overall": 1.0}, False),
        ({"overall": 0.5}, False),
        ({}, False),
        (None, False),
    ],
)
def test_untouched_admission_checks_only_overall(
    rewards: dict[str, float] | None, expected: bool
) -> None:
    assert _is_zero_reward_map(rewards) is expected


@pytest.mark.asyncio
async def test_verifier_accepts_nested_details(tmp_path: Path) -> None:
    ctx = SimpleNamespace(
        spec=SimpleNamespace(os=OperatingSystem.LINUX), verification_secrets=(), run_dir=tmp_path
    )
    value = {"overall": 1, "details": {"count": 10, "reason": "good"}}
    observed = await EpisodeProtocol(NopHarness())._read_result(ctx, RewardsSandbox(value))
    assert observed == value
    assert json.loads((tmp_path / "verification.json").read_text()) == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_score", "expected", "full", "zero"),
    [(0.999, 1.0, True, False), (0.994, 0.99, False, False), (0.004, 0.0, False, True)],
)
async def test_validation_uses_the_saved_result_precision(
    tmp_path: Path, raw_score: float, expected: float, full: bool, zero: bool
) -> None:
    ctx = SimpleNamespace(
        spec=SimpleNamespace(os=OperatingSystem.LINUX), verification_secrets=(), run_dir=tmp_path
    )
    value = {"overall": raw_score, "details": {"raw": raw_score}}
    observed = await EpisodeProtocol(NopHarness())._read_result(ctx, RewardsSandbox(value))
    assert observed == {"overall": expected, "details": {"raw": raw_score}}
    assert json.loads((tmp_path / "verification.json").read_text()) == observed
    rewards = {"overall": observed["overall"]}
    assert _is_full_reward_map(rewards) is full
    assert _is_zero_reward_map(rewards) is zero


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"overall": "1"},
        {"overall": True},
        {"overall": float("nan")},
        {"overall": 1.0001},
        b"not-json",
        FileNotFoundError("missing"),
    ],
)
async def test_verifier_rejects_bad_result(tmp_path: Path, payload: object) -> None:
    ctx = SimpleNamespace(
        spec=SimpleNamespace(os=OperatingSystem.LINUX), verification_secrets=(), run_dir=tmp_path
    )
    with pytest.raises(VerifierOutputError):
        await EpisodeProtocol(NopHarness())._read_result(ctx, RewardsSandbox(payload))


def test_validate_accepts_run_level_judge_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli = import_module("ale.run.cli.main")
    config = tmp_path / "verification.toml"
    config.write_text(
        "[verification.llm]\n"
        'provider = "openai"\n'
        'dialect = "openai-responses"\n'
        'model = "gpt-test"\n'
        'reasoning_effort = "medium"\n'
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


def test_every_command_reads_the_selected_credential_file_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    task_cli = import_module("ale.run.cli.tasks")
    credential_file = tmp_path / "judge.env"
    credential_file.write_text("TEST_VALIDATION_JUDGE_KEY=test-secret\n")
    monkeypatch.setenv("ALE_ENV_FILE", str(credential_file))
    monkeypatch.delenv("TEST_VALIDATION_JUDGE_KEY", raising=False)

    def inspect_environment(settings: RunConfig) -> None:
        assert os.environ["TEST_VALIDATION_JUDGE_KEY"] == "test-secret"
        raise RuntimeError("credential loading verified before provisioning")

    monkeypatch.setattr(task_cli, "ProviderRegistry", inspect_environment)
    try:
        result = CliRunner().invoke(app, ["validate", str(tmp_path / "task")])
    finally:
        os.environ.pop("TEST_VALIDATION_JUDGE_KEY", None)
    assert isinstance(result.exception, RuntimeError), result.output
    assert "credential loading verified" in str(result.exception)


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
                task=str(task.spec.name),
                role="solver",
                image=image,
                steps=(ImagePreparationStep("oci-build", "executed", digest),),
            )
            for task in tasks
        )

    def forbidden(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("prepare started a runtime service")

    monkeypatch.setattr(task_cli, "_prepare_images", fake_prepare)
    for name in ("Gateway", "Ledger", "EpisodeProtocol"):
        monkeypatch.setattr(task_cli, name, forbidden)

    result = CliRunner().invoke(app, ["prepare", str(repository)])

    assert result.exit_code == 0, result.output
    assert observed == ["a", "b"]
    assert "a solver kind=container source=local" in result.output
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


def test_partial_oracle_is_a_hard_failure() -> None:
    names, warnings, failures = _validation_notices(
        Verdict.completed({"overall": 0.0}),
        Verdict.completed({"overall": 0.5}),
    )
    assert names == ("overall",)
    assert warnings == ()
    assert [notice.code for notice in failures] == ["oracle_not_full"]


def test_nonzero_untouched_is_a_failure_and_other_fields_are_ignored() -> None:
    _, _, failures = _validation_notices(
        Verdict.completed({"overall": 0.1}),
        Verdict.completed({"overall": 1.0, "other": 0.2}),
    )
    assert {notice.code for notice in failures} == {
        "untouched_nonzero",
    }


def test_infrastructure_failure_remains_a_hard_failure() -> None:
    _, _, failures = _validation_notices(
        Verdict.failed(Status.ENV_ERROR, RuntimeError("no sandbox"), phase="provision"),
        Verdict.completed({"overall": 1.0}),
    )
    assert [notice.code for notice in failures] == ["untouched_not_completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "expected"), [("host", True), ("none", False)])
async def test_validation_passes_artifact_policy_to_episodes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_task_repo,  # type: ignore[no-untyped-def]
    policy: str,
    expected: bool,
) -> None:
    repository = write_task_repo("validation-artifacts")
    task_cli = import_module("ale.run.cli.tasks")
    digest = "sha256:" + "a" * 64

    async def fake_prepare(tasks, _providers):  # type: ignore[no-untyped-def]
        for task in tasks:
            task.prepared_image = PreparedTaskImage(
                kind=ImageKind.CONTAINER,
                source="solver-local",
                input_identity=digest,
                image_source_identity=digest,
                runtime_ref="ale-task:fixture",
                prepared_identity=digest,
            )
        return ()

    class EpisodeReached(Exception):
        pass

    async def fake_episode(*_args, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["collect_artifacts"] is expected
        assert kwargs["seed"] == 42
        raise EpisodeReached

    monkeypatch.setattr(task_cli, "_prepare_images", fake_prepare)
    monkeypatch.setattr(task_cli, "run_episode", fake_episode)
    settings = RunConfig.model_validate({"seed": 42, "artifacts": {"collect": policy}})
    with pytest.raises(EpisodeReached):
        await task_cli._validate(str(repository), settings, tmp_path / "runs")
