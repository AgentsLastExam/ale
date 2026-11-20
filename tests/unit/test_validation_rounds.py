"""Validation rounds: the agent's turns, each followed by validation/'s result, then tests/."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support import (
    MemoryProvider,
    MemorySandbox,
    RoundsProvider,
    TurnsHarness,
    Verifiers,
    edit_task_config,
    provider_registry,
    rounds_task,
)
from tests.unit.test_runtime_contracts import DIGEST, make_lock

from ale.core.config import RunConfig, SandboxRetentionConfig
from ale.core.errors import (
    AgentError,
    AgentUnsupportedError,
    ConfigError,
    PhaseTimeoutError,
    ProviderStartError,
    TaskDefinitionError,
    TaskError,
    VerifierOutputError,
)
from ale.core.harness import (
    AgentRun,
    AutonomousHarness,
    ResumeSupport,
)
from ale.core.result import ResultRecord
from ale.core.sandbox import ExecResult, PreparedTaskImage, SandboxRequest, SandboxRole
from ale.core.task import Stage
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources, TaskConfig
from ale.core.trajectory import AtifAgent, AtifTrajectory, TrajectoryBuilder
from ale.core.verdict import Status
from ale.run.cli.tasks import _retained_source_episodes
from ale.run.episode import check_retention, run_episode
from ale.run.harnesses.builtin import NopHarness, OracleHarness, ScriptedPolicyHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.lint import lint_repository
from ale.run.protocol.episode_protocol import EpisodeProtocol, validation_rounds
from ale.run.provenance import ProvenanceInputs
from ale.run.scaffold import scaffold_task
from ale.run.secrets import resolve_task_variables
from ale.run.tasksets.manifest import ManifestTask, load_tasks

pytestmark = pytest.mark.unit

IMAGE = PreparedTaskImage(
    kind="container",
    source="external-ref",
    input_identity=DIGEST,
    runtime_ref="image@" + DIGEST,
    prepared_identity=DIGEST,
    resolved_reference="image@" + DIGEST,
)


def _task(tmp_path: Path, **options: Any) -> ManifestTask:
    task = rounds_task(tmp_path, **options)
    task.prepared_image = IMAGE
    return task


async def _run(
    tmp_path: Path,
    provider: RoundsProvider,
    task: ManifestTask,
    harness: AutonomousHarness,
    *,
    rounds: int | None = None,
    episode: dict[str, Any] | None = None,
    **retention: str,
) -> Any:
    lock = make_lock()
    rounds = task.spec.validation_rounds if rounds is None else rounds
    return await run_episode(
        task,
        EpisodeProtocol(harness, validation_rounds=rounds),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
        sandbox_retention=SandboxRetentionConfig(**retention),  # type: ignore[arg-type]
        **(episode or {}),
        provenance=ProvenanceInputs(
            source=lock.task.source,
            agent=lock.agent,
            gateway=lock.gateway,
            config_hash=lock.config_hash,
            framework=lock.framework,
            validation_rounds=rounds,
        ),
    )


def _trace(result: Any) -> list[dict[str, Any]]:
    lines = (result.run_dir / "trace.execution.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


# --- the loop ---


@pytest.mark.asyncio
async def test_n_rounds_give_n_validations_and_n_plus_one_turns_then_tests_once(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    provider, harness = RoundsProvider(), TurnsHarness()
    task = _task(tmp_path, rounds=2)
    result = await _run(tmp_path, provider, task, harness)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    # tests/ scored the final state, once; validation/ the state after turns 1 and 2.
    assert result.verdict.rewards == {"reward": 1.0}
    assert [stage for stage, _ in verifiers.runs] == [
        "/opt/ale/validation",
        "/opt/ale/validation",
        "/opt/ale/tests",
    ]
    rounds = result.record.validation_rounds
    assert [(item.index, item.status, item.reward) for item in rounds] == [
        (1, Status.COMPLETED, 0.33),
        (2, Status.COMPLETED, 0.67),
    ]
    # The feedback is kept as a file of the episode and recorded by digest and size.
    feedback = rounds[0].feedback
    assert feedback is not None and feedback.path == "validation/1/feedback.md"
    assert (result.run_dir / feedback.path).read_text() == "1 lines; write more"
    assert feedback.size_bytes == len("1 lines; write more")
    assert (result.run_dir / "validation/2/verification.json").is_file()
    # tests/'s feedback is not even read.
    assert not (result.run_dir / "feedback.md").exists()

    # Each turn after the first runs in a solver booted from the round's snapshot, beside a
    # validation verifier booted from the same snapshot with the solver's size, no sudo and
    # an open network; the solver is requested as one that saves snapshots while a round
    # follows its turn.
    solvers = provider.booted(SandboxRole.SHARED)
    validators = provider.booted(SandboxRole.VERIFIER)
    assert [request.snapshot is None for request in solvers] == [True, False, False]
    assert [request.planned_saves for request in solvers] == [2, 1, 0]
    assert [request.snapshot for request in validators] == [
        request.snapshot for request in solvers[1:]
    ]
    for request in validators:
        assert request.resources == solvers[0].resources.model_copy(update={"sudo": False})
        assert (request.sudo, request.network.mode) == (False, NetworkMode.OPEN)
        assert request.retention == "destroy"
    assert len(set(harness.sandboxes)) == 3
    # Nothing is left: every Sandbox and every round's snapshot is gone.
    assert provider.snapshots == {}
    assert all(sandbox.destroyed for sandbox in provider.sandboxes)
    assert result.lock is not None and result.lock.validation_rounds == 2


@pytest.mark.asyncio
async def test_the_prompts_say_how_the_turns_go_and_show_validation_never_tests(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    harness = TurnsHarness()
    task = _task(tmp_path, rounds=2)
    result = await _run(tmp_path, RoundsProvider(), task, harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure

    first, second, third = harness.prompts
    # The instruction stays verbatim above the preamble.
    assert first.startswith(task.spec.instruction)
    preamble = first.removeprefix(task.spec.instruction)
    for said in ("validation set", "at most 2 times", "hidden test set", "10 minutes", "turn"):
        assert said in preamble
    assert "do not survive into your next turn" in preamble
    assert second.splitlines()[0] == "Validation round 1 of 2: reward 0.33."
    assert "<feedback>\n1 lines; write more\n</feedback>" in second
    assert "Validation rounds left: 1." in second and "validated again" in second
    assert third.splitlines()[0] == "Validation round 2 of 2: reward 0.67."
    assert "Validation rounds left: none." in third and "hidden test set" in third
    # What tests/ says never reaches the agent.
    assert not any("hidden test detail" in prompt for prompt in harness.prompts)

    # One trajectory of the native session across turns, each round's prompt marked.
    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    users = [step for step in trajectory.steps if step.source == "user"]
    assert [step.message for step in users] == harness.prompts
    assert [(step.extra or {}).get("ale", {}).get("validation_round") for step in users] == [
        None,
        1,
        2,
    ]


@pytest.mark.asyncio
async def test_each_turn_has_what_is_left_of_the_agents_time(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saves, validation and boots spend none of it."""
    clock = [1000.0]

    class Timed(TurnsHarness):
        async def _turn(self, *args: Any) -> AgentRun:
            clock[0] += 60
            return await super()._turn(*args)

    def validating(sandbox: MemorySandbox) -> None:
        clock[0] += 500

    verifiers.seen = validating
    monkeypatch.setattr(
        "ale.run.protocol.episode_protocol.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    harness = Timed()
    result = await _run(tmp_path, RoundsProvider(), _task(tmp_path, rounds=2), harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert harness.timeouts == [600, 540, 480]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "status"),
    [
        (PhaseTimeoutError("agent", 600), Status.TIMEOUT),
        (AgentError("the agent failed"), Status.AGENT_ERROR),
    ],
)
async def test_a_turn_that_did_not_end_normally_is_the_last(
    tmp_path: Path, verifiers: Verifiers, stop: Exception, status: Status
) -> None:
    provider, harness = RoundsProvider(), TurnsHarness(stops={2: stop})
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=3), harness)
    # Two turns and one round; its state is still scored, as any stopped agent's is.
    assert result.verdict.status is status
    assert len(harness.prompts) == 2
    assert [item.index for item in result.record.validation_rounds] == [1]
    assert result.record.rewards == {"reward": 0.67}
    assert [stage for stage, _ in verifiers.runs][-1] == "/opt/ale/tests"
    assert provider.snapshots == {}
    assert all(sandbox.destroyed for sandbox in provider.sandboxes)


@pytest.mark.asyncio
async def test_a_round_without_a_score_is_reported_as_such_and_the_loop_goes_on(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    verifiers.failing = {1}
    provider, harness = RoundsProvider(), TurnsHarness()
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=2), harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    first, second = result.record.validation_rounds
    assert (first.status, first.reward, first.feedback) == (Status.TASK_ERROR, None, None)
    assert first.failure is not None and first.failure.phase == "validate"
    assert (second.status, second.reward) == (Status.COMPLETED, 0.67)
    assert harness.prompts[1].splitlines()[0] == "Validation round 1 of 2: no score (task_error)."
    assert "The validation set gave no feedback." in harness.prompts[1]
    assert len(harness.prompts) == 3
    # The failed round leaves the episode's own failure phase alone.
    assert result.record.failure is None


@pytest.mark.asyncio
async def test_a_validation_verifier_that_cannot_start_is_an_unscored_round(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    provider, harness = RoundsProvider(), TurnsHarness()
    provider.fail = lambda request: request.role is SandboxRole.VERIFIER
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=1), harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    (round_,) = result.record.validation_rounds
    assert round_.status is Status.ENV_ERROR
    assert "no score (env_error)" in harness.prompts[1]
    assert provider.snapshots == {}


@pytest.mark.asyncio
async def test_a_next_solver_that_cannot_boot_ends_the_episode_and_everything_goes(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    provider, harness = RoundsProvider(), TurnsHarness()
    provider.fail = lambda request: request.role is SandboxRole.SHARED and bool(request.snapshot)
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=2), harness)
    assert result.verdict.status is Status.ENV_ERROR
    assert len(harness.prompts) == 1
    assert [item.index for item in result.record.validation_rounds] == [1]
    # The saved solver and the validation verifier went, and then the snapshot, which no
    # Provider destroys under a Sandbox booted from it.
    first, verifier = provider.sandboxes
    assert provider.destroyed == [first.sandbox_id, verifier.sandbox_id]
    assert provider.snapshots == {}


@pytest.mark.asyncio
async def test_a_solver_that_cannot_be_saved_ends_the_episode(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def snapshot(self: MemorySandbox, **_: object) -> None:
        raise ProviderStartError("the disk could not be saved")

    monkeypatch.setattr(MemorySandbox, "snapshot", snapshot)
    provider = RoundsProvider()
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=2), TurnsHarness())
    assert result.verdict.status is Status.ENV_ERROR
    assert result.verdict.failure is not None and result.verdict.failure.phase == "agent"
    assert result.record.validation_rounds == ()
    assert all(sandbox.destroyed for sandbox in provider.sandboxes)


@pytest.mark.asyncio
async def test_a_rounds_snapshot_that_cannot_be_destroyed_is_recorded_and_never_a_source(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's turns go on; the snapshot is left for its cleanup command, which the
    operator finds by ``ale sandbox list``, and recorded as not destroyed rather than as a
    snapshot a later Run takes up, and the final solver's kept snapshot is still kept."""
    provider, harness = RoundsProvider(), TurnsHarness()
    destroy = provider.destroy_retained

    async def busy(handle: str) -> None:
        if handle == provider.saved[0]:
            raise ProviderStartError("busy")
        await destroy(handle)

    monkeypatch.setattr(provider, "destroy_retained", busy)
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=2), harness, solver="snapshot")
    # Every turn ran; the snapshot left behind fails the episode as it ends.
    assert len(harness.prompts) == 3
    assert result.verdict.status is Status.ENV_ERROR
    assert result.verdict.failure is not None and result.verdict.failure.phase == "teardown"
    handle = provider.saved[0]
    (left,) = [item for item in result.record.sandboxes if item.handle == handle]
    assert (left.outcome, left.cleanup_command) == (
        "not-destroyed",
        f"ale sandbox destroy {handle}",
    )
    assert left.reason is not None and "busy" in left.reason
    assert {"handle": handle} in await provider.list_retained()
    (kept,) = [item for item in result.record.sandboxes if item.outcome == "snapshotted"]
    assert kept.handle in provider.snapshots and kept.handle != handle
    ((_, _, _, source),) = _retained_source_episodes(result.run_dir)
    assert source == kept


@pytest.mark.asyncio
async def test_each_snapshot_goes_once_nothing_runs_on_it(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """At most one round's snapshot outlives its round, the one the next solver runs on."""
    provider, held = RoundsProvider(), []

    class Counting(TurnsHarness):
        async def _turn(self, *args: Any) -> AgentRun:
            held.append(len(provider.snapshots))
            return await super()._turn(*args)

    result = await _run(tmp_path, provider, _task(tmp_path, rounds=3), Counting())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert held == [0, 1, 1, 1]
    assert provider.snapshots == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "separate", "snapshot"])
async def test_validation_is_never_in_a_solver_and_tests_never_in_a_validation_verifier(
    tmp_path: Path, verifiers: Verifiers, mode: str
) -> None:
    provider = RoundsProvider()
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=2, mode=mode), TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    validators = [sandbox for sandbox in provider.sandboxes if sandbox.request.snapshot]
    for sandbox in provider.sandboxes:
        staged = {target for target in sandbox.uploads if target.startswith("/opt/ale/")}
        if sandbox.request.role is not SandboxRole.VERIFIER:
            # No solver holds validation/ or what it wrote.
            assert "/opt/ale/validation" not in staged
            assert "/logs/verifier/feedback.md" not in sandbox.files or (
                mode == "shared" and sandbox is provider.sandboxes[-1]
            )
        elif "/opt/ale/validation" in staged:
            assert "/opt/ale/tests" not in staged
    assert validators


@pytest.mark.asyncio
async def test_the_trace_records_each_rounds_save_validation_and_boot(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    result = await _run(tmp_path, RoundsProvider(), _task(tmp_path, rounds=1), TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    messages = [record.get("message") for record in _trace(result)]
    for said in (
        "a validation round saved the solver",
        "a validation round scored",
        "a validation round booted the next solver",
    ):
        assert said in messages
    phases = [timing.phase for timing in result.record.phases]
    assert phases[:6] == ["provision", "setup", "agent", "validate", "provision", "agent"]
    # The save is the solver's, as the end of its turn.
    saves = [
        record
        for record in _trace(result)
        if record.get("message")
        in {"the turn ended; a validation round begins", "a validation round saved the solver"}
    ]
    assert saves and {record["phase"] for record in saves} == {"agent"}


@pytest.mark.asyncio
async def test_the_log_mirror_follows_the_solver_across_boots(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    from ale.core.config import LoggingPolicy

    task = _task(tmp_path, rounds=2)
    lock = make_lock()
    result = await run_episode(
        task,
        EpisodeProtocol(TurnsHarness(), validation_rounds=2),
        provider_registry(RoundsProvider()),
        run_dir=tmp_path / "runs",
        logging_policy=LoggingPolicy(native_logs="debug"),
        provenance=ProvenanceInputs(
            source=lock.task.source,
            agent=lock.agent,
            gateway=lock.gateway,
            config_hash=lock.config_hash,
            framework=lock.framework,
        ),
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    mirrored = result.run_dir / "logs/turns/.turns/session.jsonl"
    assert len(mirrored.read_text().splitlines()) == 3


@pytest.mark.asyncio
async def test_separate_validation_restores_the_rounds_artifacts_into_a_fresh_verifier(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    provider, harness = RoundsProvider(), TurnsHarness()
    task = _task(tmp_path, rounds=1, mode="separate")
    result = await _run(tmp_path, provider, task, harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    # The round's verifier saw turn 1's work, the final one turn 2's.
    assert [item.reward for item in result.record.validation_rounds] == [0.33]
    assert result.verdict.rewards == {"reward": 0.67}
    validator, final = provider.booted(SandboxRole.VERIFIER)
    assert validator.snapshot is None and final.snapshot is None
    assert (validator.resources.cpus, validator.retention) == (4, "destroy")
    solvers = provider.booted(SandboxRole.SOLVER)
    assert [request.planned_saves for request in solvers] == [1, 0]
    assert solvers[0].snapshot_verifier is None
    # The round's copy of the artifacts is gone with its round.
    assert not (result.run_dir / "validation/1/artifacts").exists()
    assert provider.snapshots == {}


@pytest.mark.asyncio
async def test_snapshot_validation_boots_from_the_rounds_snapshot_with_the_verifiers_size(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    provider = RoundsProvider()
    task = _task(tmp_path, rounds=1, mode="snapshot")
    result = await _run(tmp_path, provider, task, TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    validator, final = provider.booted(SandboxRole.VERIFIER)
    _, second = provider.booted(SandboxRole.SOLVER)
    assert validator.snapshot == second.snapshot is not None
    assert final.snapshot not in (None, validator.snapshot)
    assert validator.resources.cpus == final.resources.cpus == 4
    assert provider.snapshots == {}


@pytest.mark.asyncio
async def test_retention_concerns_only_the_final_solver_and_verifier(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    provider = RoundsProvider()
    task = _task(tmp_path, rounds=2, mode="separate")
    result = await _run(
        tmp_path, provider, task, TurnsHarness(), solver="snapshot", verifier="keep"
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    outcomes = [(item.roles, item.requested, item.outcome) for item in result.record.sandboxes]
    # Two round verifiers and two round snapshots go; the last solver's disk and the final
    # verifier are kept.
    assert outcomes.count((("verifier",), "destroy", "destroyed")) == 2
    assert outcomes.count((("solver",), "destroy", "destroyed")) == 2
    assert (("solver",), "snapshot", "snapshotted") in outcomes
    assert (("verifier",), "keep", "retained") in outcomes
    assert len(provider.snapshots) == 1


# --- without validation/, tests/ validates ---


@pytest.mark.asyncio
async def test_without_validation_tests_validates_each_round_as_a_copy_of_it_would(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    harness = TurnsHarness()
    task = _task(tmp_path, rounds=2, validation_dir=False)
    result = await _run(tmp_path, RoundsProvider(), task, harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure

    # tests/ scored the state after turns 1 and 2 in the rounds' verifiers, and the final
    # state once, which alone is the Episode's reward.
    assert [stage for stage, _ in verifiers.runs] == ["/opt/ale/tests"] * 3
    rounds = result.record.validation_rounds
    assert [(item.index, item.status, item.reward) for item in rounds] == [
        (1, Status.COMPLETED, 0.33),
        (2, Status.COMPLETED, 0.67),
    ]
    assert result.verdict.rewards == {"reward": 1.0}
    # A round has no trajectory yet; the final run of tests/ has the converted one.
    assert ["ALE_TRAJECTORY_PATH" in env for env in verifiers.envs] == [False, False, True]
    # Each round's feedback reaches the agent and is recorded; the final run's reaches no one.
    assert [item.feedback and item.feedback.path for item in rounds] == [
        "validation/1/feedback.md",
        "validation/2/feedback.md",
    ]
    assert not (result.run_dir / "feedback.md").exists()

    first, second, third = harness.prompts
    preamble = first.removeprefix(task.spec.instruction)
    for said in (
        "on the test set that scores your final state and you receive the result",
        "at most 2 times",
        "scored once more, on the same test set",
    ):
        assert said in preamble
    # Nothing the framework says names a validation set the Task does not have.
    assert not any("validat" in text.lower() for text in (preamble, second, third))
    lines = second.splitlines()
    assert lines[:5] == [
        "Round 1 of 2: reward 0.33.",
        "",
        "Feedback from the test set:",
        "<feedback>",
        "hidden test detail",
    ]
    assert lines[-1].startswith("Rounds left: 1.")
    assert lines[-1].endswith("your work is scored again.")
    assert third.splitlines()[0] == "Round 2 of 2: reward 0.67."
    assert third.endswith("your final state is scored once more, on the same test set.")

    # The RunLock says the agent's rounds were the test set's.
    assert result.lock is not None
    assert (result.lock.validation_rounds, result.lock.validation_stage) == (2, "tests")
    assert json.loads((result.run_dir / "lock.json").read_text())["validation_stage"] == "tests"


@pytest.mark.asyncio
@pytest.mark.parametrize(("rounds", "validation"), [(1, True), (0, True), (0, False)])
async def test_a_lock_records_no_stage_unless_tests_validated_rounds(
    tmp_path: Path, verifiers: Verifiers, rounds: int, validation: bool
) -> None:
    task = _task(tmp_path, rounds=rounds, validation_dir=validation)
    result = await _run(tmp_path, RoundsProvider(), task, TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.lock is not None and result.lock.validation_stage == "validation"
    assert "validation_stage" not in json.loads((result.run_dir / "lock.json").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "separate", "snapshot"])
async def test_tests_is_in_no_solver_while_the_agent_works_when_it_validates(
    tmp_path: Path, verifiers: Verifiers, mode: str
) -> None:
    """Each round's verifier holds tests/; no turn's solver holds it, or what it wrote."""
    seen: list[set[str]] = []

    class Watching(TurnsHarness):
        async def _turn(self, prompt: str, sandbox: Any, *args: Any) -> AgentRun:
            staged = {target for target in sandbox.uploads if target.startswith("/opt/ale/")}
            seen.append(staged | ({"/logs/verifier/feedback.md"} & set(sandbox.files)))
            return await super()._turn(prompt, sandbox, *args)

    provider = RoundsProvider()
    task = _task(tmp_path, rounds=2, mode=mode, validation_dir=False)
    result = await _run(tmp_path, provider, task, Watching())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert len(seen) == 3
    assert not any(
        {"/opt/ale/tests", "/opt/ale/validation", "/logs/verifier/feedback.md"} & staged
        for staged in seen
    )
    # Nor did any solver hold it before its save: only the last shared one, after the last
    # turn, for the final verification.
    last = provider.sandboxes[-1] if mode == "shared" else None
    for sandbox in provider.sandboxes:
        if sandbox.request.role is not SandboxRole.VERIFIER and sandbox is not last:
            assert "/opt/ale/tests" not in sandbox.uploads
    validators = [
        sandbox
        for sandbox in provider.sandboxes
        if sandbox.request.role is SandboxRole.VERIFIER and "/opt/ale/tests" in sandbox.uploads
    ]
    assert len(validators) == (2 if mode == "shared" else 3)
    assert not any("/opt/ale/validation" in sandbox.uploads for sandbox in provider.sandboxes)


# --- what is refused before anything starts ---


def test_rounds_need_a_harness_that_resumes(tmp_path: Path) -> None:
    task = _task(tmp_path, rounds=2)
    assert validation_rounds(task, TurnsHarness(), None) == 2
    # The Run's override replaces the Task's count.
    assert validation_rounds(task, TurnsHarness(), 0) == 0
    assert validation_rounds(task, TurnsHarness(), 5) == 5
    # The oracle and nop never loop.
    assert validation_rounds(task, OracleHarness(), 3) == 0
    assert validation_rounds(task, NopHarness(), 3) == 0
    for harness in (ScriptedPolicyHarness(), _NoResume()):
        with pytest.raises(AgentUnsupportedError, match="native resume"):
            validation_rounds(task, harness, None)  # type: ignore[arg-type]
    assert ClaudeCodeHarness().resume_support is ResumeSupport.NATIVE
    validation_rounds(task, ClaudeCodeHarness(), None)

    # A Task without validation/ loops on tests/.
    plain = load_tasks(scaffold_task(tmp_path / "plain"))[0]
    assert validation_rounds(plain, TurnsHarness(), None) == 0
    assert validation_rounds(plain, TurnsHarness(), 1) == 1


class _NoResume(TurnsHarness):
    name = "no-resume"
    resume_support = ResumeSupport.NONE


@pytest.mark.asyncio
async def test_the_protocol_refuses_rounds_it_cannot_run_before_provisioning(
    tmp_path: Path,
) -> None:
    provider = RoundsProvider()
    task = _task(tmp_path, rounds=1)
    result = await _run(tmp_path, provider, task, _NoResume())
    assert result.verdict.failure is not None
    assert result.verdict.failure.error_class == "AgentUnsupportedError"
    assert "native resume" in result.verdict.failure.message
    assert provider.requests == []


def test_the_task_format_holds_validation_rounds(tmp_path: Path) -> None:
    root = scaffold_task(tmp_path / "task")
    edit_task_config(root, {"agent": {"validation_rounds": 2}})
    # Without validation/, tests/ validates.
    assert load_tasks(root)[0].validation_stage is Stage.TESTS
    (root / "validation").mkdir()
    with pytest.raises(TaskDefinitionError, match=r"validation/test\.sh"):
        load_tasks(root)
    (root / "validation/test.sh").write_text("#!/usr/bin/env bash\n")
    task = load_tasks(root)[0]
    assert task.spec.validation_rounds == 2
    assert task.validation_stage is Stage.VALIDATION
    # A stage of the Task's own, which lint accepts as it accepts tests/.
    assert not [item for item in lint_repository(root) if "validation" in str(item.path)]
    # The count is the Task's: it moves its identity, unlike the Run's override.
    other = load_tasks(_with_rounds(root, 3))[0]
    assert other.spec.spec_hash != task.spec.spec_hash
    for value, problem in ((-1, "greater than or equal to 0"), (True, "valid integer")):
        with pytest.raises(ValidationError, match=problem):
            TaskConfig.model_validate(
                {
                    "schema_version": "0.2",
                    "task": {"name": "a/b"},
                    "agent": {"validation_rounds": value},
                }
            )


def _with_rounds(root: Path, rounds: int) -> Path:
    edit_task_config(root, {"agent": {"validation_rounds": rounds}})
    return root


def test_the_runs_override_is_a_non_negative_count_in_its_configuration_hash() -> None:
    assert RunConfig().validation_rounds is None
    assert RunConfig(validation_rounds=0).config_hash != RunConfig().config_hash
    assert RunConfig(validation_rounds=2).reuse_hash != RunConfig().reuse_hash
    for value in (-1, "2", 1.5):
        with pytest.raises(ValidationError):
            RunConfig.model_validate({"validation_rounds": value})


def test_a_kept_solver_cannot_validate(tmp_path: Path) -> None:
    spec = _task(tmp_path, rounds=1).spec
    with pytest.raises(ConfigError, match="cannot be 'keep'"):
        check_retention(spec, SandboxRetentionConfig(solver="keep"), validates=True)
    check_retention(spec, SandboxRetentionConfig(solver="snapshot"), validates=True)
    check_retention(spec, SandboxRetentionConfig(solver="keep"))


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "saves"), [("shared", 3), ("separate", 3), ("snapshot", 4)])
async def test_a_validating_solver_needs_snapshots_on_a_leaf_that_serves_its_verifier(
    tmp_path: Path, mode: str, saves: int
) -> None:
    """Selection asks for one save in a row for each round, and one at the end only for a
    snapshot verifier: a shared solver is never saved at its end."""
    task = _task(tmp_path, rounds=3, mode=mode)
    plain, validating = RoundsProvider(), RoundsProvider()
    await provider_registry(plain, only=True).select([_task(tmp_path / "plain", mode=mode)])
    task.validations = 3
    await provider_registry(validating, only=True).select([task])
    # Asked last with what the verifier that boots from its snapshot needs too.
    *_, unvalidated = [needs for role, needs in plain.asked if role is not SandboxRole.VERIFIER]
    *_, needs = [needs for role, needs in validating.asked if role is not SandboxRole.VERIFIER]
    others = [needs for role, needs in validating.asked if role is SandboxRole.VERIFIER]
    assert unvalidated.snapshot is (mode == "snapshot")
    assert needs.snapshot and needs.saves == saves
    if mode == "shared":
        # The round's verifier boots from the snapshot, on its leaf: one choice for both.
        assert others == [] and needs.verifier is not None
        assert needs.verifier.resources == task.spec.resources.model_copy(update={"sudo": False})
    elif mode == "separate":
        assert needs.verifier is None and others
    else:
        assert needs.verifier == unvalidated.verifier is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "saves"), [("shared", [2, 1, 0]), ("snapshot", [3, 2, 1]), ("separate", [2, 1, 0])]
)
async def test_each_solver_asks_for_the_saves_still_to_come(
    tmp_path: Path, verifiers: Verifiers, mode: str, saves: list[int]
) -> None:
    provider = RoundsProvider()
    task = _task(tmp_path, rounds=2, mode=mode)
    result = await _run(tmp_path, provider, task, TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    solvers = [item for item in provider.requests if item.role is not SandboxRole.VERIFIER]
    assert [item.needs.saves for item in solvers] == saves


@pytest.mark.asyncio
async def test_a_solver_rounds_save_asks_for_a_leaf_that_saves_that_many_in_a_row() -> None:
    from ale.core.errors import ProviderCapabilityError
    from ale.core.taskspec import NetworkPolicy, Resources

    request = SandboxRequest(
        episode_id="episode",
        prepared_image=IMAGE,
        planned_saves=3,
        resources=Resources(),
        network=NetworkPolicy(),
    )
    assert request.retention == "destroy" and request.saves == 3
    assert request.needs.snapshot and request.needs.saves == 3
    # Kept as a snapshot at its end too: one more.
    assert request.model_copy(update={"retention": "snapshot"}).needs.saves == 4
    with pytest.raises(ProviderCapabilityError, match="saves and boots no snapshots"):
        await MemoryProvider("docker:runc").accepts(request)
    await RoundsProvider().accepts(request)
    short = RoundsProvider()
    short.max_saves = 2
    with pytest.raises(
        ProviderCapabilityError, match="saves 3 snapshots in a row, more than its 2"
    ):
        await short.accepts(request)


@pytest.mark.asyncio
async def test_selection_passes_over_a_provider_that_cannot_save_every_round(
    tmp_path: Path,
) -> None:
    """Firecracker stacks a layer for each save, eight at most: a Task with more rounds goes
    to the next Provider, or is refused before anything starts."""
    from ale.core.errors import ProviderCapabilityError
    from ale.run.providers.firecracker import FirecrackerProvider

    firecracker = FirecrackerProvider(snapshot_dir=tmp_path / "snapshots")
    for rounds, chosen in ((8, "firecracker"), (9, "docker:runc")):
        task = _task(tmp_path / str(rounds), rounds=rounds)
        task.validations = rounds
        registry = provider_registry(firecracker, RoundsProvider(), only=True)
        await registry.select([task])
        assert task.provider == chosen
    task = _task(tmp_path / "alone", rounds=9)
    task.validations = 9
    with pytest.raises(
        ProviderCapabilityError, match="saves 9 snapshots in a row, more than its 8"
    ):
        await provider_registry(firecracker, only=True).select([task])


# --- records ---


def test_a_round_is_scored_exactly_when_it_completed() -> None:
    from datetime import UTC, datetime

    from ale.core.result import FailureInfo, ValidationRound

    now = datetime.now(UTC)
    ValidationRound(index=1, status=Status.COMPLETED, reward=0.5, started_at=now, finished_at=now)
    failure = FailureInfo(error_type="TaskError", message="verify failed", phase="validate")
    ValidationRound(
        index=1, status=Status.TASK_ERROR, failure=failure, started_at=now, finished_at=now
    )
    for fields in (
        {"status": Status.COMPLETED},
        {"status": Status.COMPLETED, "reward": 0.5, "failure": failure},
        {"status": Status.TASK_ERROR},
        {"status": Status.TASK_ERROR, "failure": failure, "reward": 0.0},
    ):
        with pytest.raises(ValidationError):
            ValidationRound(index=1, started_at=now, finished_at=now, **fields)  # type: ignore[arg-type]
    record = ResultRecord(
        episode_id="e",
        status=Status.COMPLETED,
        rewards={"reward": 1.0},
        started_at=now,
        finished_at=now,
        validation_rounds=(
            ValidationRound(
                index=1, status=Status.COMPLETED, reward=0.5, started_at=now, finished_at=now
            ),
        ),
    )
    assert ResultRecord.model_validate_json(record.model_dump_json()) == record


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("feedback", "problem"),
    [
        (b"x" * (64 * 1024 + 1), "more than the 65536"),
        (b"\xff\xfe", "is not UTF-8"),
    ],
)
async def test_feedback_that_is_too_large_or_not_text_fails_its_round(
    tmp_path: Path,
    verifiers: Verifiers,
    monkeypatch: pytest.MonkeyPatch,
    feedback: bytes,
    problem: str,
) -> None:
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        result = await exec_(self, argv, **options)
        if list(argv) == ["bash", "test.sh"] and options.get("cwd") == "/opt/ale/validation":
            self.files["/logs/verifier/feedback.md"] = feedback
        return result

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    result = await _run(tmp_path, RoundsProvider(), _task(tmp_path, rounds=1), TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    (round_,) = result.record.validation_rounds
    assert round_.status is Status.TASK_ERROR
    assert round_.failure is not None and problem in round_.failure.message


# --- calibration ---


@pytest.mark.asyncio
async def test_calibration_runs_validation_once_on_a_snapshot_of_the_final_state(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    provider = RoundsProvider()
    task = _task(tmp_path, rounds=2)
    lock = make_lock()
    result = await run_episode(
        task,
        EpisodeProtocol(OracleHarness(), check_validation=True),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
        provenance=ProvenanceInputs(
            source=lock.task.source,
            agent=lock.agent,
            gateway=lock.gateway,
            config_hash=lock.config_hash,
            framework=lock.framework,
        ),
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    # The oracle ran once; validation/ scored its state from a snapshot, and tests/ the
    # same state in a solver booted from it.
    (round_,) = result.record.validation_rounds
    assert round_.status is Status.COMPLETED
    first, booted = provider.booted(SandboxRole.SHARED)
    (validator,) = provider.booted(SandboxRole.VERIFIER)
    assert booted.snapshot == validator.snapshot is not None
    assert (first.planned_saves, booted.planned_saves) == (1, 0)
    assert [stage for stage, _ in verifiers.runs] == ["/opt/ale/validation", "/opt/ale/tests"]
    assert verifiers.runs[1][1] == provider.sandboxes[-1].sandbox_id
    assert provider.snapshots == {}
    assert result.lock is not None and result.lock.validation_rounds == 0


@pytest.mark.asyncio
async def test_calibration_checks_tests_where_it_validates_the_agents_rounds(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    provider = RoundsProvider()
    task = _task(tmp_path, rounds=2, validation_dir=False)
    lock = make_lock()
    result = await run_episode(
        task,
        EpisodeProtocol(OracleHarness(), check_validation=True),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
        provenance=ProvenanceInputs(
            source=lock.task.source,
            agent=lock.agent,
            gateway=lock.gateway,
            config_hash=lock.config_hash,
            framework=lock.framework,
        ),
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    # tests/ scored the oracle's state from a snapshot, as a round does, then the same state
    # in a solver booted from it.
    (round_,) = result.record.validation_rounds
    assert round_.status is Status.COMPLETED
    assert [stage for stage, _ in verifiers.runs] == ["/opt/ale/tests", "/opt/ale/tests"]
    (validator,) = provider.booted(SandboxRole.VERIFIER)
    assert validator.snapshot is not None
    # The oracle receives no rounds, and its RunLock still says tests/ validated.
    assert result.lock is not None
    assert (result.lock.validation_rounds, result.lock.validation_stage) == (0, "tests")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rounds", "validation", "stage"),
    [(0, True, "validation"), (2, False, "tests"), (0, False, None)],
)
async def test_calibrate_checks_the_stage_that_validates_where_there_is_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rounds: int,
    validation: bool,
    stage: str | None,
) -> None:
    """validation/ always, and else tests/ only for a Task whose agent receives rounds."""
    from datetime import UTC, datetime
    from importlib import import_module
    from unittest.mock import AsyncMock

    from ale.core.result import ValidationRound
    from ale.core.verdict import Verdict
    from ale.run.episode import EpisodeResult
    from ale.run.providers import ProviderRegistry
    from ale.run.recording import atomic_write_json

    task_cli = import_module("ale.run.cli.tasks")
    root = _task(tmp_path, rounds=rounds, validation_dir=validation).folder.root
    lock = make_lock()
    checked: list[int] = []

    async def prepare(tasks, _providers, _continued=None):  # type: ignore[no-untyped-def]
        for item in tasks:
            item.prepared_image = IMAGE
        return ()

    async def execute(task, protocol, _providers, **kwargs):  # type: ignore[no-untyped-def]
        checked.append(task.validations)
        directory = kwargs["run_dir"] / kwargs["episode_id"]
        now = datetime.now(UTC)
        scored = ValidationRound(
            index=1, status=Status.COMPLETED, reward=0.5, started_at=now, finished_at=now
        )
        record = ResultRecord(
            episode_id=kwargs["episode_id"],
            status=Status.COMPLETED,
            rewards={"reward": 1.0},
            started_at=now,
            finished_at=now,
            validation_rounds=(scored,) if protocol.check_validation else (),
        )
        atomic_write_json(directory / "result.json", record)
        atomic_write_json(directory / "lock.json", lock)
        verdict = Verdict.completed({"reward": 1.0})
        return EpisodeResult(kwargs["episode_id"], verdict, directory, 0, record, lock)

    monkeypatch.setattr(ProviderRegistry, "preflight", AsyncMock())
    monkeypatch.setattr(task_cli, "_prepare_images", prepare)
    monkeypatch.setattr(task_cli, "run_episode", execute)
    code = await task_cli._calibrate(str(root), RunConfig(), tmp_path / "runs")
    assert checked == [int(stage is not None)]
    output = capsys.readouterr().out
    if stage is None:
        assert code == 0 and "/:" not in output
    else:
        # The case expects reward 1 of the stage that validates too, which gave 0.5.
        assert code == 2
        assert f"solution: reward=1.0; {stage}/: reward is 0.5, expected 1" in output


@pytest.mark.asyncio
async def test_a_calibration_case_that_ends_early_gets_no_validation_check(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    class Fails(OracleHarness):
        async def launch(self, *args: Any, **options: Any) -> AgentRun:
            raise AgentError("the solution failed")

    result = await run_episode(
        _task(tmp_path, rounds=2),
        EpisodeProtocol(Fails(), check_validation=True),
        provider_registry(RoundsProvider()),
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.status is Status.AGENT_ERROR
    assert result.record.validation_rounds == ()
    assert [stage for stage, _ in verifiers.runs] == ["/opt/ale/tests"]


def test_a_case_may_expect_other_scores_of_validation(tmp_path: Path) -> None:
    from ale.core.taskspec import CalibrationCase

    case = CalibrationCase.model_validate(
        {"name": "solution", "expect": {"reward": 1}, "expect_validation": {"reward": [0.5, 1]}}
    )
    assert case.validation_expect == {"reward": (0.5, 1.0)}
    assert case.admits("reward", 0.6, validation=True)
    assert not case.admits("reward", 0.6)
    assert CalibrationCase(name="x", expect={"reward": 0}).validation_expect == {"reward": 0}
    with pytest.raises(ValidationError, match="must include reward"):
        CalibrationCase.model_validate(
            {"name": "x", "expect": {"reward": 0}, "expect_validation": {"tflops": 1}}
        )
    root = scaffold_task(tmp_path / "task")
    edit_task_config(
        root,
        {
            "calibration": [
                {"name": "solution", "expect": {"reward": 1}, "expect_validation": {"reward": 1}}
            ]
        },
    )
    with pytest.raises(TaskDefinitionError, match=r"expect_validation .* needs validation/"):
        load_tasks(root)


@pytest.mark.asyncio
async def test_calibrate_checks_validation_of_every_case_against_its_expectation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime
    from importlib import import_module
    from unittest.mock import AsyncMock

    import jsonschema

    from ale.core.result import ValidationRound
    from ale.core.verdict import Verdict
    from ale.run.episode import EpisodeResult
    from ale.run.providers import ProviderRegistry
    from ale.run.recording import atomic_write_json

    task_cli = import_module("ale.run.cli.tasks")
    root = _task(tmp_path, rounds=0).folder.root
    edit_task_config(
        root,
        {
            "calibration": [
                {"name": "solution", "expect": {"reward": 1}, "expect_validation": {"reward": 0.5}},
                {"name": "partial", "expect": {"reward": 0.5}},
            ]
        },
    )
    (root / "calibration/partial").mkdir(parents=True)
    (root / "calibration/partial/solve.sh").write_text("true\n")
    # validation/ gives each case's state this reward, tests/ the case's own.
    validation = {"solution": 0.5, "partial": 0.75}
    tests = {"solution": 1.0, "partial": 0.5}
    lock = make_lock()
    checked: list[bool] = []

    async def prepare(tasks, _providers, _continued=None):  # type: ignore[no-untyped-def]
        for item in tasks:
            item.prepared_image = IMAGE
        return ()

    async def execute(task, protocol, _providers, **kwargs):  # type: ignore[no-untyped-def]
        case = getattr(protocol.harness, "case", None)
        checked.append(protocol.check_validation and task.validates)
        directory = kwargs["run_dir"] / kwargs["episode_id"]
        now = datetime.now(UTC)
        reward = validation.get(case, 0.0)
        atomic_write_json(directory / "validation/1/verification.json", {"reward": reward})
        rewards = {"reward": tests.get(case, 0.0)}
        record = ResultRecord(
            episode_id=kwargs["episode_id"],
            status=Status.COMPLETED,
            rewards=rewards,
            started_at=now,
            finished_at=now,
            validation_rounds=(
                ValidationRound(
                    index=1, status=Status.COMPLETED, reward=reward, started_at=now, finished_at=now
                ),
            ),
        )
        atomic_write_json(directory / "result.json", record)
        atomic_write_json(directory / "lock.json", lock)
        (directory / "verification.json").write_text(json.dumps(rewards))
        verdict = Verdict.completed(rewards)
        return EpisodeResult(kwargs["episode_id"], verdict, directory, 0, record, lock)

    monkeypatch.setattr(ProviderRegistry, "preflight", AsyncMock())
    monkeypatch.setattr(task_cli, "_prepare_images", prepare)
    monkeypatch.setattr(task_cli, "run_episode", execute)
    code = await task_cli._calibrate(str(root), RunConfig(), tmp_path / "runs")
    assert code == 2
    assert all(checked)
    (path,) = (tmp_path / "runs").glob("calibrate-*/calibration.json")
    observation = json.loads(path.read_text())
    schema = Path(__file__).resolve().parents[1] / "fixtures/calibration-observation-v1.schema.json"
    jsonschema.validate(observation, json.loads(schema.read_text()))
    cases = {case["name"]: case for case in observation["tasks"][0]["cases"]}
    solution, partial = cases["solution"], cases["partial"]
    # expect_validation replaces expect for validation/ alone.
    assert solution["validation"] == {
        "expect": {"reward": 0.5},
        "observed": {"reward": 0.5},
        "failures": [],
        "passed": True,
    }
    assert solution["passed"] is True
    # expect applies to both verifiers otherwise: validation/ gave 0.75, not 0.5.
    assert partial["failures"] == []
    assert [notice["code"] for notice in partial["validation"]["failures"]] == ["unexpected_value"]
    assert partial["passed"] is False
    output = capsys.readouterr().out
    assert "partial: reward=0.5; validation/: reward is 0.75, expected 0.5" in output


# --- what each turn starts from ---


@pytest.mark.asyncio
async def test_each_turn_stages_its_mcp_servers_from_what_was_declared(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A booted solver's MCP command is made executable again, as the first turn's was."""
    from ale.core.harness import EffectiveAgentResources, ResolvedMcpServer
    from ale.core.taskspec import StdioMcpServer

    files = tmp_path / "mcp"
    files.mkdir()
    (files / "server.sh").write_text("#!/bin/sh\n")
    resources = EffectiveAgentResources(
        mcp_servers=(
            ResolvedMcpServer(
                name="local",
                server=StdioMcpServer(name="local", transport="stdio", command="{mcp}/server.sh"),
                staged_files=files,
                source_layers=("task",),
                declared_sources=("tools/local.toml",),
                digest="sha256:" + "0" * 64,
                reportable=True,
            ),
        )
    )

    class WithMcp(TurnsHarness):
        def validate_resources(self, resources: EffectiveAgentResources) -> None:
            pass

    executable: dict[str, set[str]] = {}
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        held = executable.setdefault(self.sandbox_id, set())
        if list(argv[:3]) == ["chmod", "--", "u+x"]:
            held.add(argv[3])
        if list(argv[:2]) == ["test", "-x"]:
            return ExecResult(exit_code=0 if argv[2] in held else 1)
        return await exec_(self, argv, **options)

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    harness = WithMcp()
    result = await _run(
        tmp_path,
        RoundsProvider(),
        _task(tmp_path, rounds=2),
        harness,
        episode={"agent_resources": resources},
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert len(harness.prompts) == 3
    command = "/home/user/.ale-mcp/local/server.sh"
    assert [command in held for held in executable.values() if held] == [True] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [AgentError("its CLI is gone"), TaskError("not a directory")])
async def test_a_later_turn_that_cannot_start_in_what_the_agent_left_is_its_stop(
    tmp_path: Path, verifiers: Verifiers, error: Exception
) -> None:
    """Preparing a later turn checks what the agent left: that failing stops the agent, whose
    state tests/ still scores, rather than failing the Task."""

    class Breaks(TurnsHarness):
        installs = 0

        async def install(self, sandbox: Any) -> str:
            self.installs += 1
            if self.installs == 2:
                raise error
            return await super().install(sandbox)

    harness = Breaks()
    result = await _run(tmp_path, RoundsProvider(), _task(tmp_path, rounds=2), harness)
    assert result.verdict.status is Status.AGENT_ERROR
    assert len(harness.prompts) == 1
    # The agent was never given round 1's prompt, so its session lacks none.
    trajectory = json.loads((result.run_dir / "trajectory.json").read_text())
    assert not (trajectory.get("extra") or {}).get("ale", {}).get("parse_issues")
    assert [item.index for item in result.record.validation_rounds] == [1]
    assert result.verdict.rewards == {"reward": 0.33}
    assert verifiers.runs[-1][0] == "/opt/ale/tests"


@pytest.mark.asyncio
async def test_a_turn_that_ended_after_a_gateway_limit_is_the_last(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """It exited normally, but the Gateway had refused one of its calls: a budget stop."""
    from ale.run.gateway.session import LimitReached, SessionRegistry

    registry = SessionRegistry()

    class Limited(TurnsHarness):
        async def _turn(self, prompt: str, sandbox: Any, session: Any, timeout: float) -> Any:
            opened = registry.resolve(session.token)
            assert opened is not None
            opened.refusal = LimitReached("max_model_calls", 1, 2)
            return await super()._turn(prompt, sandbox, session, timeout)

    harness = Limited()
    result = await _run(
        tmp_path,
        RoundsProvider(),
        _task(tmp_path, rounds=2),
        harness,
        episode={"session_registry": registry},
    )
    assert result.verdict.status is Status.BUDGET_EXCEEDED, result.verdict.failure
    assert len(harness.prompts) == 1
    assert result.record.validation_rounds == ()


@pytest.mark.asyncio
async def test_the_agents_session_is_open_only_while_its_turns_run(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Between turns and after the last, the Gateway refuses the agent's token, and the
    credential files its Harness declares are gone from what a round saves."""
    from ale.run.gateway.session import SessionRegistry

    registry = SessionRegistry()
    tokens: list[str] = []
    open_in_turns: list[bool] = []
    credentials = "/home/user/.turns/credentials.json"

    class Credentialed(TurnsHarness):
        credentials = (".turns/credentials.json",)

        async def _turn(self, prompt: str, sandbox: Any, session: Any, timeout: float) -> Any:
            tokens.append(session.token)
            open_in_turns.append(registry.resolve(session.token) is not None)
            await sandbox.write_file(credentials, session.token.encode())
            return await super()._turn(prompt, sandbox, session, timeout)

    seen: list[tuple[bool, bool]] = []
    verifiers.seen = lambda sandbox: seen.append(
        (registry.resolve(tokens[-1]) is None, credentials not in sandbox.files)
    )
    provider = RoundsProvider()
    result = await _run(
        tmp_path,
        provider,
        _task(tmp_path, rounds=2),
        Credentialed(),
        episode={"session_registry": registry},
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert open_in_turns == [True, True, True]
    # Refused while each round's validation/ ran, without the file the turn wrote.
    assert seen == [(True, True), (True, True)]


@pytest.mark.asyncio
async def test_at_most_one_rounds_snapshot_exists_while_validation_runs(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """The previous round's snapshot goes once the next save succeeds."""
    provider = RoundsProvider()
    held: list[int] = []
    verifiers.seen = lambda sandbox: held.append(len(provider.snapshots))
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=3), TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert held == [1, 1, 1]
    assert provider.snapshots == {}


@pytest.mark.asyncio
async def test_an_older_rounds_snapshot_never_costs_the_final_one_its_retention(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under snapshot verification the final solver is saved too, after the rounds' snapshots,
    and finalization goes on past one that cannot be destroyed."""
    provider = RoundsProvider()
    destroy = provider.destroy_retained

    async def busy(handle: str) -> None:
        if handle == provider.saved[0]:
            raise ProviderStartError("busy")
        await destroy(handle)

    monkeypatch.setattr(provider, "destroy_retained", busy)
    task = _task(tmp_path, rounds=2, mode="snapshot")
    result = await _run(tmp_path, provider, task, TurnsHarness(), solver="snapshot")
    assert result.verdict.failure is not None and result.verdict.failure.phase == "teardown"
    outcomes = {item.handle: item.outcome for item in result.record.sandboxes if item.handle}
    assert outcomes[provider.saved[0]] == "not-destroyed"
    (kept,) = [handle for handle, outcome in outcomes.items() if outcome == "snapshotted"]
    assert kept == provider.saved[-1] and kept in provider.snapshots


@pytest.mark.asyncio
async def test_a_rounds_verifier_that_cannot_be_destroyed_ends_the_episode(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As the verifier of tests/ does, and it is never recorded as destroyed."""
    destroy_ = MemorySandbox.destroy

    async def destroy(self: MemorySandbox) -> None:
        if self.request.role is SandboxRole.VERIFIER:
            raise ProviderStartError("stuck")
        await destroy_(self)

    monkeypatch.setattr(MemorySandbox, "destroy", destroy)
    harness = TurnsHarness()
    result = await _run(tmp_path, RoundsProvider(), _task(tmp_path, rounds=2), harness)
    assert result.verdict.status is Status.ENV_ERROR
    assert len(harness.prompts) == 1
    verifiers_ = [item for item in result.record.sandboxes if item.roles == ("verifier",)]
    assert [item.outcome for item in verifiers_] == ["not-destroyed"]
    assert verifiers_[0].reason is not None and "stuck" in verifiers_[0].reason
    # It scored before its verifier was left: the round is recorded.
    assert [(item.status, item.reward) for item in result.record.validation_rounds] == [
        (Status.COMPLETED, 0.33)
    ]


@pytest.mark.asyncio
async def test_each_round_records_the_allocation_of_the_solver_it_booted(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    provider = RoundsProvider()
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=2), TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    solvers = [item for item in provider.sandboxes if item.request.role is SandboxRole.SHARED]
    assert [item.solver_allocation for item in result.record.validation_rounds] == [
        solver.allocation for solver in solvers[1:]
    ]
    lock = json.loads((result.run_dir / "lock.json").read_text())
    assert lock["resources"]["effective"]["provider"] == solvers[0].allocation.provider


@pytest.mark.parametrize("mode", ["shared", "separate", "snapshot"])
def test_no_setting_keeps_a_solver_that_runs_on_a_rounds_snapshot(
    tmp_path: Path, mode: str
) -> None:
    """Whichever setting would keep it: the solver's own, or, under shared verification,
    the verifier's, which then applies to the one Sandbox."""
    spec = _task(tmp_path, rounds=1, mode=mode).spec
    with pytest.raises(ConfigError, match="cannot be 'keep'"):
        check_retention(spec, SandboxRetentionConfig(solver="keep"), validates=True)
    keeps_the_verifier = SandboxRetentionConfig(verifier="keep", solver="snapshot")
    check_retention(spec, keeps_the_verifier, validates=True)
    kept = SandboxRetentionConfig(verifier="keep")
    if mode == "shared":
        with pytest.raises(ConfigError, match=r"sandbox_retention\.verifier"):
            check_retention(spec, kept, validates=True)
    elif mode == "separate":
        check_retention(spec, kept, validates=True)
    # Without validation/ in its episodes, nothing boots from a round's snapshot.
    if mode != "snapshot":
        check_retention(spec, kept, validates=False)


# --- what each round is given and records ---


@pytest.mark.asyncio
async def test_feedback_holding_a_judges_token_fails_and_other_credentials_are_redacted(
    tmp_path: Path,
) -> None:
    protocol = EpisodeProtocol(TurnsHarness())
    sandbox = await RoundsProvider().create(
        SandboxRequest(
            episode_id="e1",
            prepared_image=IMAGE,
            resources=Resources(cpus=1, memory_mb=1024),
            network=NetworkPolicy(),
        )
    )
    assert isinstance(sandbox, MemorySandbox)
    ctx = SimpleNamespace(
        spec=SimpleNamespace(os=OperatingSystem.LINUX),
        verification_secrets=("judge-token",),
        secrets=("agent-token", "judge-token"),
    )
    sandbox.files["/logs/verifier/feedback.md"] = b"the Judge said judge-token"
    with pytest.raises(VerifierOutputError, match="contains a configured credential"):
        await protocol._read_feedback(ctx, sandbox, tmp_path)  # type: ignore[arg-type]
    sandbox.files["/logs/verifier/feedback.md"] = b"you leaked agent-token"
    shown = await protocol._read_feedback(ctx, sandbox, tmp_path)  # type: ignore[arg-type]
    assert shown is not None and "agent-token" not in shown
    assert "agent-token" not in (tmp_path / "feedback.md").read_text()


def test_a_rounds_prompt_the_session_lacks_is_a_parse_issue() -> None:
    from ale.run.protocol.episode_protocol import _with_validation_rounds

    builder = TrajectoryBuilder(
        trajectory_id="t", session_id="s", agent=AtifAgent(name="turns", version="1")
    )
    for message in ("the instruction", "round one", "answer"):
        builder.add(source="user" if message != "answer" else "agent", message=message)
    marked = _with_validation_rounds(builder.build(), {1: "round one", 2: "round two"})
    rounds = [(step.extra or {}).get("ale", {}).get("validation_round") for step in marked.steps]
    assert rounds == [None, 1, None]
    issues = (marked.extra or {})["ale"]["parse_issues"]
    assert [issue["reason"] for issue in issues] == ["validation_round_not_found"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["validation", "tests"])
async def test_validation_gets_no_trajectory_and_its_judges_are_recorded_as_validate(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """A trajectory in the episode's directory is never staged for a round, of validation/
    or of tests/, only for the final run of tests/, and a Judge a round called is in the
    RunLock with its placement."""
    import dataclasses

    from tests.unit.test_verification_recording import invocation

    staged: list[tuple[bool, bool]] = []
    judged = json.dumps({"invocation": dataclasses.asdict(invocation())}) + "\n"
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        if list(argv) == ["bash", "test.sh"] and options.get("cwd") == f"/opt/ale/{stage}":
            round_ = self.request.role is SandboxRole.VERIFIER
            staged.append((round_, f"/opt/ale/{stage}/trajectory.json" in self.files))
            if round_:
                self.files[f"/opt/ale/{stage}/llm-judge.jsonl"] = judged.encode()
        return await exec_(self, argv, **options)

    class Converted(TurnsHarness):
        async def _turn(self, prompt: str, sandbox: Any, session: Any, timeout: float) -> Any:
            # As if a trajectory were converted already.
            (tmp_path / "runs" / session.episode_id / "trajectory.json").write_text("{}")
            return await super()._turn(prompt, sandbox, session, timeout)

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    task = _task(tmp_path, rounds=1, validation_dir=stage == "validation")
    result = await _run(tmp_path, RoundsProvider(), task, Converted())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert staged == ([(True, False)] if stage == "validation" else [(True, False), (False, True)])
    assert result.lock is not None
    assert [judge.placement for judge in result.lock.judges] == ["validate"]


@pytest.mark.asyncio
async def test_validation_places_the_rounds_of_tests_where_there_is_no_validation_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without validation/, [validation] places the verifier of each round of tests/: here a
    separate one in its own working directory, while tests/ scores the final state shared."""
    runs: list[tuple[SandboxRole, str | None, list[str]]] = []
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        if list(argv[:1]) == ["bash"] and str(argv[-1]).endswith("test.sh"):
            runs.append((self.request.role, options.get("cwd"), list(argv)))
            self.files["/logs/verifier/reward.json"] = b'{"reward": 0.5}'
            return ExecResult(exit_code=0)
        return await exec_(self, argv, **options)

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    provider = RoundsProvider()
    task = _task(
        tmp_path,
        rounds=1,
        validation_dir=False,
        validation={"environment_mode": "separate", "environment": {"cpus": 3, "workdir": "grade"}},
    )
    result = await _run(tmp_path, provider, task, TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert [(item.index, item.reward) for item in result.record.validation_rounds] == [(1, 0.5)]
    (validator,) = provider.booted(SandboxRole.VERIFIER)
    assert (validator.snapshot, validator.resources.cpus) == (None, 3)
    assert runs == [
        (SandboxRole.VERIFIER, "/home/user/grade", ["bash", "/opt/ale/tests/test.sh"]),
        (SandboxRole.SHARED, "/opt/ale/tests", ["bash", "test.sh"]),
    ]


@pytest.mark.asyncio
async def test_a_turn_that_reports_no_native_session_is_the_last(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    class Sessionless(TurnsHarness):
        async def _turn(self, *args: Any) -> AgentRun:
            run = await super()._turn(*args)
            return run.model_copy(update={"continuation": None})

    harness = Sessionless()
    result = await _run(tmp_path, RoundsProvider(), _task(tmp_path, rounds=2), harness)
    assert result.verdict.failure is not None
    assert result.verdict.failure.error_class == "NativeContinuationError"
    assert len(harness.prompts) == 1 and result.record.validation_rounds == ()
    assert result.verdict.rewards == {"reward": 0.33}


@pytest.mark.asyncio
async def test_a_turn_that_leaves_no_time_is_the_last(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]

    class Slow(TurnsHarness):
        async def _turn(self, *args: Any) -> AgentRun:
            clock[0] += 600
            return await super()._turn(*args)

    monkeypatch.setattr(
        "ale.run.protocol.episode_protocol.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    harness = Slow()
    result = await _run(tmp_path, RoundsProvider(), _task(tmp_path, rounds=2), harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert len(harness.prompts) == 1 and result.record.validation_rounds == ()


@pytest.mark.asyncio
async def test_validation_rounds_need_an_agent(tmp_path: Path) -> None:
    result = await run_episode(
        _task(tmp_path, rounds=1),
        EpisodeProtocol(TurnsHarness(), agent_enabled=False, validation_rounds=1),
        provider_registry(RoundsProvider()),
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.failure is not None
    assert "validation rounds need an agent" in result.verdict.failure.message


@pytest.mark.asyncio
async def test_the_verifiers_variables_reach_validation(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    given: list[str | None] = []
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        if list(argv) == ["bash", "test.sh"] and options.get("cwd") == "/opt/ale/validation":
            given.append((options.get("env") or {}).get("ROUND_FLAG"))
        return await exec_(self, argv, **options)

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    task = _task(
        tmp_path,
        rounds=1,
        verifier={"environment_mode": "shared", "env": {"ROUND_FLAG": "on"}},
    )
    resolve_task_variables([task], oracle=False, protected={})
    result = await _run(tmp_path, RoundsProvider(), task, TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert given == ["on"]


# --- what reaches the agent between its turns ---


@pytest.mark.asyncio
async def test_the_judges_sessions_are_open_only_while_a_verifier_runs(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Judge's token that reaches the agent between its turns is refused in the next."""
    from ale.core.config import VerificationConfig
    from ale.run.gateway.session import GatewaySession, SessionRegistry

    registry = SessionRegistry()

    class Gateway:
        def open_session(self, session: GatewaySession, trace: Any = None) -> GatewaySession:
            return registry.open(session)

        def close_session(self, session: GatewaySession) -> None:
            registry.close(session)

    judge = GatewaySession(episode_id="e", model="judge", purpose="judge")
    monkeypatch.setattr(
        "ale.run.episode.open_judge_sessions", lambda *_, **__: {"llm": registry.open(judge)}
    )
    in_turns: list[bool] = []

    class Watching(TurnsHarness):
        async def _turn(self, *args: Any) -> AgentRun:
            in_turns.append(registry.resolve(judge.token) is not None)
            return await super()._turn(*args)

    in_validation: list[bool] = []
    verifiers.seen = lambda sandbox: in_validation.append(registry.resolve(judge.token) is not None)
    result = await _run(
        tmp_path,
        RoundsProvider(),
        _task(tmp_path, rounds=2),
        Watching(),
        episode={"gateway": Gateway(), "verification_config": VerificationConfig()},
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert in_turns == [False, False, False]
    assert in_validation == [True, True]


@pytest.mark.asyncio
async def test_every_harness_credential_files_go_before_a_rounds_save(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Whichever Harness runs: another's, from an earlier episode, go as well."""
    left = "/home/user/.codex-ale/auth.json"

    class Leaving(TurnsHarness):
        async def _turn(self, prompt: str, sandbox: Any, session: Any, timeout: float) -> Any:
            await sandbox.write_file(left, b"token")
            return await super()._turn(prompt, sandbox, session, timeout)

    seen: list[bool] = []
    verifiers.seen = lambda sandbox: seen.append(left in sandbox.files)
    result = await _run(tmp_path, RoundsProvider(), _task(tmp_path, rounds=1), Leaving())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert seen == [False]


@pytest.mark.asyncio
async def test_a_booted_solver_is_sealed_before_its_cli_is_checked(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """So what the agent set to start at boot gets no open egress for longer than the boot."""
    open_at_install: list[bool] = []

    class Watching(TurnsHarness):
        async def install(self, sandbox: Any) -> str:
            open_at_install.append(sandbox.egress_open)
            return await super().install(sandbox)

    result = await _run(tmp_path, RoundsProvider(), _task(tmp_path, rounds=2), Watching())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    # The first turn's CLI may still need the network to be put in place.
    assert open_at_install == [True, False, False]


@pytest.mark.asyncio
async def test_the_next_solver_waits_for_room_it_lost_while_validation_ran(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another Run took its GPUs, say: it tries again under a bound of the framework's."""
    from ale.core.errors import ProviderCapacityError

    monkeypatch.setattr("ale.run.protocol.episode_protocol.BOOT_RETRY_SEC", 0.0)
    provider, harness = RoundsProvider(), TurnsHarness()
    provider.refusal = ProviderCapacityError
    refused = iter([True, True])
    provider.fail = lambda request: (
        bool(request.snapshot)
        and request.role is not (SandboxRole.VERIFIER)
        and next(refused, False)
    )
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=1), harness)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert len(harness.prompts) == 2
    monkeypatch.setattr("ale.run.protocol.episode_protocol.BOOT_WAIT_SEC", 0.0)
    provider.fail = lambda request: (
        bool(request.snapshot) and request.role is not (SandboxRole.VERIFIER)
    )
    result = await _run(tmp_path / "again", provider, _task(tmp_path / "again", rounds=1), harness)
    assert result.verdict.failure is not None and "found no room" in result.verdict.failure.message


@pytest.mark.asyncio
async def test_a_rounds_failed_save_keeps_the_runs_policy(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a saved round's solver is the lease's to destroy: one whose save failed is a
    retention that failed."""

    async def snapshot(self: MemorySandbox, **_: object) -> None:
        raise ProviderStartError("the disk could not be saved")

    monkeypatch.setattr(MemorySandbox, "snapshot", snapshot)
    result = await _run(
        tmp_path, RoundsProvider(), _task(tmp_path, rounds=1), TurnsHarness(), solver="snapshot"
    )
    assert [(item.requested, item.outcome) for item in result.record.sandboxes] == [
        ("snapshot", "retention-failed")
    ]
