"""Calibration: how a Task declares its cases, what each expects, and how a run checks them."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import jsonschema
import pytest
from pydantic import ValidationError
from test_runtime_contracts import make_lock
from tests.support import edit_task_config
from typer.testing import CliRunner

from ale.core.config import RunConfig
from ale.core.errors import TaskDefinitionError
from ale.core.harness import HarnessSession
from ale.core.result import FailureInfo, ResultRecord
from ale.core.sandbox import ExecResult, PreparedTaskImage
from ale.core.taskspec import CalibrationCase, ImageKind, OperatingSystem, TaskConfig
from ale.core.verdict import Status, Verdict
from ale.run.assets import asset_roots
from ale.run.cli.main import app
from ale.run.episode import EpisodeResult
from ale.run.gateway.session import GatewaySession
from ale.run.harnesses.builtin import OracleHarness
from ale.run.lint import lint_repository
from ale.run.providers import ProviderRegistry
from ale.run.recording import atomic_write_json
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from ale_verify import read_result

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "a" * 64
SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures/calibration-observation-v1.schema.json"
    ).read_text()
)


@pytest.fixture(autouse=True)
def host_checks(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """These tests fake selection and preparation, so the leaves' checks on the Host go too,
    and are only counted."""
    checks = AsyncMock()
    monkeypatch.setattr(ProviderRegistry, "preflight", checks)
    return checks


def _case(**expect: object) -> CalibrationCase:
    return CalibrationCase.model_validate({"name": "case", "expect": expect})


def _config(*entries: dict[str, Any]) -> TaskConfig:
    return TaskConfig.model_validate(
        {"schema_version": "0.2", "task": {"name": "a/b"}, "calibration": list(entries)}
    )


@pytest.mark.parametrize(
    ("expected", "value", "admitted"),
    [
        (1, 1.0, True),
        (1, 0.99, False),
        ([0.4, 0.6], 0.4, True),
        ([0.4, 0.6], 0.6, True),
        ([0.4, 0.6], 0.61, False),
        ([0.4, 0.6], 0.39, False),
    ],
)
def test_an_exact_value_must_match_and_an_interval_is_closed(
    expected: object, value: float, admitted: bool
) -> None:
    assert _case(reward=expected).admits("reward", value) is admitted


@pytest.mark.parametrize(
    "expect",
    [
        {"reward": 0.57},
        {"reward": [0, 1]},
        {"reward": [0.401, 0.41]},
        {"reward": 0, "tflops": [600, 680.5]},
        {"reward": 1, "count": 3},
    ],
)
def test_a_case_accepts_what_a_stored_reward_and_a_result_can_meet(
    expect: dict[str, object],
) -> None:
    _case(**expect)


@pytest.mark.parametrize(
    ("expect", "problem"),
    [
        ({}, "must include reward"),
        ({"tflops": 600}, "must include reward"),
        ({"reward": True}, "expected a number"),
        ({"reward": "1"}, "expected a number"),
        ({"reward": 0, "tflops": [1, False]}, "expected a number"),
        ({"reward": 0, "tflops": float("inf")}, "finite"),
        ({"reward": 0, "": 1}, "at least 1 character"),
        ({"reward": [0.6, 0.4]}, "needs lo <= hi"),
        ({"reward": [0.4]}, r"written \[lo, hi\]"),
        ({"reward": [0.1, 0.2, 0.3]}, r"written \[lo, hi\]"),
        ({"reward": -0.1}, "from 0 through 1"),
        ({"reward": 1.5}, "from 0 through 1"),
        ({"reward": [0.5, 1.2]}, "from 0 through 1"),
        ({"reward": 0.125}, "at most two decimals"),
        ({"reward": [0.401, 0.409]}, "holds no two-decimal value"),
    ],
)
def test_a_case_refuses_what_no_stored_reward_or_number_could_meet(
    expect: dict[str, object], problem: str
) -> None:
    with pytest.raises(ValidationError, match=problem):
        _config({"name": "x", "expect": expect})


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ({"name": "Upper", "expect": {"reward": 0}}, "pattern"),
        ({"name": "x", "expect": {"reward": 0}, "agent": "oracle"}, "Extra inputs"),
    ],
)
def test_a_case_is_a_slug_and_an_expectation(entry: dict[str, Any], problem: str) -> None:
    with pytest.raises(ValidationError, match=problem):
        _config(entry)


def test_solution_comes_first_and_an_entry_only_overrides_its_default() -> None:
    assert [(case.name, case.expect) for case in _config().calibration_cases] == [
        ("solution", {"reward": 1.0}),
    ]
    config = _config(
        {"name": "hardcoded-output", "expect": {"reward": 0}},
        {"name": "solution", "expect": {"reward": [0.0, 0.05], "tflops": [600, 680]}},
        {"name": "missing-summary", "expect": {"reward": [0.4, 0.6]}},
    )
    assert [(case.name, case.expect) for case in config.calibration_cases] == [
        ("solution", {"reward": (0.0, 0.05), "tflops": (600.0, 680.0)}),
        ("hardcoded-output", {"reward": 0.0}),
        ("missing-summary", {"reward": (0.4, 0.6)}),
    ]
    with pytest.raises(ValidationError, match="names must be unique"):
        _config({"name": "x", "expect": {"reward": 0}}, {"name": "x", "expect": {"reward": 1}})


def _with_case(task: Path, name: str, entry: str = "solve.sh") -> None:
    directory = task / "calibration" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / entry).write_text("#!/usr/bin/env bash\n")


def _declare(task: Path, *names: str) -> None:
    edit_task_config(
        task, {"calibration": [{"name": name, "expect": {"reward": 0.5}} for name in names]}
    )


def test_declared_cases_follow_the_solution_which_needs_no_entry(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    edit_task_config(
        task,
        {
            "calibration": [
                {"name": "partial", "expect": {"reward": 0.5}},
                {"name": "nop", "expect": {"reward": 0}},
            ]
        },
    )
    _with_case(task, "partial")
    # No name is reserved: a case that leaves the start state is an ordinary one.
    _with_case(task, "nop")
    loaded = load_tasks(task)[0]
    assert [(case.name, case.expect) for case in loaded.spec.calibration] == [
        ("solution", {"reward": 1.0}),
        ("partial", {"reward": 0.5}),
        ("nop", {"reward": 0.0}),
    ]
    assert loaded.folder.case_dir("partial") == task / "calibration/partial"
    assert loaded.folder.case_dir("solution") == task / "solution"
    assert lint_repository(task) == []


@pytest.mark.parametrize(
    ("arrange", "problem"),
    [
        (
            lambda task: _declare(task, "partial"),
            r"missing required Task file\(s\): calibration/partial/solve\.sh",
        ),
        (
            lambda task: _with_case(task, "stray"),
            r"calibration/stray/ needs a \[\[calibration\]\] entry",
        ),
        (
            lambda task: _with_case(task, "nop"),
            r"calibration/nop/ needs a \[\[calibration\]\] entry",
        ),
        (
            lambda task: shutil.rmtree(task / "solution"),
            r"missing required Task file\(s\): solution/solve\.sh",
        ),
    ],
    ids=(
        "declared-without-directory",
        "directory-without-entry",
        "no-reserved-name",
        "no-reference-solution",
    ),
)
def test_a_case_and_its_directory_come_together(
    tmp_path: Path, arrange: Callable[[Path], None], problem: str
) -> None:
    task = scaffold_task(tmp_path / "demo")
    arrange(task)
    with pytest.raises(TaskDefinitionError, match=problem):
        load_tasks(task)
    assert any(re.search(problem, str(finding)) for finding in lint_repository(task))


def test_the_reference_solution_is_where_harbor_keeps_it(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    (task / "calibration").mkdir()
    (task / "solution").rename(task / "calibration/solution")
    problem = (
        "calibration/solution/ is not a calibration case; the reference solution lives in solution/"
    )
    with pytest.raises(TaskDefinitionError, match=problem):
        load_tasks(task)
    findings = {finding.path: finding.message for finding in lint_repository(task)}
    assert findings[task / "calibration/solution"].startswith(
        "the reference solution lives in solution/"
    )
    assert any(message == "no solution/solve.sh" for message in findings.values())


def _file_in_calibration(task: Path) -> None:
    (task / "calibration/README.md").write_text("Why each case scores as it does.\n")


def _linked_case(task: Path) -> None:
    _declare(task, "partial")
    (task / "calibration/partial").symlink_to(task / "solution", target_is_directory=True)


def _linked_file(task: Path) -> None:
    (task / "calibration/notes.md").symlink_to(task / "instruction.md")


def _calibration_link(task: Path) -> None:
    # Within the Task, since a link out of it fails the Task as it loads.
    elsewhere = task / "tests/cases"
    (task / "calibration").rename(elsewhere)
    (task / "calibration").symlink_to(elsewhere, target_is_directory=True)


@pytest.mark.parametrize(
    ("arrange", "path"),
    [
        (_file_in_calibration, "calibration/README.md"),
        (_linked_case, "calibration/partial"),
        (_linked_file, "calibration/notes.md"),
        (_calibration_link, "calibration"),
    ],
    ids=("file", "linked-case", "linked-file", "calibration-link"),
)
def test_calibration_holds_only_case_directories(
    tmp_path: Path, arrange: Callable[[Path], None], path: str
) -> None:
    task = scaffold_task(tmp_path / "demo")
    (task / "calibration").mkdir()
    arrange(task)
    load_tasks(task)
    (finding,) = lint_repository(task)
    assert finding.path == task / path


def test_a_windows_case_runs_solve_ps1(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    (task / "environment/Dockerfile").unlink()
    (task / "solution/solve.sh").rename(task / "solution/solve.ps1")
    (task / "tests/test.sh").rename(task / "tests/test.ps1")
    edit_task_config(task, {"environment": {"os": "windows", "image": "/srv/windows.qcow2"}})
    _declare(task, "partial")
    _with_case(task, "partial")
    with pytest.raises(TaskDefinitionError, match=r"calibration/partial/solve\.ps1"):
        load_tasks(task)
    _with_case(task, "partial", "solve.ps1")
    assert [case.name for case in load_tasks(task)[0].spec.calibration][-1] == "partial"


def test_each_case_keeps_assets_in_its_directory(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    _declare(task, "partial")
    _with_case(task, "partial")
    before = load_tasks(task)[0]
    assert {"calibration/partial/assets", "solution/assets"} <= set(asset_roots(task))
    asset = task / "calibration/partial/assets/reference.bin"
    asset.parent.mkdir()
    asset.write_bytes(b"large")
    assert load_tasks(task)[0].task_digest == before.task_digest
    (task / "calibration/partial/solve.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    assert load_tasks(task)[0].task_digest != before.task_digest


class _ExecSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    async def exec(self, argv: list[str], **kwargs: Any) -> ExecResult:
        self.calls.append((argv, kwargs))
        return ExecResult(exit_code=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operating_system", "home", "stage", "entry"),
    [
        (OperatingSystem.LINUX, "/home/user", "/home/user/.ale-solution", "solve.sh"),
        (OperatingSystem.WINDOWS, "C:\\Users\\user", "C:\\Users\\user\\.ale-solution", "solve.ps1"),
    ],
)
async def test_the_oracle_runs_its_solution_in_its_stage_directory(
    operating_system: OperatingSystem, home: str, stage: str, entry: str
) -> None:
    sandbox = _ExecSandbox()
    session = HarnessSession(
        episode_id="e", gateway_url="", token="t", model="", os=operating_system, home=home
    )
    await OracleHarness("partial").launch("", sandbox, session, timeout_sec=5)  # type: ignore[arg-type]
    ((argv, kwargs),) = sandbox.calls
    assert argv[-1].endswith(entry) and argv[-1].startswith(stage)
    assert kwargs["cwd"] == stage
    assert kwargs["env"] == {"ALE_HOME": home, "ALE_STAGE_DIR": stage}


#: What a case's episode ends with: the result its verifier wrote, or how it failed.
Outcome = Mapping[str, object] | Status


async def _calibrate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entries: list[dict[str, Any]],
    outcomes: Mapping[str, Outcome],
    *,
    retries: int = 0,
) -> tuple[int, dict[str, Any], list[str], list[str]]:
    """Calibrate a scaffolded Task declaring ``entries`` with faked episodes.

    Each case's episode ends as ``outcomes`` says, by default with full marks; a verifier's
    result is stored rounded, as the engine stores it. Returns the exit code, the Task's
    calibration record, the console lines and the cases each episode ran, in order.
    """
    task_cli = import_module("ale.run.cli.tasks")
    task = scaffold_task(tmp_path / "demo")
    if entries:
        edit_task_config(task, {"calibration": entries})
    for entry in entries:
        if entry["name"] != "solution":
            _with_case(task, entry["name"])
    ran: list[str] = []
    lock = make_lock()

    async def prepare(tasks, _providers, _continued=None):  # type: ignore[no-untyped-def]
        for item in tasks:
            item.prepared_image = PreparedTaskImage(
                kind=ImageKind.CONTAINER,
                source="solver-local",
                input_identity=DIGEST,
                image_source_identity=DIGEST,
                runtime_ref="ale-task:fixture",
                prepared_identity=DIGEST,
            )
        return ()

    async def execute(_task, protocol, _providers, **kwargs):  # type: ignore[no-untyped-def]
        harness = protocol.harness
        assert isinstance(harness, OracleHarness) and protocol.agent_enabled
        case = harness.case
        ran.append(case)
        episode_id = kwargs["episode_id"]
        directory = kwargs["run_dir"] / episode_id
        directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        outcome = outcomes.get(case, {"reward": 1.0})
        if isinstance(outcome, Status):
            failure = FailureInfo(error_type="ProviderStartError", message="no sandbox")
            record = ResultRecord(
                episode_id=episode_id,
                status=outcome,
                failure=failure,
                started_at=now,
                finished_at=now,
            )
            atomic_write_json(directory / "result.json", record)
            verdict = Verdict.failed(outcome, RuntimeError("no sandbox"), phase="provision")
            return EpisodeResult(episode_id, verdict, directory, 0, record)
        result = read_result(json.dumps({"reward": outcome["reward"]}), json.dumps(dict(outcome)))
        rewards = {"reward": result["reward"]}
        record = ResultRecord(
            episode_id=episode_id,
            status=Status.COMPLETED,
            rewards=rewards,
            started_at=now,
            finished_at=now,
        )
        atomic_write_json(directory / "result.json", record)
        atomic_write_json(directory / "lock.json", lock)
        (directory / "verification.json").write_text(json.dumps(result))
        return EpisodeResult(episode_id, Verdict.completed(rewards), directory, 0, record, lock)

    monkeypatch.setattr(task_cli, "_prepare_images", prepare)
    monkeypatch.setattr(task_cli, "run_episode", execute)
    code = await task_cli._calibrate(
        str(task), RunConfig(episode_retries=retries), tmp_path / "runs"
    )
    (path,) = (tmp_path / "runs").glob("calibrate-*/calibration.json")
    record = json.loads(path.read_text())
    jsonschema.validate(record, SCHEMA)
    (observed,) = record["tasks"]
    return code, observed, capsys.readouterr().out.splitlines(), ran


def _by_name(observed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {case["name"]: case for case in observed["cases"]}


@pytest.mark.asyncio
async def test_calibrate_runs_the_solution_then_each_case_in_file_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, observed, output, ran = await _calibrate(
        tmp_path,
        monkeypatch,
        capsys,
        [
            {"name": "hardcoded-output", "expect": {"reward": 0}},
            {"name": "partial", "expect": {"reward": [0.4, 0.6], "tflops": [600, 680]}},
            {"name": "start-state", "expect": {"reward": 0}},
            {"name": "solution", "expect": {"reward": [0.9, 1]}},
        ],
        {
            "hardcoded-output": {"reward": 1.0},
            "partial": {"reward": 0.5, "tflops": 640},
            "start-state": {"reward": 0.0},
        },
        retries=1,
    )
    assert code == 2
    # Every case is an oracle run of its own directory, the solution first wherever its entry
    # stands, and a completed case outside its expectation is not retried.
    assert ran == ["solution", "hardcoded-output", "partial", "start-state"]
    assert observed["name"] == "agentslastexam/demo"
    assert observed["passed"] is False
    cases = _by_name(observed)
    assert list(cases) == ran
    assert [case["passed"] for case in cases.values()] == [True, False, True, True]
    assert cases["solution"]["expect"] == {"reward": [0.9, 1.0]}
    assert cases["partial"]["expect"] == {"reward": [0.4, 0.6], "tflops": [600.0, 680.0]}
    assert cases["partial"]["observed"] == {"reward": 0.5, "tflops": 640.0}
    assert [notice["code"] for notice in cases["hardcoded-output"]["failures"]] == [
        "unexpected_value"
    ]
    for case in cases.values():
        assert case["attempt"]["lock_digest"].startswith("sha256:")
    assert output[:5] == [
        "ok    agentslastexam/demo solution: reward=1.0",
        "FAIL  agentslastexam/demo hardcoded-output: reward is 1.0, expected 0.0",
        "ok    agentslastexam/demo partial: reward=0.5, tflops=640.0",
        "ok    agentslastexam/demo start-state: reward=0.0",
        "FAIL  agentslastexam/demo: 3/4 cases as expected",
    ]


@pytest.mark.asyncio
async def test_a_case_whose_episode_fails_is_not_completed_and_the_rest_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, observed, _, ran = await _calibrate(
        tmp_path,
        monkeypatch,
        capsys,
        [{"name": "partial", "expect": {"reward": 0.5}}],
        {"solution": Status.ENV_ERROR, "partial": {"reward": 0.5}},
        retries=1,
    )
    assert code == 2
    assert ran == ["solution", "solution", "partial"]
    cases = _by_name(observed)
    attempt = cases["solution"]["attempt"]
    assert (attempt["status"], attempt["rewards"]) == ("env_error", None)
    assert (attempt["lock_path"], attempt["lock_digest"]) == (None, None)
    assert cases["solution"]["observed"] == {}
    assert [notice["code"] for notice in cases["solution"]["failures"]] == ["not_completed"]
    assert cases["partial"]["passed"] is True


@pytest.mark.asyncio
async def test_a_case_meets_its_expectation_by_the_stored_reward_and_the_results_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    measured = {"reward": [0.4, 0.6], "tflops": [600, 680]}
    entries = {
        "rounds-down": ({"reward": [0.4, 0.6]}, {"reward": 0.6049}),
        "rounds-up": ({"reward": [0.4, 0.6]}, {"reward": 0.605}),
        "exact-number": ({"reward": 0.5, "count": 3}, {"reward": 0.5, "count": 3}),
        "measured": (measured, {"reward": 0.5, "tflops": 612.5}),
        "missing": (measured, {"reward": 0.5}),
        "text": ({"reward": 0.5, "label": 1}, {"reward": 0.5, "label": "fast"}),
        "boolean": ({"reward": 0.5, "flag": 1}, {"reward": 0.5, "flag": True}),
    }
    code, observed, _, _ = await _calibrate(
        tmp_path,
        monkeypatch,
        capsys,
        [{"name": name, "expect": expect} for name, (expect, _) in entries.items()],
        {name: outcome for name, (_, outcome) in entries.items()},
    )
    assert code == 2
    cases = _by_name(observed)
    assert {
        name: (
            cases[name]["observed"],
            [notice["code"] for notice in cases[name]["failures"]],
        )
        for name in entries
    } == {
        "rounds-down": ({"reward": 0.6}, []),
        "rounds-up": ({"reward": 0.61}, ["unexpected_value"]),
        "exact-number": ({"reward": 0.5, "count": 3.0}, []),
        "measured": ({"reward": 0.5, "tflops": 612.5}, []),
        "missing": ({"reward": 0.5, "tflops": None}, ["missing_value"]),
        "text": ({"reward": 0.5, "label": None}, ["not_a_number"]),
        "boolean": ({"reward": 0.5, "flag": None}, ["not_a_number"]),
    }


def test_calibrate_accepts_run_level_judge_config(
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

    async def fake_calibrate(reference, settings, runs_dir):  # type: ignore[no-untyped-def]
        observed.update(reference=reference, settings=settings, runs_dir=runs_dir)
        return 0

    monkeypatch.setattr(cli, "_calibrate", fake_calibrate)
    result = CliRunner().invoke(
        app,
        [
            "calibrate",
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


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "expected"), [("host", True), ("none", False)])
async def test_calibration_passes_artifact_policy_to_episodes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_task_repo,  # type: ignore[no-untyped-def]
    policy: str,
    expected: bool,
) -> None:
    repository = write_task_repo("calibration-artifacts")
    task_cli = import_module("ale.run.cli.tasks")
    digest = "sha256:" + "a" * 64

    async def fake_prepare(tasks, _providers, _continued=None):  # type: ignore[no-untyped-def]
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
        await task_cli._calibrate(str(repository), settings, tmp_path / "runs")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "network",
    [
        {"network_mode": "allowlist", "allowed_hosts": ["example.com"]},
        {"network_mode": "blocklist", "blocked_hosts": ["pypi.org"]},
    ],
)
@pytest.mark.parametrize("table", ["agent", "verifier"])
@pytest.mark.parametrize("command", ["calibrate", "run"])
async def test_an_oracle_run_gives_a_listed_task_its_egress_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_task_repo,  # type: ignore[no-untyped-def]
    host_checks: AsyncMock,
    command: str,
    table: str,
    network: dict[str, object],
) -> None:
    """An oracle run calls no model, yet the oracle meets the agent's network, and the
    verifier its own, a list included: the episode gets the proxy and a registry for its
    egress session whichever of them has the list."""
    repository = write_task_repo("oracle-list", config={table: network})
    task_cli = import_module("ale.run.cli.tasks")
    digest = "sha256:" + "a" * 64

    async def fake_prepare(tasks, _providers, _continued=None):  # type: ignore[no-untyped-def]
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

    seen: dict[str, object] = {}

    async def fake_episode(task, *_args, **kwargs):  # type: ignore[no-untyped-def]
        seen["gateway"] = kwargs["gateway"]
        network = task.spec.network if table == "agent" else task.spec.verify.network
        session = kwargs["session_registry"].open(
            GatewaySession(
                episode_id=kwargs["episode_id"],
                model="",
                purpose="egress",
                policy=network,
            )
        )
        port = int(kwargs["proxy_url"].rpartition(":")[2])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"CONNECT pypi.org:443 HTTP/1.1\r\nProxy-Authorization: Bearer {session.token}"
            "\r\n\r\n".encode()
        )
        seen["status"] = (await reader.readline()).decode()
        writer.close()
        raise EpisodeReached

    monkeypatch.setattr(task_cli, "_prepare_images", fake_prepare)
    monkeypatch.setattr(task_cli, "run_episode", fake_episode)
    if command == "calibrate":
        with pytest.raises(EpisodeReached):
            await task_cli._calibrate(str(repository), RunConfig(), tmp_path / "runs")
    else:
        oracle = RunConfig.model_validate({"agent": {"name": "oracle"}})
        await task_cli._run_one(str(repository), oracle, tmp_path / "runs")
    assert seen["gateway"] is None
    # Its leaves were checked on this Host, once, before the episode.
    ((checked,), _) = host_checks.await_args
    assert [task.spec.name for task in checked] == ["agentslastexam/demo"]
    assert host_checks.await_count == 1
    # The proxy knows the episode's session, and holds it to the Task's list.
    assert str(seen["status"]).split()[1] == "403", seen["status"]
