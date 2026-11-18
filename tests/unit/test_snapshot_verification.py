"""Verification in a new Sandbox booted from a snapshot of the solver's disk."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tests.support import MemoryProvider, MemorySandbox, edit_task_config, provider_registry
from tests.unit.test_runtime_contracts import DIGEST, make_lock

from ale.core.capability import CapabilityProfile
from ale.core.config import SandboxRetentionConfig
from ale.core.errors import (
    ConfigError,
    ProviderCapabilityError,
    ProviderStartError,
    RetentionFinalizationError,
)
from ale.core.harness import NativeContinuation
from ale.core.lock import ResumedFrom
from ale.core.sandbox import (
    ExecResult,
    PreparedTaskImage,
    ResolvedImage,
    SandboxRequest,
    SandboxRole,
)
from ale.core.taskspec import (
    ContainerFeature,
    NetworkMode,
    NetworkPolicy,
    Resources,
    VerificationMode,
)
from ale.core.verdict import Status
from ale.run.episode import _Artifacts, _Lease, check_retention, run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import (
    EpisodeProtocol,
    Resume,
    ReverificationProtocol,
    SolverSnapshot,
)
from ale.run.provenance import ProvenanceInputs
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import ManifestTask, load_tasks

pytestmark = pytest.mark.unit

#: What the agent leaves on its disk outside any declared artifact.
CHECKPOINT = "/home/user/checkpoints/step-1000.pt"
REWARD = "/logs/verifier/reward.json"
IMAGE = PreparedTaskImage(
    kind="container",
    source="external-ref",
    input_identity=DIGEST,
    runtime_ref="image@" + DIGEST,
    prepared_identity=DIGEST,
    resolved_reference="image@" + DIGEST,
)
#: The verifier's own size: larger than its solver's.
VERIFIER = {"cpus": 8, "memory_mb": 8192}


class Provider(MemoryProvider):
    """Saves snapshots; its solvers hold what an agent left, and its verifiers score
    whether they see it."""

    def __init__(self, name: str = "docker:runc", **options: Any) -> None:
        super().__init__(name, saves_snapshots=True, **options)
        self.fail: SandboxRole | None = None
        self.max_cpus: int | None = None

    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        return tuple(
            profile.model_copy(update={"max_cpus": self.max_cpus})
            for profile in await super().profiles()
        )

    async def create(self, request: SandboxRequest) -> MemorySandbox:
        if request.role is self.fail:
            self.requests.append(request)
            raise ProviderStartError(f"no room for the {request.role}")
        sandbox = await super().create(request)
        assert isinstance(sandbox, MemorySandbox)
        if request.role is SandboxRole.SOLVER and request.snapshot is None:
            sandbox.files[CHECKPOINT] = b"weights"
        return sandbox


@pytest.fixture(autouse=True)
def verifier_scores_the_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        if list(argv) == ["bash", "test.sh"]:
            self.files[REWARD] = json.dumps({"reward": int(CHECKPOINT in self.files)}).encode()
        return await exec_(self, argv, **options)

    monkeypatch.setattr(MemorySandbox, "exec", exec)


def _task(tmp_path: Path, environment: dict[str, Any] | None = VERIFIER) -> ManifestTask:
    root = scaffold_task(tmp_path / "task")
    verifier: dict[str, Any] = {"environment_mode": "snapshot"}
    if environment is not None:
        verifier["environment"] = environment
    edit_task_config(root, {"environment": {"cpus": 1, "memory_mb": 1024}, "verifier": verifier})
    task = load_tasks(root)[0]
    task.prepared_image = IMAGE
    return task


async def _run(tmp_path: Path, provider: Provider, task: ManifestTask, **retention: str) -> Any:
    lock = make_lock()
    return await run_episode(
        task,
        EpisodeProtocol(OracleHarness()),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
        sandbox_retention=SandboxRetentionConfig(**retention),  # type: ignore[arg-type]
        provenance=ProvenanceInputs(
            source=lock.task.source,
            agent=lock.agent,
            gateway=lock.gateway,
            config_hash=lock.config_hash,
            framework=lock.framework,
        ),
    )


@pytest.mark.asyncio
async def test_the_verifier_boots_from_the_solvers_snapshot_with_its_own_resources(
    tmp_path: Path,
) -> None:
    provider = Provider()
    result = await _run(tmp_path, provider, _task(tmp_path))

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    # It saw what the agent left outside any declared artifact.
    assert result.verdict.rewards == {"reward": 1.0}
    solver, verifier = provider.requests
    assert (solver.role, solver.snapshot, solver.resources.cpus) == (SandboxRole.SOLVER, None, 1)
    solver_sandbox = provider.sandboxes[0]
    assert verifier.role is SandboxRole.VERIFIER
    assert verifier.snapshot == f"memory:snapshot:{solver_sandbox.sandbox_id}"
    assert verifier.prepared_image == IMAGE
    assert verifier.user == solver.user
    assert (verifier.resources.cpus, verifier.resources.memory_mb) == (8, 8192)
    assert verifier.network.mode is NetworkMode.OPEN
    # Sanitized first: the snapshot names the agent's episode and holds no result.
    booted = provider.sandboxes[1]
    assert booted.files["/opt/ale/agent-episode"] == result.episode_id.encode()
    # Nothing it did reached the solver, which was gone before it started.
    assert REWARD not in solver_sandbox.files
    assert solver_sandbox.destroyed and booted.destroyed
    assert provider.snapshots == {}
    assert [(item.roles, item.outcome) for item in result.record.sandboxes] == [
        (("verifier",), "destroyed"),
        (("solver",), "destroyed"),
    ]
    lock = result.lock
    assert lock is not None and lock.verification.mode is VerificationMode.SNAPSHOT
    assert lock.verification.resources is not None
    effective = lock.verification.resources.effective
    assert (effective.cpus, effective.provider, effective.leaf) == (8, provider.id, provider.id)
    assert lock.verification.image is not None
    assert lock.verification.image.prepared_identity == IMAGE.prepared_identity
    assert lock.verification.image.provider == provider.id
    # The trace says when the snapshot was saved, for the solver, and when it went.
    assert [record["phase"] for record in _snapshot_records(result, verifier.snapshot)] == [
        "agent",
        "teardown",
    ]


def _snapshot_records(result: Any, handle: str | None) -> list[dict[str, Any]]:
    """The execution trace's records of the snapshot ``handle``."""
    lines = (result.run_dir / "trace.execution.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    return [record for record in records if (record.get("data") or {}).get("handle") == handle]


@pytest.mark.asyncio
async def test_without_its_table_the_verifier_takes_the_solvers_size(tmp_path: Path) -> None:
    provider = Provider()
    task = _task(tmp_path, environment=None)
    result = await _run(tmp_path, provider, task)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    solver, verifier = provider.requests
    assert verifier.resources.model_dump(exclude={"sudo"}) == solver.resources.model_dump(
        exclude={"sudo"}
    )


@pytest.mark.asyncio
async def test_a_snapshot_the_solver_keeps_is_the_one_its_verifier_booted_from(
    tmp_path: Path,
) -> None:
    provider = Provider()
    result = await _run(tmp_path, provider, _task(tmp_path), solver="snapshot", verifier="keep")

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    (handle,) = provider.snapshots
    assert provider.requests[1].snapshot == handle
    by_role = {item.roles: item for item in result.record.sandboxes}
    assert (by_role[("solver",)].outcome, by_role[("solver",)].handle) == ("snapshotted", handle)
    assert by_role[("verifier",)].outcome == "retained"
    assert result.lock is not None and result.lock.sandbox_outcomes == result.record.sandboxes


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provision", "verify"])
async def test_a_failed_verifier_still_takes_the_snapshot_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    provider = Provider()
    if failure == "provision":
        provider.fail = SandboxRole.VERIFIER
    else:
        exec_ = MemorySandbox.exec

        async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
            if list(argv) == ["bash", "test.sh"]:
                return ExecResult(exit_code=1, stderr="the verifier crashed")
            return await exec_(self, argv, **options)

        monkeypatch.setattr(MemorySandbox, "exec", exec)
    result = await _run(tmp_path, provider, _task(tmp_path))

    # A verifier that fails is the Task's failure, as a separate one's is.
    expected = Status.ENV_ERROR if failure == "provision" else Status.TASK_ERROR
    assert result.verdict.status is expected
    assert provider.snapshots == {}
    assert all(sandbox.destroyed for sandbox in provider.sandboxes)
    assert (("solver",), "destroyed") in [
        (item.roles, item.outcome) for item in result.record.sandboxes
    ]


@pytest.mark.asyncio
async def test_a_solver_that_cannot_be_sanitized_is_never_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        if argv[:3] == ["rm", "-rf", "--"] and "/opt/ale/setup" in argv:
            return ExecResult(exit_code=1, stderr="read-only file system")
        return await exec_(self, argv, **options)

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    provider = Provider()
    result = await _run(tmp_path, provider, _task(tmp_path), solver="snapshot")

    assert result.verdict.status is Status.ENV_ERROR
    assert result.verdict.failure is not None
    assert "read-only file system" in (result.verdict.failure.message or "")
    assert provider.snapshots == {} and len(provider.requests) == 1
    assert provider.sandboxes[0].destroyed


@pytest.mark.parametrize(
    ("solver", "verifier", "refused"),
    [
        ("keep", "destroy", "sandbox_retention.solver cannot be 'keep'"),
        ("destroy", "keep", "needs sandbox_retention.solver = 'snapshot'"),
        ("snapshot", "keep", None),
        ("destroy", "destroy", None),
    ],
)
def test_a_retention_that_cannot_hold_is_refused_up_front(
    tmp_path: Path, solver: str, verifier: str, refused: str | None
) -> None:
    retention = SandboxRetentionConfig(solver=solver, verifier=verifier)  # type: ignore[arg-type]
    spec = _task(tmp_path).spec
    if refused is None:
        check_retention(spec, retention)
    else:
        with pytest.raises(ConfigError, match=refused):
            check_retention(spec, retention)
    # Other topologies keep their solver as before.
    separate = spec.verify.model_copy(update={"environment_mode": VerificationMode.SEPARATE})
    check_retention(spec.model_copy(update={"verify": separate}), retention)


@pytest.mark.asyncio
async def test_an_episode_refuses_a_kept_solver_before_anything_starts(tmp_path: Path) -> None:
    provider = Provider()
    with pytest.raises(ConfigError, match="cannot be 'keep'"):
        await _run(tmp_path, provider, _task(tmp_path), solver="keep")
    assert provider.requests == []


@pytest.mark.asyncio
async def test_one_provider_that_saves_snapshots_serves_both_solver_and_verifier(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path, {**VERIFIER, "container_features": ["perf"]})
    # The first serves the solver, but not the verifier's container feature; the second
    # serves both; the third saves no snapshot.
    first = MemoryProvider("docker:runc", saves_snapshots=True)
    second = Provider("docker:kata", container_features=frozenset({ContainerFeature.PERF}))
    await provider_registry(first, second, only=True).select([task])
    assert (task.provider, task.verifier_provider) == ("docker:kata", "docker:kata")

    unsaved = MemoryProvider("docker:gvisor", container_features=frozenset(ContainerFeature))
    with pytest.raises(ProviderCapabilityError) as refused:
        await provider_registry(first, unsaved, only=True).select([task])
    # Each reason says whose it is.
    assert "agentslastexam/task solver and verifier:" in str(refused.value)
    assert "docker:runc: verifier: gives no container features perf" in str(refused.value)
    assert "docker:gvisor: solver: saves and boots no snapshots" in str(refused.value)


@pytest.mark.asyncio
async def test_reverification_boots_a_new_verifier_and_leaves_the_snapshot(
    tmp_path: Path,
) -> None:
    provider = Provider()
    handle = "memory:snapshot:source"
    provider.snapshots[handle] = {CHECKPOINT: b"weights", "/opt/ale/agent-episode": b"source"}
    task = _task(tmp_path)
    task.provider = task.verifier_provider = provider.id
    task.prepared_verifier_image = IMAGE
    lock = make_lock()
    assert lock.resources is not None
    snapshot = SolverSnapshot(
        handle=handle,
        episode_id="source",
        resolved_image=ResolvedImage(
            kind="container",
            prepared_identity=DIGEST,
            observed_identity=DIGEST,
            observed_ref="snapshot",
        ),
        allocation=lock.resources.effective,
    )

    async def reverify(source: SolverSnapshot) -> Any:
        return await run_episode(
            task,
            ReverificationProtocol(source, tmp_path, None),
            provider_registry(provider),
            run_dir=tmp_path / "runs",
            sandbox_retention=SandboxRetentionConfig(solver="keep", verifier="destroy"),
        )

    result = await reverify(snapshot)
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    (verifier,) = provider.requests
    assert (verifier.role, verifier.snapshot, verifier.resources.cpus) == (
        SandboxRole.VERIFIER,
        handle,
        8,
    )
    assert provider.snapshots[handle] == {
        CHECKPOINT: b"weights",
        "/opt/ale/agent-episode": b"source",
    }
    assert [(item.roles, item.outcome) for item in result.record.sandboxes] == [
        (("verifier",), "destroyed")
    ]

    # A snapshot that holds another episode's state is refused, and its verifier goes.
    provider.snapshots[handle]["/opt/ale/agent-episode"] = b"later"
    result = await reverify(snapshot)
    assert result.verdict.status is Status.ENV_ERROR
    assert "holds episode later's agent state" in (result.verdict.failure.message or "")
    assert all(sandbox.destroyed for sandbox in provider.sandboxes)
    assert handle in provider.snapshots


@pytest.mark.asyncio
async def test_a_save_that_fails_ends_the_episode_before_any_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What fails to be saved is what the agent left, so the failure is the solver's: the
    Episode ends unscored, no verifier starts, and the solver goes as a retention that
    failed, saved once, never again as the episode ends."""
    saves: list[str] = []

    async def snapshot(self: MemorySandbox, **_: object) -> Any:
        saves.append(self.sandbox_id)
        raise ProviderStartError("the disk is full")

    monkeypatch.setattr(MemorySandbox, "snapshot", snapshot)
    provider = Provider()
    result = await _run(tmp_path, provider, _task(tmp_path), solver="snapshot")
    assert result.verdict.status is Status.ENV_ERROR
    assert result.verdict.failure is not None and result.verdict.failure.phase == "agent"
    assert [request.role for request in provider.requests] == [SandboxRole.SOLVER]
    assert provider.sandboxes[0].destroyed and provider.snapshots == {}
    assert len(saves) == 1
    assert [(item.outcome, item.reason) for item in result.record.sandboxes] == [
        ("retention-failed", "the disk is full")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["bound", "save"])
async def test_only_the_save_s_own_bound_is_reported_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    """A timeout of the save's own says what timed out; only the framework's bound on the
    save reports that bound."""

    async def snapshot(self: MemorySandbox, **_: object) -> Any:
        if cause == "save":
            raise TimeoutError("the daemon did not answer in time")
        await asyncio.sleep(60)

    monkeypatch.setattr(MemorySandbox, "snapshot", snapshot)
    monkeypatch.setattr("ale.run.protocol.episode_protocol.SNAPSHOT_TIMEOUT_SEC", 0.05)
    provider = Provider()
    result = await _run(tmp_path, provider, _task(tmp_path))
    assert result.verdict.status is Status.ENV_ERROR
    message = result.verdict.failure.message if result.verdict.failure else ""
    if cause == "bound":
        assert "saving the solver's snapshot took more than 0 s" in (message or "")
    else:
        assert message == "the daemon did not answer in time"
    assert [request.role for request in provider.requests] == [SandboxRole.SOLVER]
    assert provider.sandboxes[0].destroyed


@pytest.mark.asyncio
@pytest.mark.parametrize("solver", ["destroy", "snapshot"])
async def test_a_solver_that_outlives_its_save_is_destroyed_with_its_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, solver: str
) -> None:
    """A solver its save could not destroy goes as the episode ends, whether its snapshot
    goes then or is kept."""
    destroy = MemorySandbox.destroy
    failed: list[str] = []

    async def flaky(self: MemorySandbox) -> None:
        if self.request.role is SandboxRole.SOLVER and not failed:
            failed.append(self.sandbox_id)
            raise ProviderStartError("the daemon did not answer")
        await destroy(self)

    monkeypatch.setattr(MemorySandbox, "destroy", flaky)
    provider = Provider()
    result = await _run(tmp_path, provider, _task(tmp_path), solver=solver)
    assert result.verdict.status is Status.ENV_ERROR
    assert [request.role for request in provider.requests] == [SandboxRole.SOLVER]
    assert failed and provider.sandboxes[0].destroyed
    (outcome,) = result.record.sandboxes
    if solver == "snapshot":
        assert outcome.outcome == "snapshotted" and list(provider.snapshots) == [outcome.handle]
    else:
        assert outcome.outcome == "destroyed" and provider.snapshots == {}


@pytest.mark.asyncio
async def test_a_verifier_timeout_destroys_the_snapshot_its_solver_asked_to_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out episode keeps nothing, its solver's snapshot included."""
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        if list(argv) == ["bash", "test.sh"]:
            return ExecResult(exit_code=None, timed_out=True)
        return await exec_(self, argv, **options)

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    provider = Provider()
    result = await _run(tmp_path, provider, _task(tmp_path), solver="snapshot")
    assert result.verdict.status is Status.TIMEOUT
    assert provider.snapshots == {}
    assert all(item.outcome == "destroyed" for item in result.record.sandboxes)


@pytest.mark.asyncio
async def test_a_cancelled_verifier_takes_its_snapshot_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    exec_ = MemorySandbox.exec

    async def exec(self: MemorySandbox, argv: Any, **options: Any) -> ExecResult:
        if list(argv) == ["bash", "test.sh"]:
            started.set()
            await asyncio.sleep(60)
        return await exec_(self, argv, **options)

    monkeypatch.setattr(MemorySandbox, "exec", exec)
    provider = Provider()
    episode = asyncio.ensure_future(_run(tmp_path, provider, _task(tmp_path)))
    await started.wait()
    episode.cancel()
    with pytest.raises(asyncio.CancelledError):
        await episode
    assert all(sandbox.destroyed for sandbox in provider.sandboxes)
    assert len(provider.sandboxes) == 2 and provider.snapshots == {}


def _lease(provider: Provider, **retention: str) -> _Lease:
    return _Lease(
        provider_registry(provider),
        SandboxRetentionConfig(**retention),  # type: ignore[arg-type]
        solver=provider.id,
        verifier=provider.id,
    )


def _solver_request() -> SandboxRequest:
    return SandboxRequest(
        episode_id="episode",
        role=SandboxRole.SOLVER,
        prepared_image=IMAGE,
        snapshot_verifier=Resources(cpus=8, memory_mb=8192),
        resources=Resources(cpus=1, memory_mb=1024),
        network=NetworkPolicy(),
    )


@pytest.mark.asyncio
async def test_a_snapshot_outlasts_every_sandbox_booted_from_it() -> None:
    """Released while its verifier runs, the solver's snapshot stays; reaped, the verifier
    goes first, since no Provider destroys a snapshot one still runs on."""
    provider = Provider()
    lease = _lease(provider)
    solver = await lease.acquire(_solver_request())
    lease.mark_sanitized(solver, succeeded=True)
    handle = await lease.snapshot(solver)
    verifier = await lease.acquire(
        _solver_request().model_copy(
            update={"role": SandboxRole.VERIFIER, "snapshot": handle, "snapshot_verifier": None}
        )
    )
    await lease.release(solver)
    assert handle in provider.snapshots
    await lease.release(verifier)
    await lease.release(solver)
    assert provider.snapshots == {}
    assert [item.roles for item in lease.outcomes] == [("verifier",), ("solver",)]


@pytest.mark.asyncio
async def test_undoing_a_kept_snapshot_and_its_kept_verifier_removes_the_verifier_first() -> None:
    provider = Provider()
    lease = _lease(provider, solver="snapshot", verifier="keep")
    solver = await lease.acquire(_solver_request())
    lease.mark_sanitized(solver, succeeded=True)
    handle = await lease.snapshot(solver)
    verifier = await lease.acquire(
        _solver_request().model_copy(
            update={"role": SandboxRole.VERIFIER, "snapshot": handle, "snapshot_verifier": None}
        )
    )
    lease.mark_sanitized(verifier, succeeded=True)
    await lease.finalize()
    assert [item.outcome for item in lease.outcomes] == ["retained", "snapshotted"]
    await lease.destroy_retained()
    assert provider.snapshots == {}
    assert verifier.destroyed  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_snapshot_that_cannot_be_destroyed_is_recorded_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last attempt records the snapshot it could not destroy, with its handle and why,
    so that it is found and removed rather than lost."""
    provider = Provider()
    monkeypatch.setattr(
        provider, "destroy_retained", AsyncMock(side_effect=ProviderStartError("busy"))
    )
    lease = _lease(provider)
    solver = await lease.acquire(_solver_request())
    lease.mark_sanitized(solver, succeeded=True)
    handle = await lease.snapshot(solver)
    with pytest.raises(RetentionFinalizationError):
        await lease.release(solver)
    assert lease.outcomes == [] and lease.live
    with pytest.raises(RetentionFinalizationError):
        await lease.release_all()
    (outcome,) = lease.outcomes
    assert (outcome.outcome, outcome.handle) == ("snapshotted", handle)
    assert outcome.reason is not None and "busy" in outcome.reason
    assert not lease.live


@pytest.mark.asyncio
async def test_a_continued_snapshot_is_verified_from_a_snapshot_of_its_own(
    tmp_path: Path,
) -> None:
    """A solver booted from an earlier episode's snapshot is saved again for its snapshot
    verifier, which boots from that new snapshot; the earlier one is left as it was."""
    provider = Provider()
    earlier = "memory:snapshot:earlier"
    provider.snapshots[earlier] = {CHECKPOINT: b"weights", "/opt/ale/agent-episode": b"earlier"}
    source = ResumedFrom(
        episode_id="earlier",
        retention="snapshot",
        handle=earlier,
        continuation=NativeContinuation(harness="oracle", native_session_id="session"),
    )
    task = _task(tmp_path)
    result = await run_episode(
        task,
        EpisodeProtocol(
            OracleHarness(),
            agent_enabled=False,
            resume=Resume(source=source, trajectory=None),
        ),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    solver, verifier = provider.requests
    assert solver.snapshot == earlier and solver.snapshot_verifier is not None
    assert verifier.snapshot not in (None, earlier)
    assert list(provider.snapshots) == [earlier]


@pytest.mark.asyncio
async def test_reverification_from_a_snapshot_keeps_the_agent_s_output_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With artifacts.collect = "host" the declared output is copied from the snapshot as
    the agent left it, before tests/ arrive."""
    order: list[str] = []
    collect = _Artifacts.collect

    async def collecting(self: _Artifacts, sandbox: Any, source: str, *args: Any) -> None:
        order.append(f"collect {source}")
        await collect(self, sandbox, source, *args)

    upload = MemorySandbox.upload_dir

    async def uploading(self: MemorySandbox, source: str, target: str, **options: Any) -> None:
        order.append(f"upload {target}")
        await upload(self, source, target, **options)

    monkeypatch.setattr(_Artifacts, "collect", collecting)
    monkeypatch.setattr(MemorySandbox, "upload_dir", uploading)
    provider = Provider()
    handle = "memory:snapshot:source"
    provider.snapshots[handle] = {CHECKPOINT: b"weights", "/opt/ale/agent-episode": b"source"}
    task = _task(tmp_path)
    task.provider = task.verifier_provider = provider.id
    task.prepared_verifier_image = IMAGE
    lock = make_lock()
    assert lock.resources is not None
    snapshot = SolverSnapshot(
        handle=handle,
        episode_id="source",
        resolved_image=ResolvedImage(
            kind="container",
            prepared_identity=DIGEST,
            observed_identity=DIGEST,
            observed_ref="snapshot",
        ),
        allocation=lock.resources.effective,
    )
    result = await run_episode(
        task,
        ReverificationProtocol(snapshot, tmp_path, None),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
        collect_artifacts=True,
        sandbox_retention=SandboxRetentionConfig(solver="keep", verifier="destroy"),
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert order[0] == "collect /home/user/output"
    assert order.index("collect /home/user/output") < order.index("upload /opt/ale/tests")


@pytest.mark.asyncio
async def test_the_solver_of_a_snapshot_verifier_asks_for_a_leaf_that_saves_and_serves_both() -> (
    None
):
    """Its request says it will be saved whatever its retention, and what its verifier needs
    of the same leaf, so a leaf that saves no snapshots, or does not serve the verifier,
    refuses it before anything starts."""
    request = _solver_request()
    assert request.retention == "destroy" and request.needs.snapshot
    with pytest.raises(ProviderCapabilityError, match="saves and boots no snapshots"):
        await MemoryProvider("docker:runc").accepts(request)
    small = Provider("docker:kata")
    small.max_cpus = 4
    with pytest.raises(
        ProviderCapabilityError, match="the verifier booted from its snapshot: 8 CPUs exceed"
    ):
        await small.accepts(request)
    await Provider("docker:kata").accepts(request)


@pytest.mark.asyncio
async def test_a_run_refuses_a_retention_that_cannot_hold_before_anything_is_prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ale.core.config import RunConfig
    from ale.run.cli import tasks as task_cli
    from ale.run.providers import ProviderRegistry

    for name in ("resolve_image_kinds", "prepare_task_image_result"):
        monkeypatch.setattr(task_cli, name, AsyncMock(side_effect=AssertionError(name)))
    settings = RunConfig.model_validate({"sandbox_retention": {"solver": "keep"}})
    with pytest.raises(ConfigError, match="cannot be 'keep'"):
        await task_cli._prepare_images([_task(tmp_path)], ProviderRegistry(settings))


@pytest.mark.asyncio
async def test_undoing_a_kept_snapshot_that_cannot_be_destroyed_records_it_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never recorded destroyed: it is left with its handle and cleanup command."""
    provider = Provider()
    lease = _lease(provider, solver="snapshot")
    solver = await lease.acquire(_solver_request().model_copy(update={"snapshot_verifier": None}))
    lease.mark_sanitized(solver, succeeded=True)
    await lease.finalize()
    (kept,) = lease.outcomes
    monkeypatch.setattr(
        provider, "destroy_retained", AsyncMock(side_effect=ProviderStartError("busy"))
    )
    with pytest.raises(RetentionFinalizationError):
        await lease.destroy_retained()
    (left,) = lease.outcomes
    assert (left.outcome, left.handle, left.cleanup_command) == (
        "not-destroyed",
        kept.handle,
        kept.cleanup_command,
    )
    assert left.reason is not None and "busy" in left.reason


@pytest.mark.asyncio
async def test_a_snapshot_stays_while_a_sandbox_that_could_not_be_destroyed_runs_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Sandbox is recorded as left, with its handle; its snapshot is never destroyed
    under it, and is recorded as left too."""
    provider = Provider()
    lease = _lease(provider)
    solver = await lease.acquire(_solver_request())
    lease.mark_sanitized(solver, succeeded=True)
    handle = await lease.snapshot(solver, validation=True)
    booted = await lease.acquire(
        _solver_request().model_copy(update={"snapshot": handle, "snapshot_verifier": None})
    )
    monkeypatch.setattr(booted, "destroy", AsyncMock(side_effect=ProviderStartError("stuck")))
    destroy_retained = AsyncMock()
    monkeypatch.setattr(provider, "destroy_retained", destroy_retained)
    with pytest.raises(RetentionFinalizationError):
        await lease.release(booted)
    await lease.release(solver)
    with pytest.raises(RetentionFinalizationError, match="still runs on it"):
        await lease.release_all()
    destroy_retained.assert_not_called()
    by_handle = {item.handle: item for item in lease.outcomes}
    assert by_handle[handle].outcome == "not-destroyed"
    (stuck,) = [item for item in lease.outcomes if item.handle != handle]
    assert stuck.outcome == "not-destroyed" and stuck.handle and stuck.cleanup_command
