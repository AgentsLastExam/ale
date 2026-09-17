from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_runtime_contracts import make_lock
from typer.testing import CliRunner

from ale.core.config import RunConfig, SandboxRetentionConfig
from ale.core.result import FailureInfo, ResultRecord, SandboxOutcome
from ale.core.sandbox import PreparedTaskImage, RetainedSandbox, SandboxRequest
from ale.core.verdict import Status, Verdict
from ale.run.cli.tasks import _retained_source_episodes
from ale.run.episode import EpisodeResult, _Lease, run_episode, run_episode_with_retries
from ale.run.ledger import Ledger
from ale.run.recording import atomic_write_json
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit


def write_result(
    root: Path, status: Status, attempt: int, *, failure_type: str = "TestFailure"
) -> EpisodeResult:
    root.mkdir(parents=True, exist_ok=True)
    assert not (root / "result.json").exists(), "retry reused previous execution files"
    now = datetime.now(UTC)
    record = ResultRecord(
        episode_id=root.name,
        status=status,
        rewards={"quality": 0.0} if status is Status.COMPLETED else None,
        failure=None
        if status is Status.COMPLETED
        else FailureInfo(error_type=failure_type, message=f"failure {attempt}"),
        started_at=now,
        finished_at=now,
    )
    atomic_write_json(root / "result.json", record)
    (root / "logs").mkdir()
    (root / "logs/native.log").write_text(f"attempt {attempt}")
    verdict = (
        Verdict.completed({"quality": 0.0})
        if status is Status.COMPLETED
        else Verdict.failed(status, RuntimeError(f"failure {attempt}"))
    )
    return EpisodeResult(root.name, verdict, root, 0, record)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [status for status in Status if status not in {Status.COMPLETED, Status.TIMEOUT}]
)
async def test_retry_preserves_failed_evidence_and_stops_on_completed_zero(
    tmp_path: Path, failure: Status
) -> None:
    calls = 0
    episode_dir = tmp_path / "run/episode"

    async def execute() -> EpisodeResult:
        nonlocal calls
        calls += 1
        return write_result(episode_dir, failure if calls == 1 else Status.COMPLETED, calls)

    result = await run_episode_with_retries(execute, retries=2)

    assert calls == 2
    assert result.record.rewards == {"quality": 0.0}
    assert list(tmp_path.glob("*/*/result.json")) == [episode_dir / "result.json"]
    assert (episode_dir / "attempts/1/logs/native.log").read_text() == "attempt 1"
    previous = ResultRecord.model_validate_json(
        (episode_dir / "attempts/1/result.json").read_text()
    )
    assert previous.status is failure
    assert previous.episode_id == result.episode_id


@pytest.mark.asyncio
async def test_timeout_is_terminal_without_retry(tmp_path: Path) -> None:
    calls = 0

    async def execute() -> EpisodeResult:
        nonlocal calls
        calls += 1
        return write_result(
            tmp_path / "episode", Status.TIMEOUT, calls, failure_type="PhaseTimeoutError"
        )

    result = await run_episode_with_retries(execute, retries=3)
    assert calls == 1
    assert result.record.status is Status.TIMEOUT
    assert not (result.run_dir / "attempts").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type",
    [
        "VerificationInfrastructureError",
        "VerificationConfigurationError",
        "VerificationProviderError",
        "VerificationAdapterError",
        "VerificationVerdictError",
    ],
)
async def test_judge_failure_is_not_retried_as_an_episode(
    tmp_path: Path, failure_type: str
) -> None:
    calls = 0

    async def execute() -> EpisodeResult:
        nonlocal calls
        calls += 1
        return write_result(
            tmp_path / "episode", Status.ENV_ERROR, calls, failure_type=failure_type
        )

    result = await run_episode_with_retries(execute, retries=3)
    assert calls == 1
    assert result.record.status is Status.ENV_ERROR
    assert result.record.failure.error_type == failure_type
    assert not (result.run_dir / "attempts").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 2])
async def test_retry_exhaustion_keeps_only_the_last_failure_current(
    tmp_path: Path, retries: int
) -> None:
    calls = 0

    async def execute() -> EpisodeResult:
        nonlocal calls
        calls += 1
        return write_result(tmp_path / "episode", Status.TASK_ERROR, calls)

    result = await run_episode_with_retries(execute, retries=retries)
    assert calls == retries + 1
    assert result.record.failure is not None
    assert result.record.failure.message == f"failure {retries + 1}"
    assert len(list(result.run_dir.glob("attempts/*/result.json"))) == retries


@pytest.mark.asyncio
async def test_cancellation_propagates_without_starting_another_attempt(tmp_path: Path) -> None:
    calls = 0

    async def execute() -> EpisodeResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        return write_result(tmp_path / "episode", Status.ENV_ERROR, calls)

    with pytest.raises(asyncio.CancelledError):
        await run_episode_with_retries(execute, retries=5)
    assert calls == 2
    assert (tmp_path / "episode/attempts/1/result.json").is_file()
    assert not (tmp_path / "episode/result.json").exists()


@pytest.mark.asyncio
async def test_failed_retry_destroys_even_sanitized_keep_sandboxes() -> None:
    digest = "sha256:" + "a" * 64
    request = SandboxRequest(
        episode_id="episode",
        resources={},
        network={},
        prepared_image=PreparedTaskImage(
            kind="container",
            source="external-ref",
            input_identity=digest,
            runtime_ref="image@" + digest,
            prepared_identity=digest,
            resolved_reference="image@" + digest,
        ),
    )
    sandbox = SimpleNamespace(
        sandbox_id="fresh-sandbox",
        destroy=AsyncMock(),
        retain=AsyncMock(),
        release_resources=lambda: None,
    )
    provider = SimpleNamespace(name="docker", create=AsyncMock(return_value=sandbox))
    lease = _Lease(
        SimpleNamespace(get=lambda _: provider),  # type: ignore[arg-type]
        SandboxRetentionConfig(solver="keep", verifier="keep"),
    )
    acquired = await lease.acquire(request)
    lease.mark_sanitized(acquired, succeeded=True)
    await lease.finalize(allow_retention=False)
    sandbox.destroy.assert_awaited_once()
    sandbox.retain.assert_not_awaited()
    assert not lease.live
    assert not any(outcome.handle for outcome in lease.outcomes)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["blob", "lock", "cancel"])
async def test_runtime_cleans_sandboxes_for_late_failure_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    runtime = import_module("ale.run.episode")
    task = load_tasks(scaffold_task(tmp_path / "task"))[0]
    digest = "sha256:" + "a" * 64
    image = PreparedTaskImage(
        kind="container",
        source="solver-local",
        input_identity=digest,
        runtime_ref="image@" + digest,
        prepared_identity=digest,
        image_source_identity=digest,
    )
    task.prepared_image = task.prepared_verifier_image = image
    sandbox = SimpleNamespace(
        sandbox_id="sandbox",
        destroy=AsyncMock(),
        retain=AsyncMock(
            return_value=RetainedSandbox(
                provider="docker",
                handle="docker:test",
                episode_id="episode",
                roles=("solver", "verifier"),
                reason="test",
                cleanup_command="cleanup test",
            )
        ),
        release_resources=lambda: None,
    )
    provider = SimpleNamespace(name="docker", create=AsyncMock(return_value=sandbox))

    async def environment_run(_task, ctx):  # type: ignore[no-untyped-def]
        acquired = await ctx.sandboxes.acquire(
            SandboxRequest(
                episode_id=ctx.episode_id,
                prepared_image=image,
                resources=task.spec.resources,
                network=task.spec.network,
            )
        )
        ctx.sandboxes.mark_sanitized(acquired, succeeded=True)
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "blob":
            (ctx.run_dir / "trajectory.json").write_text('{"path":"blobs/missing"}')
        return Verdict.completed({"quality": 1.0})

    def fail_lock(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("invalid final provenance")

    monkeypatch.setattr(runtime, "_write_lock", fail_lock)
    episode = run_episode(
        task,
        SimpleNamespace(run=environment_run),  # type: ignore[arg-type]
        SimpleNamespace(get=lambda _: provider),  # type: ignore[arg-type]
        run_dir=tmp_path / "runs",
        episode_id="episode",
        sandbox_retention=SandboxRetentionConfig(solver="keep"),
        destroy_failed_sandboxes=True,
        provenance=SimpleNamespace(),  # type: ignore[arg-type]
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await episode
        sandbox.retain.assert_not_awaited()
    else:
        result = await episode
        assert result.record.status is Status.ENV_ERROR
        assert result.record.failure is not None
        assert result.record.failure.phase == "finalize"
        assert result.record.sandboxes[0].outcome == "destroyed"
        sandbox.retain.assert_awaited_once()
    sandbox.destroy.assert_awaited_once()


def test_reverification_source_discovery_accepts_three_levels_without_archived_results(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    episode_dir = runs / "run/episode"
    result = write_result(episode_dir, Status.COMPLETED, 1)
    retained = result.record.model_copy(
        update={
            "sandboxes": (
                SandboxOutcome(
                    roles=("solver",),
                    requested="keep",
                    outcome="retained",
                    provider="docker",
                    handle="docker:source",
                ),
            )
        }
    )
    for root in (episode_dir, episode_dir / "attempts/1", episode_dir / "artifacts/output"):
        atomic_write_json(root / "result.json", retained)
        atomic_write_json(root / "lock.json", make_lock())
    for source in (runs, runs / "run", episode_dir):
        found = _retained_source_episodes(source)
        assert len(found) == 1
        assert found[0][0] == episode_dir
        assert found[0][3] == "docker:source"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "separate"])
async def test_reverification_retries_only_with_fresh_verifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    task_cli = import_module("ale.run.cli.tasks")
    task_path = scaffold_task(tmp_path / "task")
    if mode == "separate":
        manifest = task_path / "task.yaml"
        manifest.write_text(
            manifest.read_text()
            + (
                "verify:\n  environment_mode: separate\n"
                "  resources: {cpus: 1, memory_mb: 512, storage_mb: null, gpus: 0}\n"
            )
        )
    task = load_tasks(task_path)[0]
    source = write_result(tmp_path / "source", Status.COMPLETED, 1)
    original = (source.run_dir / "result.json").read_bytes()
    lock = make_lock()
    lock = lock.model_copy(
        update={
            "task": lock.task.model_copy(
                update={
                    "name": task.spec.name,
                    "variant": task.spec.variant,
                }
            )
        }
    )
    calls = 0

    async def prepare(tasks, _providers):  # type: ignore[no-untyped-def]
        for current in tasks:
            current.prepared_image = PreparedTaskImage(
                kind="container",
                source="solver-local",
                input_identity=lock.image.input_identity,
                runtime_ref="image",
                prepared_identity=lock.image.prepared_identity,
                image_source_identity=lock.image.image_source_identity,
            )

    async def execute(_task, environment, _providers, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        assert environment.source_run_dir == source.run_dir
        assert kwargs["destroy_failed_sandboxes"]
        return write_result(
            kwargs["run_dir"] / kwargs["episode_id"],
            Status.ENV_ERROR if calls == 1 else Status.COMPLETED,
            calls,
        )

    attach = AsyncMock(side_effect=[object(), object()])
    monkeypatch.setattr(task_cli, "_prepare_images", prepare)
    monkeypatch.setattr(task_cli, "_attach_retained", attach)
    monkeypatch.setattr(
        task_cli,
        "_retained_source_episodes",
        lambda _: ((source.run_dir, source.record, lock, "docker:source"),),
    )
    monkeypatch.setattr(task_cli, "run_episode", execute)
    code = await task_cli._reverify(
        str(task_path),
        source.run_dir,
        RunConfig(episode_retries=1),
        tmp_path / "runs",
    )
    assert calls == (2 if mode == "separate" else 1)
    assert code == (0 if mode == "separate" else 2)
    assert attach.await_count == calls
    assert (source.run_dir / "result.json").read_bytes() == original
    assert len(list((tmp_path / "runs").glob("*/*/result.json"))) == 1


@pytest.mark.asyncio
async def test_validation_projects_only_final_episode_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_cli = import_module("ale.run.cli.tasks")
    task_path = scaffold_task(tmp_path / "task")
    counts: Counter[str] = Counter()
    lock = make_lock()

    async def prepare(tasks, _providers):  # type: ignore[no-untyped-def]
        for task in tasks:
            task.prepared_image = PreparedTaskImage(
                kind="container",
                source="solver-local",
                input_identity=lock.image.input_identity,
                runtime_ref="image",
                prepared_identity=lock.image.prepared_identity,
                image_source_identity=lock.image.image_source_identity,
            )

    async def execute(_task, environment, _providers, **kwargs):  # type: ignore[no-untyped-def]
        name = "oracle" if environment.agent_enabled else "untouched"
        counts[name] += 1
        failed = name == "oracle" and counts[name] == 1
        result = write_result(
            kwargs["run_dir"] / kwargs["episode_id"],
            Status.ENV_ERROR if failed else Status.COMPLETED,
            counts[name],
        )
        if not failed:
            rewards = {"quality": 1.0 if name == "oracle" else 0.0}
            result.record = result.record.model_copy(update={"rewards": rewards})
            result.verdict = Verdict.completed(rewards)
            atomic_write_json(result.run_dir / "result.json", result.record)
        result.lock = lock
        atomic_write_json(result.run_dir / "lock.json", lock)
        return result

    monkeypatch.setattr(task_cli, "_prepare_images", prepare)
    monkeypatch.setattr(task_cli, "run_episode", execute)
    assert (
        await task_cli._validate(
            str(task_path),
            RunConfig(episode_retries=1),
            tmp_path / "runs",
        )
        == 0
    )
    assert counts == {"untouched": 1, "oracle": 2}
    assert len(list((tmp_path / "runs").glob("*/*/result.json"))) == 2


@pytest.mark.asyncio
async def test_run_retries_each_episode_independently_and_resumes_only_final_successes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task_cli = import_module("ale.run.cli.tasks")
    task_path = scaffold_task(tmp_path / "task")
    calls: Counter[str] = Counter()
    digest = "sha256:" + "a" * 64

    async def prepare(tasks, _providers):  # type: ignore[no-untyped-def]
        for task in tasks:
            task.prepared_image = PreparedTaskImage(
                kind="container",
                source="solver-local",
                input_identity=digest,
                runtime_ref="image@" + digest,
                prepared_identity=digest,
                image_source_identity=digest,
            )

    async def execute(_task, _environment, _providers, **kwargs):  # type: ignore[no-untyped-def]
        episode_id = kwargs["episode_id"]
        assert kwargs["destroy_failed_sandboxes"]
        calls[episode_id] += 1
        status = (
            Status.ENV_ERROR if len(calls) == 1 and calls[episode_id] == 1 else Status.COMPLETED
        )
        return write_result(kwargs["run_dir"] / episode_id, status, calls[episode_id])

    monkeypatch.setattr(task_cli, "_prepare_images", prepare)
    monkeypatch.setattr(task_cli, "run_episode", execute)
    settings = RunConfig(episodes=2, episode_retries=2, agent={"name": "nop"})
    runs = tmp_path / "runs"
    assert await task_cli._run_one(str(task_path), settings, runs, "run") == 0
    assert "retry 1/2" in capsys.readouterr().out
    assert sorted(calls.values()) == [1, 2]
    assert len(list((runs / "run").glob("*/result.json"))) == 2
    ledger = Ledger(runs / "run")
    assert len(ledger.episodes("run")) == 2
    assert all(row.succeeded for row in ledger.episodes("run"))
    ledger.close()
    assert await task_cli._run_one(str(task_path), settings, runs, "run") == 0
    assert sorted(calls.values()) == [1, 2]


@pytest.mark.parametrize("command", ["run", "reverify", "validate"])
def test_retry_configuration_and_cli_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    cli = import_module("ale.run.cli.main")
    config = tmp_path / "run.toml"
    config.write_text("episode_retries=2\n[agent]\nname='nop'\n")
    seen: list[int] = []

    async def workflow(*args: object) -> int:
        settings = next(arg for arg in args if isinstance(arg, RunConfig))
        seen.append(settings.episode_retries)
        return 0

    for name in ("_run_one", "_reverify", "_validate"):
        monkeypatch.setattr(cli, name, workflow)
    argv = [command, "task"] + (["source"] if command == "reverify" else [])
    argv += ["--config", str(config)]
    cases = [([], 2), (["--set", "episode_retries=3"], 3)]
    if command != "validate":
        cases.append((["--set", "episode_retries=3", "--episode-retries", "1"], 1))
    for flags, expected in cases:
        result = CliRunner().invoke(cli.app, [*argv, *flags])
        assert result.exit_code == 0, result.output
        assert seen[-1] == expected
    with pytest.raises(ValidationError):
        RunConfig(episode_retries=-1)
