"""A solver whose Provider holds its Sandbox only until a deadline moves before it: the turn
goes on in a solver booted from its snapshot, in the same native session, with the agent's
time it has left, and it is all one episode."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.support import (
    ROUNDS_WORK,
    TURNS_LOG,
    TURNS_SESSION,
    MemorySandbox,
    RoundsProvider,
    ScriptedCliHarness,
    TurnsHarness,
    Verifiers,
    edit_task_config,
    provider_registry,
    rounds_task,
)
from tests.unit.test_runtime_contracts import DIGEST, make_lock

from orbit.core.config import RunTimeouts
from orbit.core.errors import AllocationEndedError, PhaseTimeoutError
from orbit.core.harness import AgentRun, HarnessSession
from orbit.core.sandbox import ExecResult, PreparedTaskImage, SandboxRequest, SandboxRole
from orbit.core.trajectory import AtifTrajectory
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.protocol import episode_protocol
from orbit.run.protocol.episode_protocol import MOVED_PROMPT, EpisodeProtocol
from orbit.run.provenance import ProvenanceInputs
from orbit.run.tasksets.manifest import ManifestTask

pytestmark = pytest.mark.unit

IMAGE = PreparedTaskImage(
    kind="container",
    source="external-ref",
    input_identity=DIGEST,
    runtime_ref="image@" + DIGEST,
    prepared_identity=DIGEST,
    resolved_reference="image@" + DIGEST,
)


#: What the last turn keeps of its Sandbox for a shared verifier, which this module's tests
#: of moves do without: its limit is minutes, their Sandboxes last seconds.
KEEPS = EpisodeProtocol._kept_for_verification


@pytest.fixture(autouse=True)
def margins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seconds where an episode has minutes."""
    monkeypatch.setattr(episode_protocol, "MOVE_MARGIN_SEC", 0.0)
    monkeypatch.setattr(episode_protocol, "MIN_TURN_SEC", 0.05)
    monkeypatch.setattr(EpisodeProtocol, "_kept_for_verification", lambda self, ctx: 0.0)


class DeadlineProvider(RoundsProvider):
    """Holds each Sandbox it starts for the next of ``held`` seconds, the last for every one
    after, as a scheduler holds an allocation for its wall time."""

    def __init__(self, *held: float) -> None:
        super().__init__()
        self.held = list(held)
        #: ``progress.json`` as each Sandbox booted from a snapshot was asked for.
        self.progress: list[dict[str, Any]] = []
        self.run_dir: Path | None = None

    async def create(self, request: SandboxRequest) -> Any:
        if request.snapshot is not None and self.run_dir is not None:
            for found in self.run_dir.rglob("progress.json"):
                self.progress.append(json.loads(found.read_text()))
        sandbox = await super().create(request)
        held = self.held.pop(0) if len(self.held) > 1 else self.held[0]
        sandbox.deadline = datetime.now(UTC) + timedelta(seconds=held)
        return sandbox


class WorkHarness(TurnsHarness):
    """Works ``works[n]`` seconds of the agent's time in its turn ``n``, writing a line of
    work as each turn ends; a run its limit cuts reports the session it left, unless
    ``reports`` is false, and the turn goes on when the session resumes."""

    def __init__(self, *works: float, reports: bool = True) -> None:
        super().__init__()
        self.works = list(works)
        self.done = 0.0
        self.reports = reports

    async def _turn(
        self, prompt: str, sandbox: Any, session: HarnessSession, timeout_sec: float
    ) -> AgentRun:
        self.prompts.append(prompt)
        self.timeouts.append(timeout_sec)
        self.sandboxes.append(sandbox.sandbox_id)
        left = self.works[0] - self.done
        spent = min(timeout_sec, left)
        await asyncio.sleep(spent)
        self.done += spent
        log = await _read(sandbox, TURNS_LOG)
        event = {"user": prompt, "agent": f"run {len(self.prompts)}"}
        await sandbox.write_file(TURNS_LOG, log + (json.dumps(event) + "\n").encode())
        if spent < left:
            raise PhaseTimeoutError(
                "agent", timeout_sec, native_session_id="session" if self.reports else None
            )
        self.works.pop(0)
        self.done = 0.0
        await sandbox.write_file(ROUNDS_WORK, await _read(sandbox, ROUNDS_WORK) + b"line\n")
        return AgentRun(exit_code=0, final_message="done", continuation=TURNS_SESSION)


async def _read(sandbox: Any, path: str) -> bytes:
    try:
        return await sandbox.read_file(path)
    except Exception:
        return b""


def _task(tmp_path: Path, rounds: int = 0, **options: Any) -> ManifestTask:
    task = rounds_task(tmp_path, rounds=rounds, **options)
    task.prepared_image = IMAGE
    return task


async def _run(
    tmp_path: Path,
    provider: RoundsProvider,
    task: ManifestTask,
    harness: TurnsHarness,
    timeouts: RunTimeouts | None = None,
) -> Any:
    lock = make_lock()
    rounds = task.spec.validation_rounds
    if isinstance(provider, DeadlineProvider):
        provider.run_dir = tmp_path / "runs"
    return await run_episode(
        task,
        EpisodeProtocol(harness, validation_rounds=rounds, timeouts=timeouts),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
        provenance=ProvenanceInputs(
            source=lock.task.source,
            agent=lock.agent,
            gateway=lock.gateway,
            config_hash=lock.config_hash,
            framework=lock.framework,
            validation_rounds=rounds,
        ),
    )


def _solvers(provider: RoundsProvider) -> list[Any]:
    return [box for box in provider.sandboxes if box.request.role is not SandboxRole.VERIFIER]


def _marked(result: Any, key: str) -> list[Any]:
    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    return [
        ((step.extra or {}).get("orbit") or {}).get(key)
        for step in trajectory.steps
        if step.source == "user"
    ]


def _moved(prompts: Sequence[str]) -> list[bool]:
    return [prompt.startswith(MOVED_PROMPT.partition("{minutes}")[0]) for prompt in prompts]


@pytest.mark.asyncio
async def test_a_turn_that_outlasts_its_sandbox_goes_on_in_one_episode(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Each Sandbox lasts 0.4 s of a turn of 1 s: the turn is cut twice, and goes on each
    time in a solver booted from the one before, resumed in the same session with what is
    left of the agent's time; tests/ scores the one line the turn wrote."""
    provider, harness = DeadlineProvider(0.4), WorkHarness(1.0)
    result = await _run(tmp_path, provider, _task(tmp_path), harness)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": round(1 / 3, 2)}
    assert _moved(harness.prompts) == [False, True, True]
    # Each run in a solver of its own, the next booted from a snapshot of the one before.
    solvers = _solvers(provider)
    assert harness.sandboxes == [box.sandbox_id for box in solvers[:3]]
    assert all(box.request.snapshot for box in solvers[1:])
    segments = result.record.segments
    assert [(item.began, item.sandbox_id) for item in segments] == [
        ("provisioned", solvers[0].sandbox_id),
        ("moved", solvers[1].sandbox_id),
        ("moved", solvers[2].sandbox_id),
    ]
    assert sum(item.agent_sec for item in segments) == pytest.approx(1.0, abs=0.15)
    # Each run had what its Sandbox had left, less than the agent's time.
    assert harness.timeouts == pytest.approx([0.4, 0.4, 0.4], abs=0.05)
    # One trajectory, the prompts that resumed it marked where each segment began.
    assert _marked(result, "moved") == [None, 2, 3]
    agent = [item.outcome for item in result.record.phases if item.phase == "agent"]
    assert agent == ["moved", "moved", "succeeded"]
    outcomes = [item.outcome for item in result.record.sandboxes]
    assert all(outcome == "destroyed" for outcome in outcomes), outcomes


@pytest.mark.asyncio
async def test_the_time_a_move_takes_is_not_the_agents(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """A boot that waits, as for a queue, spends none of the agent's time: the segments
    spend what the agent's runs did, and the record says so as it goes."""
    provider, harness = DeadlineProvider(0.3), WorkHarness(0.5)
    provider.create_delay = 0.4
    result = await _run(tmp_path, provider, _task(tmp_path), harness)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    first, second = result.record.segments[:2]
    assert first.ended_at is not None
    assert (second.started_at - first.ended_at).total_seconds() >= 0.4
    spent = sum(item.agent_sec for item in result.record.segments)
    assert spent == pytest.approx(0.5, abs=0.1)
    # As the moved solver boots: moving, in the session the cut run left.
    moving = provider.progress[0]
    assert moving["phase"] == "moving"
    assert moving["continuation"] == TURNS_SESSION.model_dump(mode="json")
    assert moving["segments"][0]["ended_at"] is not None
    progress = json.loads((result.run_dir / "progress.json").read_text())
    assert progress["agent_used_sec"] == pytest.approx(spent)
    assert progress["agent_sec"] == 600


@pytest.mark.asyncio
async def test_rounds_and_moves_share_the_turns_of_one_session(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """A move cuts the first turn, which ends in the moved solver; its round validates that
    and boots the next turn's solver, which knows of no move."""
    provider, harness = DeadlineProvider(0.3, 5.0), WorkHarness(0.5, 0.1)
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=1), harness)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert _moved(harness.prompts) == [False, True, False]
    assert harness.prompts[2].startswith("Your work was scored: reward")
    assert [(item.index, item.reward) for item in result.record.validation_rounds] == [
        (1, round(1 / 3, 2))
    ]
    assert [(item.began, item.round) for item in result.record.segments] == [
        ("provisioned", None),
        ("moved", None),
        ("round", 1),
    ]
    assert _marked(result, "moved") == [None, 2, None]
    assert _marked(result, "validation_round") == [None, None, 1]


@pytest.mark.asyncio
async def test_a_turn_with_no_time_left_in_its_sandbox_starts_in_the_next(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Too little is left of the first Sandbox to start a turn in: the solver moves first,
    and the agent receives its instruction, as it was to, where it lasts."""
    provider, harness = DeadlineProvider(0.01, 5.0), WorkHarness(0.1)
    result = await _run(tmp_path, provider, _task(tmp_path), harness)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert _moved(harness.prompts) == [False]
    assert harness.sandboxes == [_solvers(provider)[1].sandbox_id]
    assert [item.began for item in result.record.segments] == ["provisioned", "moved"]
    assert result.record.segments[0].agent_sec == 0


@pytest.mark.asyncio
async def test_sandboxes_too_brief_for_any_turn_end_the_episode(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """A solver that moves and still has no time to start a turn ends the episode, as its
    Provider's, rather than moving forever."""
    provider, harness = DeadlineProvider(0.01), WorkHarness(0.1)
    result = await _run(tmp_path, provider, _task(tmp_path), harness)

    assert result.verdict.status is not Status.COMPLETED
    assert result.verdict.failure is not None
    assert "too briefly to start a turn" in result.verdict.failure.message
    assert harness.prompts == []


@pytest.mark.asyncio
async def test_a_run_cut_with_no_session_to_resume_ends_the_episode_as_the_providers(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """A run that reports no session cannot go on, and the agent did not run out of its
    time: the episode ends with an error of the environment's, not the agent's, unscored."""
    provider, harness = DeadlineProvider(0.2, 5.0), WorkHarness(1.0, reports=False)
    result = await _run(tmp_path, provider, _task(tmp_path), harness)

    assert result.verdict.status is Status.ENV_ERROR, result.verdict.failure
    assert result.verdict.failure is not None
    assert "left no native session to resume" in result.verdict.failure.message
    assert result.verdict.rewards is None and len(harness.prompts) == 1
    assert [(item.began, item.agent_sec > 0) for item in result.record.segments] == [
        ("provisioned", True)
    ]


@pytest.mark.asyncio
async def test_a_shared_verifier_that_would_not_last_moves_its_solver_where_it_does(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """tests/ runs in the solver, so the solver moves first, to a Sandbox asked to last the
    verifier's limit; that move is no segment of the agent's work."""
    provider, harness = DeadlineProvider(0.3), WorkHarness(0.05)
    task = _task(tmp_path)
    result = await _run(tmp_path, provider, task, harness)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    tested = [sandbox for stage, sandbox in verifiers.runs if stage == "/opt/orbit/tests"]
    solvers = _solvers(provider)
    assert tested == [solvers[1].sandbox_id]
    assert solvers[1].request.duration_sec == task.spec.timeouts.verify
    assert [item.began for item in result.record.segments] == ["provisioned"]
    # As the solver moved for its verifier, the episode's progress said so.
    assert provider.progress[-1]["phase"] == "moving"


@pytest.mark.asyncio
async def test_the_last_turn_keeps_its_sandbox_for_a_shared_verifier(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tests/ runs in the solver, where the agent's processes still run: the last turn's
    runs end the verifier's limit before the Sandbox's deadline, so the run cut there moves,
    and tests/ runs where the turn ended, in no solver a move stopped them in."""
    monkeypatch.setattr(EpisodeProtocol, "_kept_for_verification", KEEPS)
    provider, harness = DeadlineProvider(0.8), WorkHarness(0.7)
    task = _task(tmp_path)
    result = await _run(tmp_path, provider, task, harness, RunTimeouts(verify=0.3))

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    # Each run had its Sandbox's 0.8 s less the 0.3 s kept, so the work took two of them.
    assert harness.timeouts[0] == pytest.approx(0.5, abs=0.05)
    assert [item.began for item in result.record.segments] == ["provisioned", "moved"]
    tested = [sandbox for stage, sandbox in verifiers.runs if stage == "/opt/orbit/tests"]
    solvers = _solvers(provider)
    assert len(solvers) == 2 and tested == [solvers[1].sandbox_id]
    assert solvers[1].request.duration_sec is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "rounds", "timeouts"),
    [("snapshot", 0, [0.8]), ("separate", 0, [0.8]), ("shared", 1, [0.8, 0.5])],
)
async def test_only_the_last_turn_before_tests_in_the_solver_keeps_its_sandbox(
    tmp_path: Path,
    verifiers: Verifiers,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    rounds: int,
    timeouts: list[float],
) -> None:
    """A verifier of its own boots from a save, and a turn a round follows is saved for the
    round, which stops every process anyway: neither keeps time for verification."""
    monkeypatch.setattr(EpisodeProtocol, "_kept_for_verification", KEEPS)
    provider, harness = DeadlineProvider(0.8), WorkHarness(*[0.05] * (rounds + 1))
    task = _task(tmp_path, rounds=rounds, mode=mode)
    result = await _run(tmp_path, provider, task, harness, RunTimeouts(verify=0.3))

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert harness.timeouts == pytest.approx(timeouts, abs=0.05)


@pytest.mark.asyncio
async def test_a_verifier_of_its_own_asks_to_last_its_time_limit(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Where its Provider holds Sandboxes only so long, a verifier starts where it lasts."""
    provider, harness = DeadlineProvider(5.0), WorkHarness(0.1)
    task = _task(tmp_path, mode="snapshot")
    edit_task_config(task.folder.root, {"verifier": {"timeout_sec": 120}})
    task.spec = task.spec.model_copy(
        update={"verify": task.spec.verify.model_copy(update={"timeout_sec": 120})}
    )
    result = await _run(tmp_path, provider, task, harness)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    verifier = next(box for box in provider.sandboxes if box.request.role is SandboxRole.VERIFIER)
    assert verifier.request.duration_sec == 120
    solver = _solvers(provider)[0]
    assert solver.request.duration_sec is None


@pytest.mark.asyncio
async def test_a_round_whose_verifier_lost_its_allocation_runs_again_on_its_snapshot(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Not the agent's doing, and no verifier changed the snapshot: the round is the second
    attempt's, scored, and the agent receives one result."""
    provider, harness = RoundsProvider(), WorkHarness(0.05, 0.05)
    lost: list[str] = []

    def seen(sandbox: MemorySandbox) -> None:
        # As a Provider explains a Sandbox it lost as validation/ runs.
        if not lost:
            lost.append(sandbox.sandbox_id)
            raise AllocationEndedError("the scheduler preempted the verifier's allocation")

    verifiers.seen = seen
    result = await _run(tmp_path, provider, _task(tmp_path, rounds=1), harness)

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    validated = [sandbox for stage, sandbox in verifiers.runs if stage == "/opt/orbit/validation"]
    assert len(validated) == 2 and validated[0] == lost[0] != validated[1]
    assert [(item.index, item.status, item.reward) for item in result.record.validation_rounds] == [
        (1, Status.COMPLETED, round(1 / 3, 2))
    ]
    assert harness.prompts[1].startswith("Your work was scored: reward 0.33.")


@pytest.mark.asyncio
async def test_a_cli_whose_run_its_sandboxs_deadline_cut_resumes_its_session(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the episode's own view of the Sandbox, which raises the timeout of a cut run
    itself: the CLI's session reaches the move, and the next run resumes it."""

    class CutCli(ScriptedCliHarness):
        async def prepare_command(  # type: ignore[no-untyped-def]
            self, prompt_path, sandbox, session, *, native_session_id
        ):
            from orbit.run.harnesses.base import CliCommand

            return CliCommand("cut-cli-run", {}, "scripted")

    runs: list[str] = []
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        command = " ".join(str(part) for part in argv)
        if "mkdir -p" in command and ".orbit-harness" in command:
            # The log files the CLI's shell would create.
            for match in re.finditer(r": > ('[^']*'|\S+)", command):
                self.files[shlex.split(match.group(1))[0]] = b""
            return ExecResult(exit_code=0)
        if "cut-cli-run" not in command:
            return await exec_(self, argv, **options)
        runs.append(self.sandbox_id)
        home = options["cwd"]
        self.files[f"{home}/.scripted-cli/scripted/run-{len(runs)}.md"] = b"run"
        if len(runs) == 1:
            return ExecResult(exit_code=None, timed_out=True)
        out = f"{home}/.orbit-harness/stdout.log"
        reply = json.dumps({"session_id": "scripted", "result": "done"}) + "\n"
        self.files[out] = self.files.get(out, b"") + reply.encode()
        return ExecResult(exit_code=0)

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    provider = DeadlineProvider(0.3, 5.0)
    result = await _run(tmp_path, provider, _task(tmp_path), CutCli())

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert runs == [box.sandbox_id for box in _solvers(provider)[:2]]
    assert [item.began for item in result.record.segments] == ["provisioned", "moved"]
    assert result.record.continuation is not None
    assert result.record.continuation.native_session_id == "scripted"


@pytest.mark.parametrize("mode", ["shared", "snapshot"])
def test_selection_asks_every_leaf_to_hold_a_verifier_for_its_limit(
    tmp_path: Path, mode: str
) -> None:
    """A shared verifier runs in the solver and a snapshot verifier on the leaf that saved
    the snapshot, so the solver's leaf must hold one that long; the Run's limit holds over
    the Task's."""
    from orbit.core.config import RunTimeouts
    from orbit.run.providers import _sandboxes

    task = _task(tmp_path, rounds=1, mode=mode)
    limit = task.spec.timeouts.verify
    planned = list(_sandboxes(task, snapshot=False, solver=True, timeouts=RunTimeouts()))
    assert planned[0][2].duration_sec == limit
    if mode == "snapshot":
        assert planned[1][2].duration_sec == limit
    shorter = list(_sandboxes(task, snapshot=False, solver=True, timeouts=RunTimeouts(verify=60)))
    assert shorter[0][2].duration_sec == 60
    unlimited = RunTimeouts(verify="unlimited")
    assert (
        next(_sandboxes(task, snapshot=False, solver=True, timeouts=unlimited))[2].duration_sec
        is None
    )
