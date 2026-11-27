"""An episode whose Provider holds no solver past the process that runs it, as a cluster's job
holds none past its wall time, is handed off at the move: the process ends without verifying,
and a later one, with a Provider and a registry of its own, continues the same episode from
the snapshot and the record it left, as one episode with one result."""

from __future__ import annotations

import asyncio
import json
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
    TurnsHarness,
    Verifiers,
    provider_registry,
    rounds_task,
)
from tests.unit.test_runtime_contracts import DIGEST, make_lock

from orbit.core.config import RunTimeouts
from orbit.core.episode import EpisodeContinuation
from orbit.core.errors import PhaseTimeoutError, SandboxHandOff
from orbit.core.harness import AgentRun, HarnessSession
from orbit.core.sandbox import PreparedTaskImage, SandboxRequest, SandboxRole
from orbit.core.trace import read_jsonl
from orbit.core.trajectory import AtifTrajectory
from orbit.core.verdict import Status
from orbit.run.episode import EpisodeResult, run_episode
from orbit.run.gateway.session import LimitReached, Limits, SessionRegistry
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

#: The agent's progress in its work, which it keeps in its Sandbox, as an agent keeps its files.
STATE = "/home/user/.turns/state.json"


@pytest.fixture(autouse=True)
def margins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seconds where an episode has minutes; its verifiers' limits, minutes, kept nowhere."""
    monkeypatch.setattr(episode_protocol, "MOVE_MARGIN_SEC", 0.0)
    monkeypatch.setattr(episode_protocol, "MIN_TURN_SEC", 0.05)
    monkeypatch.setattr(EpisodeProtocol, "_kept_for_verification", lambda self, ctx, box: 0.0)


class JobProvider(RoundsProvider):
    """Starts Sandboxes in one job, each held ``held`` seconds as the job's wall time holds it,
    and boots a solver from a snapshot ``boots`` times at most, where given: the next it was to
    boot it writes to ``store``, which outlives the job, and hands the episode off. Another
    job's instance boots a snapshot from there."""

    def __init__(
        self, job: str, store: dict[str, dict[str, bytes]], *, held: float, boots: int | None
    ) -> None:
        super().__init__()
        self.job = job
        self.store = store
        self.held = held
        self.boots = boots
        self.handed: list[str] = []

    async def create(self, request: SandboxRequest) -> MemorySandbox:
        handle = request.snapshot
        if handle is not None and handle not in self.snapshots:
            # Written out by an earlier job.
            self.snapshots[handle] = dict(self.store[handle])
        elif handle is not None and request.role is not SandboxRole.VERIFIER:
            if self.boots == 0:
                self.store[handle] = dict(self.snapshots[handle])
                self.handed.append(handle)
                raise SandboxHandOff(handle)
            if self.boots is not None:
                self.boots -= 1
        sandbox = await super().create(request)
        sandbox.sandbox_id = f"{self.job}-{len(self.sandboxes)}"
        sandbox.deadline = datetime.now(UTC) + timedelta(seconds=self.held)
        return sandbox

    async def destroy_retained(self, handle: str) -> None:
        await super().destroy_retained(handle)
        self.store.pop(handle, None)


class JobHarness(TurnsHarness):
    """Works ``works[n]`` seconds of the agent's time in its turn ``n``, keeping how far it got
    in the Sandbox, so that the instance of a later process goes on where it was cut; makes
    ``calls`` model calls at each run's start through ``sessions``, the Gateway's."""

    def __init__(
        self, *works: float, calls: int = 0, sessions: SessionRegistry | None = None
    ) -> None:
        super().__init__()
        self.works = works
        self.calls = calls
        self.sessions = sessions
        #: Each call it tried: whether the Gateway took it.
        self.taken: list[bool] = []

    async def _turn(
        self, prompt: str, sandbox: Any, session: HarnessSession, timeout_sec: float
    ) -> AgentRun:
        self.prompts.append(prompt)
        self.timeouts.append(timeout_sec)
        self.sandboxes.append(sandbox.sandbox_id)
        await self._call(session)
        state = json.loads((await _read(sandbox, STATE)) or b'{"turn": 0, "done": 0}')
        left = self.works[state["turn"]] - state["done"]
        spent = min(timeout_sec, left)
        await asyncio.sleep(spent)
        log = await _read(sandbox, TURNS_LOG)
        event = {"user": prompt, "agent": f"{sandbox.sandbox_id} run"}
        await sandbox.write_file(TURNS_LOG, log + (json.dumps(event) + "\n").encode())
        if spent < left:
            state["done"] += spent
            await sandbox.write_file(STATE, json.dumps(state).encode())
            raise PhaseTimeoutError("agent", timeout_sec, native_session_id="session")
        await sandbox.write_file(STATE, json.dumps({"turn": state["turn"] + 1, "done": 0}).encode())
        await sandbox.write_file(ROUNDS_WORK, await _read(sandbox, ROUNDS_WORK) + b"line\n")
        return AgentRun(exit_code=0, final_message="done", continuation=TURNS_SESSION)

    async def _call(self, session: HarnessSession) -> None:
        if self.sessions is None:
            return
        gateway = self.sessions.resolve(session.token)
        assert gateway is not None, "the agent's session is open while it runs"
        for _ in range(self.calls):
            try:
                reservation = await gateway.reserve()
            except LimitReached as limit:
                # As the Gateway refuses it, which a CLI reports as its failure.
                gateway.refusal = gateway.refusal or limit
                self.taken.append(False)
                return
            await gateway.commit(reservation, answered=True)
            self.taken.append(True)


async def _read(sandbox: Any, path: str) -> bytes:
    try:
        return await sandbox.read_file(path)
    except Exception:
        return b""


def _task(tmp_path: Path, rounds: int = 0) -> ManifestTask:
    task = rounds_task(tmp_path, rounds=rounds)
    task.prepared_image = IMAGE
    return task


async def _job(
    tmp_path: Path,
    task: ManifestTask,
    provider: JobProvider,
    harness: JobHarness,
    continuation: EpisodeContinuation | None = None,
    **options: Any,
) -> EpisodeResult | EpisodeContinuation:
    """The episode's part one job runs: all of it, or until it hands the episode off; its
    verifier's limit is a second, which every job holds."""
    lock = make_lock()
    rounds = task.spec.validation_rounds
    return await run_episode(
        task,
        EpisodeProtocol(
            harness,
            validation_rounds=rounds,
            timeouts=options.pop("timeouts", RunTimeouts(verify=1.0)),
        ),
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
        continuation=continuation,
        **options,
    )


def _marked(result: EpisodeResult, key: str) -> list[Any]:
    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    return [
        ((step.extra or {}).get("orbit") or {}).get(key)
        for step in trajectory.steps
        if step.source == "user"
    ]


def _moved(prompts: list[str]) -> list[bool]:
    return [prompt.startswith(MOVED_PROMPT) for prompt in prompts]


def _episode_dir(tmp_path: Path, record: EpisodeContinuation) -> Path:
    return tmp_path / "runs" / record.episode_id


@pytest.mark.asyncio
async def test_a_move_the_job_cannot_hold_hands_the_episode_off(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """The job's wall time cuts the turn and its Provider boots the moved solver in no later
    Sandbox of this job: the episode ends here unverified, with no result, and its snapshot
    goes on, held for the next job."""
    store: dict[str, dict[str, bytes]] = {}
    provider = JobProvider("job1", store, held=0.4, boots=0)
    harness = JobHarness(1.0)
    task = _task(tmp_path)
    record = await _job(tmp_path, task, provider, harness)

    assert isinstance(record, EpisodeContinuation)
    episode = _episode_dir(tmp_path, record)
    assert not (episode / "result.json").exists() and not (episode / "lock.json").exists()
    assert provider.handed == [record.snapshot.handle]
    # Not destroyed: held for the next job, as written out.
    assert record.snapshot.handle in store and record.snapshot.handle in provider.snapshots
    assert record.provider == provider.id
    assert [stage for stage, _ in verifiers.runs] == []
    assert record.turn.began == "moved" and record.turn.ran
    assert record.turn.continuation == TURNS_SESSION
    assert _moved([record.turn.prompt or ""]) == [True]
    assert record.agent_time.used == pytest.approx(0.4, abs=0.1)
    assert [item.began for item in record.segments] == ["provisioned"]
    assert [(item.phase, item.outcome) for item in record.phases if item.phase != "teardown"] == [
        ("provision", "succeeded"),
        ("setup", "succeeded"),
        ("agent", "moved"),
        ("provision", "moved"),
    ]
    # The native logs copied so far stay for the next job to extend.
    assert (episode / "logs" / "turns" / ".turns" / "session.jsonl").is_file()
    assert not (episode / "trajectory.json").exists()


@pytest.mark.asyncio
async def test_the_next_job_continues_the_same_episode(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """A new Provider, registry and Harness continue the cut turn with what it has left, in the
    same session, and the episode is one: its segments, phases, time and trajectory span both
    jobs."""
    store: dict[str, dict[str, bytes]] = {}
    task = _task(tmp_path)
    first, second = JobHarness(0.9), JobHarness(0.9)
    timeouts = RunTimeouts(agent=1.0, verify=1.0)
    record = await _job(
        tmp_path, task, JobProvider("job1", store, held=0.4, boots=0), first, timeouts=timeouts
    )
    assert isinstance(record, EpisodeContinuation)
    provider = JobProvider("job2", store, held=5.0, boots=None)
    result = await _job(tmp_path, task, provider, second, record, timeouts=timeouts)

    assert isinstance(result, EpisodeResult)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": round(1 / 3, 2)}
    assert result.episode_id == record.episode_id
    assert result.run_dir == _episode_dir(tmp_path, record)
    # The turn went on with what it had left of its time, which no new turn reset.
    assert _moved(first.prompts + second.prompts) == [False, True]
    assert second.timeouts == pytest.approx([1.0 - record.agent_time.used], abs=0.05)
    assert second.sandboxes == [provider.sandboxes[0].sandbox_id]
    assert provider.sandboxes[0].request.snapshot == record.snapshot.handle
    segments = result.record.segments
    assert [(item.began, item.booted_from) for item in segments] == [
        ("provisioned", None),
        ("moved", record.snapshot.handle),
    ]
    assert sum(item.agent_sec for item in segments) == pytest.approx(0.9, abs=0.15)
    # One trajectory, the prompt that resumed it marked where the moved segment began.
    trajectory = json.loads((result.run_dir / "trajectory.json").read_text())
    assert trajectory["trajectory_id"] == record.trajectory_id
    assert _marked(result, "moved") == [None, 2]
    phases = [(item.phase, item.outcome) for item in result.record.phases]
    assert [outcome for phase, outcome in phases if phase == "agent"] == ["moved", "succeeded"]
    assert [outcome for phase, outcome in phases if phase == "provision"] == [
        "succeeded",
        "moved",
        "succeeded",
    ]
    assert result.record.started_at == record.started_at
    # The snapshot went after the turn it held, as a move's does.
    assert store == {}
    assert all(item.outcome == "destroyed" for item in result.record.sandboxes)
    # Neither job reused the other's execution ids.
    trace = read_jsonl(result.run_dir / "trace.execution.jsonl").records
    started = [item["execution_id"] for item in trace if item.get("kind") == "command_started"]
    assert len(started) == len(set(started))
    assert result.lock is not None and result.lock.resources is not None


@pytest.mark.asyncio
async def test_a_hand_off_after_a_round_continues_the_rounds_where_they_were(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """The second turn, after round 1, is handed off: the next job finishes it and goes on with
    round 2, not round 1 again, and the agent receives two results in all, each saying which
    round it was and how many are left."""
    store: dict[str, dict[str, bytes]] = {}
    task = _task(tmp_path, rounds=2)
    first, second = JobHarness(0.05, 1.0, 0.05), JobHarness(0.05, 1.0, 0.05)
    # Round 1's verifier fits in the first job; the round's boot holds; the move's does not.
    timeouts = RunTimeouts(verify=0.2)
    job1 = JobProvider("job1", store, held=0.4, boots=1)
    record = await _job(tmp_path, task, job1, first, timeouts=timeouts)
    assert isinstance(record, EpisodeContinuation)
    assert [item.index for item in record.validation_rounds] == [1]
    result = await _job(
        tmp_path,
        task,
        JobProvider("job2", store, held=5.0, boots=None),
        second,
        record,
        timeouts=timeouts,
    )

    assert isinstance(result, EpisodeResult)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    rounds = result.record.validation_rounds
    assert [(item.index, item.reward) for item in rounds] == [
        (1, round(1 / 3, 2)),
        (2, round(2 / 3, 2)),
    ]
    prompts = first.prompts + second.prompts
    assert _moved(prompts) == [False, False, True, False]
    assert prompts[1].startswith("Validation round 1 of 2: reward 0.33.")
    assert "Validation rounds left: 1." in prompts[1]
    assert prompts[3].startswith("Validation round 2 of 2: reward 0.67.")
    assert "Validation rounds left: none." in prompts[3]
    assert [(item.began, item.round) for item in result.record.segments] == [
        ("provisioned", None),
        ("round", 1),
        ("moved", None),
        ("round", 2),
    ]
    assert _marked(result, "validation_round") == [None, 1, None, 2]
    assert _marked(result, "moved") == [None, None, 3, None]
    validated = [stage for stage, _ in verifiers.runs if stage == "/opt/orbit/validation"]
    assert len(validated) == 2


@pytest.mark.asyncio
async def test_a_round_whose_next_solver_the_job_cannot_hold_hands_off_that_turn(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Round 1 scored, but its next turn's solver boots only in the next job: that turn begins
    there, with the round's result and the time the agent has left."""
    store: dict[str, dict[str, bytes]] = {}
    task = _task(tmp_path, rounds=1)
    first, second = JobHarness(0.05, 0.05), JobHarness(0.05, 0.05)
    record = await _job(tmp_path, task, JobProvider("job1", store, held=5.0, boots=0), first)
    assert isinstance(record, EpisodeContinuation)
    assert record.turn.began == "round" and record.turn.round == 1
    assert record.turn.prompt is not None
    assert record.turn.prompt.startswith("Validation round 1 of 1: reward 0.33.")
    assert "Validation rounds left: none." in record.turn.prompt
    result = await _job(
        tmp_path, task, JobProvider("job2", store, held=5.0, boots=None), second, record
    )

    assert isinstance(result, EpisodeResult)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert second.prompts == [record.turn.prompt]
    assert [(item.index, item.reward) for item in result.record.validation_rounds] == [
        (1, round(1 / 3, 2))
    ]
    assert [(item.began, item.round) for item in result.record.segments] == [
        ("provisioned", None),
        ("round", 1),
    ]
    assert _marked(result, "validation_round") == [None, 1]


@pytest.mark.asyncio
async def test_a_move_hands_off_where_the_agents_time_is_unlimited(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Only the job's wall time cuts the run: it is handed off all the same, the record names
    no total, and the next job resumes the run with a prompt that names no time."""
    store: dict[str, dict[str, bytes]] = {}
    task = _task(tmp_path)
    timeouts = RunTimeouts(agent="unlimited", verify=1.0)
    first, second = JobHarness(0.6), JobHarness(0.6)
    record = await _job(
        tmp_path, task, JobProvider("job1", store, held=0.4, boots=0), first, timeouts=timeouts
    )
    assert isinstance(record, EpisodeContinuation)
    assert record.agent_time.total is None
    assert record.agent_time.used == pytest.approx(0.4, abs=0.1)
    assert record.turn.prompt is not None and "Time left" not in record.turn.prompt
    result = await _job(
        tmp_path, task, JobProvider("job2", store, held=5.0, boots=None), second, record,
        timeouts=timeouts,
    )  # fmt: skip

    assert isinstance(result, EpisodeResult)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert second.prompts == [record.turn.prompt]
    assert [item.began for item in result.record.segments] == ["provisioned", "moved"]


@pytest.mark.asyncio
async def test_unlimited_rounds_go_on_from_the_round_after_a_hand_off(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Unlimited rounds, the third turn's solver booting only in the next job: there the
    rounds go on from round 3 until the agent's time is up, and no prompt names a total."""
    store: dict[str, dict[str, bytes]] = {}
    task = _task(tmp_path, rounds=1)
    timeouts = RunTimeouts(agent=1.0, verify=1.0)
    first, second = JobHarness(0.05, 0.05, 0.05, 2.0), JobHarness(0.05, 0.05, 0.05, 2.0)

    async def job(
        name: str, harness: JobHarness, boots: int | None, record: Any = None
    ) -> EpisodeResult | EpisodeContinuation:
        lock = make_lock()
        return await run_episode(
            task,
            EpisodeProtocol(harness, validation_rounds="unlimited", timeouts=timeouts),
            provider_registry(JobProvider(name, store, held=5.0, boots=boots)),
            run_dir=tmp_path / "runs",
            provenance=ProvenanceInputs(
                source=lock.task.source,
                agent=lock.agent,
                gateway=lock.gateway,
                config_hash=lock.config_hash,
                framework=lock.framework,
                validation_rounds="unlimited",
            ),
            continuation=record,
        )

    record = await job("job1", first, boots=1)
    assert isinstance(record, EpisodeContinuation)
    assert record.turn.began == "round" and record.turn.round == 2
    assert record.turn.prompt is not None
    assert record.turn.prompt.startswith("Validation round 2: reward 0.67.")
    assert "rounds left" not in record.turn.prompt
    result = await job("job2", second, boots=None, record=record)

    assert isinstance(result, EpisodeResult)
    # The fourth turn ran out of the agent's time, which makes it the last.
    assert result.verdict.status is Status.TIMEOUT, result.verdict.failure
    assert [item.index for item in result.record.validation_rounds] == [1, 2, 3]
    assert second.prompts[0] == record.turn.prompt
    assert second.prompts[1].startswith("Validation round 3: reward 1.00.")
    assert [(item.began, item.round) for item in result.record.segments] == [
        ("provisioned", None),
        ("round", 1),
        ("round", 2),
        ("round", 3),
    ]
    assert _marked(result, "validation_round") == [None, 1, 2, 3]


@pytest.mark.asyncio
async def test_a_turns_gateway_limits_hold_across_jobs(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Two of the episode's three calls are spent before the hand-off: the next job's Gateway
    takes one more and refuses the next, as one Gateway would have in one process."""
    store: dict[str, dict[str, bytes]] = {}
    task = _task(tmp_path)
    limits = Limits(max_model_calls=3)
    sessions = [SessionRegistry(), SessionRegistry()]
    first = JobHarness(1.0, calls=2, sessions=sessions[0])
    second = JobHarness(1.0, calls=2, sessions=sessions[1])
    record = await _job(
        tmp_path,
        task,
        JobProvider("job1", store, held=0.4, boots=0),
        first,
        session_registry=sessions[0],
        limits=limits,
    )
    assert isinstance(record, EpisodeContinuation)
    assert record.gateway is not None and record.gateway.usage.model_calls == 2
    result = await _job(
        tmp_path,
        task,
        JobProvider("job2", store, held=5.0, boots=None),
        second,
        record,
        session_registry=sessions[1],
        limits=limits,
    )

    assert isinstance(result, EpisodeResult)
    assert first.taken == [True, True]
    assert second.taken == [True, False]
    # The next job's token, not the first's, carried the turn on.
    first_token = next(iter(record.redacted))
    assert sessions[1].resolve(first_token) is None


@pytest.mark.asyncio
async def test_an_episode_goes_on_through_a_chain_of_jobs(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """The second job's wall time cuts the turn too: a third job finishes it, and each
    snapshot goes once the next is saved."""
    store: dict[str, dict[str, bytes]] = {}
    task = _task(tmp_path)
    harnesses = [JobHarness(1.0), JobHarness(1.0), JobHarness(1.0)]
    first = await _job(
        tmp_path,
        task,
        JobProvider("job1", store, held=0.4, boots=0),
        harnesses[0],
        session_registry=SessionRegistry(),
    )
    assert isinstance(first, EpisodeContinuation)
    second = await _job(
        tmp_path,
        task,
        JobProvider("job2", store, held=0.4, boots=0),
        harnesses[1],
        first,
        session_registry=SessionRegistry(),
    )
    assert isinstance(second, EpisodeContinuation)
    # The first job's snapshot went once the second's was saved; the second job's token is
    # kept out of the records with the first's.
    assert list(store) == [second.snapshot.handle] != [first.snapshot.handle]
    assert second.started_at == first.started_at and second.turn.segment == 3
    assert set(first.redacted) < set(second.redacted)
    result = await _job(
        tmp_path, task, JobProvider("job3", store, held=5.0, boots=None), harnesses[2], second
    )

    assert isinstance(result, EpisodeResult)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    prompts = [prompt for harness in harnesses for prompt in harness.prompts]
    assert _moved(prompts) == [False, True, True]
    assert [item.began for item in result.record.segments] == ["provisioned", "moved", "moved"]
    assert sum(item.agent_sec for item in result.record.segments) == pytest.approx(1.0, abs=0.2)
    assert _marked(result, "moved") == [None, 2, 3]
    assert store == {}
    agent = [item.outcome for item in result.record.phases if item.phase == "agent"]
    assert agent == ["moved", "moved", "succeeded"]


@pytest.mark.asyncio
async def test_the_continuation_record_round_trips_through_json(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Written as JSON by one job and read by the next, it is what it was; its secrets are
    kept out of its representation."""
    task = _task(tmp_path, rounds=2)
    record = await _job(
        tmp_path,
        task,
        JobProvider("job1", {}, held=5.0, boots=1),
        JobHarness(0.05, 0.05, 0.05),
        session_registry=SessionRegistry(),
    )
    assert isinstance(record, EpisodeContinuation)
    assert record.validation_rounds and record.validation_prompts and record.redacted
    text = json.dumps(record.model_dump(mode="json"))
    assert EpisodeContinuation.model_validate(json.loads(text)) == record
    assert all(secret not in repr(record) for secret in record.redacted)


@pytest.mark.asyncio
async def test_a_hand_off_where_no_agent_works_ends_the_episode(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """A verifier its Provider cannot hold goes on in no later process: the episode ends with
    the Provider's error, unscored, and the snapshot goes."""
    store: dict[str, dict[str, bytes]] = {}
    task = _task(tmp_path, rounds=1)
    provider = JobProvider("job1", store, held=5.0, boots=None)
    create = provider.create

    async def hold_no_verifier(request: SandboxRequest) -> MemorySandbox:
        if request.role is SandboxRole.VERIFIER and request.snapshot is not None:
            raise SandboxHandOff(request.snapshot)
        return await create(request)

    provider.create = hold_no_verifier  # type: ignore[method-assign]
    result = await _job(tmp_path, task, provider, JobHarness(0.05, 0.05))

    assert isinstance(result, EpisodeResult)
    assert result.verdict.status is Status.ENV_ERROR
    assert result.verdict.failure is not None
    assert "only the agent's work goes on" in result.verdict.failure.message
    assert result.record.validation_rounds == ()
    assert provider.snapshots == {}


def _snapshot_task(tmp_path: Path, rounds: int = 0) -> ManifestTask:
    task = rounds_task(tmp_path, rounds=rounds, mode="snapshot")
    task.prepared_image = IMAGE
    return task


@pytest.mark.asyncio
async def test_a_verifier_the_job_would_not_last_runs_in_the_next_job(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """The agent worked until near the job's end, kept nothing for a snapshot verifier: the
    solver moves first, the next job boots it and verifies, and nothing of the agent's runs
    there."""
    store: dict[str, dict[str, bytes]] = {}
    task = _snapshot_task(tmp_path)
    first, second = JobHarness(0.3), JobHarness(0.3)
    timeouts = RunTimeouts(verify=0.2)
    job1 = JobProvider("job1", store, held=0.4, boots=0)
    record = await _job(tmp_path, task, job1, first, timeouts=timeouts)
    assert isinstance(record, EpisodeContinuation)
    assert record.turn.began == "verifying" and record.turn.round is None
    assert first.timeouts[0] == pytest.approx(0.4, abs=0.05)
    assert not [stage for stage, _ in verifiers.runs]
    job2 = JobProvider("job2", store, held=5.0, boots=None)
    result = await _job(tmp_path, task, job2, second, record, timeouts=timeouts)

    assert isinstance(result, EpisodeResult)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert second.prompts == []
    assert [item.began for item in result.record.segments] == ["provisioned"]
    tested = [box for stage, box in verifiers.runs if stage == "/opt/orbit/tests"]
    assert len(tested) == 1 and tested[0].startswith("job2-")


@pytest.mark.asyncio
async def test_a_round_the_job_would_not_last_validates_in_the_next_job(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Round 1's verifier would not last the first job: the next validates that round, gives
    the agent its result and goes on with the turn after it."""
    store: dict[str, dict[str, bytes]] = {}
    task = _snapshot_task(tmp_path, rounds=1)
    first, second = JobHarness(0.3, 0.05), JobHarness(0.3, 0.05)
    timeouts = RunTimeouts(verify=0.2)
    job1 = JobProvider("job1", store, held=0.4, boots=0)
    record = await _job(tmp_path, task, job1, first, timeouts=timeouts)
    assert isinstance(record, EpisodeContinuation)
    assert (record.turn.began, record.turn.round) == ("validating", 1)
    assert record.validation_rounds == ()
    job2 = JobProvider("job2", store, held=5.0, boots=None)
    result = await _job(tmp_path, task, job2, second, record, timeouts=timeouts)

    assert isinstance(result, EpisodeResult)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert [item.index for item in result.record.validation_rounds] == [1]
    assert len(second.prompts) == 1
    assert second.prompts[0].startswith("Validation round 1 of 1:")
    assert [(item.began, item.round) for item in result.record.segments] == [
        ("provisioned", None),
        ("round", 1),
    ]


@pytest.mark.asyncio
async def test_a_verifier_no_job_lasts_ends_the_episode(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """Handed off for its verifier, the episode fails rather than hands off again where the
    next job would not last that verifier either."""
    store: dict[str, dict[str, bytes]] = {}
    task = _snapshot_task(tmp_path)
    timeouts = RunTimeouts(verify=0.2)
    job1 = JobProvider("job1", store, held=0.4, boots=0)
    record = await _job(tmp_path, task, job1, JobHarness(0.3), timeouts=timeouts)
    assert isinstance(record, EpisodeContinuation)
    job2 = JobProvider("job2", store, held=0.1, boots=0)
    result = await _job(tmp_path, task, job2, JobHarness(0.3), record, timeouts=timeouts)

    assert isinstance(result, EpisodeResult)
    assert result.verdict.status is not Status.COMPLETED
    assert result.verdict.failure is not None
    assert "outlasts" in result.verdict.failure.message
