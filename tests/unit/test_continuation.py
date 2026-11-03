"""Continuing an earlier episode: its records, its retained solver and its trajectory."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_runtime_contracts import DIGEST, make_lock

from ale.core.config import RunConfig, SandboxRetentionConfig
from ale.core.errors import ConfigError, ProviderCapabilityError, TaskDefinitionError
from ale.core.harness import NativeContinuation
from ale.core.lock import ResourceProvenance, ResumedFrom
from ale.core.result import ResultRecord, SandboxOutcome
from ale.core.sandbox import (
    ExecResult,
    PreparedTaskImage,
    RetainedSandbox,
    SandboxRequest,
    SandboxRole,
)
from ale.core.taskspec import NetworkPolicy, OperatingSystem, Resources
from ale.core.trajectory import (
    AtifAgent,
    AtifObservation,
    AtifObservationResult,
    AtifSubagentTrajectoryRef,
    AtifToolCall,
    AtifTrajectory,
    TrajectoryBuilder,
)
from ale.core.verdict import Status
from ale.run.cli.tasks import _continuation
from ale.run.episode import _Lease
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.protocol.episode_protocol import (
    EpisodeProtocol,
    Resume,
    _continued_segment,
    _require_agent_episode,
)
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit

CONTINUATION = NativeContinuation(harness="claude-code", native_session_id="session")
AGENT = AtifAgent(name="claude-code", version="2.1.170")


# --- the lease ---


def _request(role: SandboxRole = SandboxRole.SOLVER) -> SandboxRequest:
    return SandboxRequest(
        episode_id="later",
        role=role,
        prepared_image=PreparedTaskImage(
            kind="container",
            source="external-ref",
            input_identity=DIGEST,
            runtime_ref="image@" + DIGEST,
            prepared_identity=DIGEST,
            resolved_reference="image@" + DIGEST,
        ),
        resources=Resources(),
        network=NetworkPolicy(),
    )


def _kept() -> SimpleNamespace:
    return SimpleNamespace(
        sandbox_id="kept",
        allocation=SimpleNamespace(provider="qemu"),
        destroy=AsyncMock(),
        retain=AsyncMock(),
        release_resources=lambda: None,
    )


@pytest.mark.asyncio
async def test_an_adopted_sandbox_follows_this_episodes_retention() -> None:
    sandbox = _kept()
    attached: list[SandboxRequest] = []

    async def attach(request: SandboxRequest) -> Any:
        attached.append(request)
        return sandbox

    lease = _Lease(SimpleNamespace(), SandboxRetentionConfig(solver="destroy"))  # type: ignore[arg-type]
    assert await lease.adopt(_request(), attach) is sandbox
    assert attached[0].retention == "destroy"
    await lease.finalize()
    sandbox.destroy.assert_awaited_once()
    assert [(item.outcome, item.provider) for item in lease.outcomes] == [("destroyed", "qemu")]


@pytest.mark.asyncio
async def test_a_refused_attachment_leaves_nothing_to_reap() -> None:
    async def attach(request: SandboxRequest) -> Any:
        raise ProviderCapabilityError("refused")

    lease = _Lease(SimpleNamespace(), SandboxRetentionConfig(solver="keep"))  # type: ignore[arg-type]
    with pytest.raises(ProviderCapabilityError):
        await lease.adopt(_request(), attach)
    await lease.finalize()
    assert not lease.live and not lease.outcomes


def _retained(handle: str) -> RetainedSandbox:
    return RetainedSandbox(
        provider="docker",
        handle=handle,
        episode_id="later",
        roles=("solver",),
        reason="kept",
        cleanup_command=f"ale sandbox destroy {handle}",
    )


async def _snapshotting_lease(sandbox: SimpleNamespace, providers: object = None) -> _Lease:
    async def attach(request: SandboxRequest) -> Any:
        sandbox.request = request
        return sandbox

    lease = _Lease(providers or SimpleNamespace(), SandboxRetentionConfig(solver="snapshot"))  # type: ignore[arg-type]
    await lease.adopt(_request(), attach)
    lease.mark_sanitized(sandbox, succeeded=True)  # type: ignore[arg-type]
    return lease


@pytest.mark.asyncio
async def test_a_snapshotted_solver_is_saved_then_destroyed() -> None:
    sandbox = _kept()
    sandbox.snapshot = AsyncMock(return_value=_retained("docker:snapshot:kept"))
    lease = await _snapshotting_lease(sandbox)
    assert sandbox.request.retention == "snapshot"
    await lease.release(sandbox)  # type: ignore[arg-type]
    sandbox.destroy.assert_not_awaited()  # kept for verification, then saved

    await lease.finalize()
    sandbox.snapshot.assert_awaited_once()
    sandbox.retain.assert_not_awaited()
    sandbox.destroy.assert_awaited_once()
    assert [(item.outcome, item.handle) for item in lease.outcomes] == [
        ("snapshotted", "docker:snapshot:kept")
    ]
    assert not lease.live


@pytest.mark.asyncio
async def test_a_failed_snapshot_destroys_the_solver_and_says_why() -> None:
    sandbox = _kept()
    sandbox.snapshot = AsyncMock(side_effect=RuntimeError("disk full"))
    lease = await _snapshotting_lease(sandbox)
    await lease.finalize()
    sandbox.destroy.assert_awaited_once()
    assert [(item.outcome, item.reason) for item in lease.outcomes] == [
        ("retention-failed", "disk full")
    ]


@pytest.mark.asyncio
async def test_undoing_a_snapshot_removes_it() -> None:
    sandbox = _kept()
    sandbox.snapshot = AsyncMock(return_value=_retained("docker:snapshot:kept"))
    provider = SimpleNamespace(destroy_retained=AsyncMock())
    lease = await _snapshotting_lease(sandbox, SimpleNamespace(get=lambda kind: provider))
    await lease.finalize()
    await lease.destroy_retained()
    provider.destroy_retained.assert_awaited_once_with("docker:snapshot:kept")
    assert [item.outcome for item in lease.outcomes] == ["destroyed"]


@pytest.mark.parametrize(
    ("solver", "verifier", "policy"),
    [
        ("snapshot", "keep", "snapshot"),
        ("keep", "destroy", "keep"),
        ("destroy", "keep", "keep"),
        ("destroy", "destroy", "destroy"),
    ],
)
def test_a_shared_sandbox_follows_the_solvers_retention_first(
    solver: str, verifier: str, policy: str
) -> None:
    lease = _Lease(
        SimpleNamespace(),  # type: ignore[arg-type]
        SandboxRetentionConfig(solver=solver, verifier=verifier),  # type: ignore[arg-type]
    )
    assert lease._policy(_request(SandboxRole.SHARED)) == (("solver", "verifier"), policy)


# --- the solver's owner ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("os", "path"),
    [
        (OperatingSystem.LINUX, "/opt/ale/agent-episode"),
        (OperatingSystem.WINDOWS, r"C:\ProgramData\ALE\agent-episode"),
    ],
)
async def test_a_retained_solver_serves_only_the_episode_whose_agent_it_holds(
    os: OperatingSystem, path: str
) -> None:
    held = {path: b"earlier"}

    async def read_file(name: str, **_: object) -> bytes:
        return held[name]

    sandbox = SimpleNamespace(sandbox_id="kept", read_file=read_file, retain=AsyncMock())
    await _require_agent_episode(sandbox, os, "earlier")  # type: ignore[arg-type]
    sandbox.retain.assert_not_awaited()

    held[path] = b"later"
    with pytest.raises(ProviderCapabilityError, match="holds episode later's agent state"):
        await _require_agent_episode(sandbox, os, "earlier")  # type: ignore[arg-type]
    # Refused, it is only disconnected: it stays retained for the episode that owns it.
    sandbox.retain.assert_awaited_once()

    del held[path]
    with pytest.raises(ProviderCapabilityError, match="no recorded episode's"):
        await _require_agent_episode(sandbox, os, "earlier")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_snapshot_continuation_boots_a_new_solver_and_claims_it(tmp_path: Path) -> None:
    held = {"/opt/ale/agent-episode": b"earlier"}

    async def read_file(path: str, **_: object) -> bytes:
        return held[path]

    async def write_file(path: str, data: bytes, **_: object) -> None:
        held[path] = data

    sandbox = SimpleNamespace(
        read_file=read_file,
        write_file=write_file,
        resolved_image=None,
        allocation=None,
        agent_user="user",
        agent_home="/home/user",
    )
    acquired: list[SandboxRequest] = []

    async def acquire(request: SandboxRequest) -> Any:
        acquired.append(request)
        return sandbox

    ctx = SimpleNamespace(
        spec=load_tasks(scaffold_task(tmp_path / "continued"))[0].spec,
        proxy_url="",
        allowed_hosts=frozenset(),
        episode_id="later",
        prepared_image=_request().prepared_image,
        session=SimpleNamespace(gateway_url="", token=""),
        sandboxes=SimpleNamespace(acquire=acquire),
    )
    source = ResumedFrom(
        episode_id="earlier",
        retention="snapshot",
        handle="docker:snapshot:kept",
        continuation=CONTINUATION,
    )
    protocol = EpisodeProtocol(ClaudeCodeHarness(), resume=Resume(source=source, trajectory=None))
    assert await protocol._provision(ctx) is sandbox  # type: ignore[arg-type]
    assert acquired[0].snapshot == "docker:snapshot:kept"
    assert held == {"/opt/ale/agent-episode": b"later"}
    assert ctx.resumed_from == source

    # Another episode's state is refused; the lease reaps the new solver.
    with pytest.raises(ProviderCapabilityError, match="holds episode later's agent state"):
        await protocol._provision(ctx)  # type: ignore[arg-type]


def _retention_context(
    os: OperatingSystem, home: str, record: list[tuple[bool, str | None]]
) -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(os=os),
        home=home,
        episode_id="episode",
        sandboxes=SimpleNamespace(
            mark_sanitized=lambda _, *, succeeded, reason=None: record.append((succeeded, reason))
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "agent", "marked"),
    [
        (SandboxRole.SOLVER, True, True),
        (SandboxRole.SHARED, True, True),
        (SandboxRole.VERIFIER, True, False),
        (SandboxRole.SOLVER, False, False),
    ],
)
async def test_retention_removes_every_task_stage_and_names_the_agents_episode(
    role: SandboxRole, agent: bool, marked: bool
) -> None:
    removed: list[list[str]] = []
    written: dict[str, bytes] = {}

    async def exec(argv: list[str], **_: object) -> ExecResult:
        removed.append(list(argv))
        return ExecResult(exit_code=0)

    async def write_file(path: str, data: bytes, **_: object) -> None:
        written[path] = data

    sandbox = SimpleNamespace(
        request=_request(role).model_copy(update={"retention": "keep"}),
        exec=exec,
        write_file=write_file,
    )
    sanitized: list[tuple[bool, str | None]] = []
    ctx = _retention_context(OperatingSystem.LINUX, "/home/user", sanitized)
    protocol = EpisodeProtocol(ClaudeCodeHarness(), agent_enabled=agent)
    assert await protocol._sanitize_for_retention(ctx, sandbox)  # type: ignore[arg-type]

    arguments = {argument for argv in removed for argument in argv}
    assert {
        "/opt/ale/setup",
        "/opt/ale/tests",
        "/home/user/.ale-solution",
        "/logs/verifier",
    } <= arguments
    assert written == ({"/opt/ale/agent-episode": b"episode"} if marked else {})
    assert sanitized == [(True, None)]


@pytest.mark.asyncio
async def test_windows_retention_empties_the_bases_stage_directories() -> None:
    commands: list[str] = []

    async def exec(argv: list[str], **_: object) -> ExecResult:
        commands.append(argv[-1])
        return ExecResult(exit_code=0)

    async def write_file(path: str, data: bytes, **_: object) -> None:
        pass

    sandbox = SimpleNamespace(
        request=_request(SandboxRole.SOLVER).model_copy(update={"retention": "keep"}),
        exec=exec,
        write_file=write_file,
    )
    sanitized: list[tuple[bool, str | None]] = []
    ctx = _retention_context(OperatingSystem.WINDOWS, r"C:\Users\user", sanitized)
    protocol = EpisodeProtocol(ClaudeCodeHarness())
    assert await protocol._sanitize_for_retention(ctx, sandbox)  # type: ignore[arg-type]

    stages, others, result = commands
    # The base made these directories with their own ACL; only what is in them goes.
    assert r"'C:\ProgramData\ALE\setup','C:\ProgramData\ALE\tests'" in stages
    assert "Get-ChildItem -LiteralPath $stage" in stages
    assert r"'C:\Users\user\.ale-solution'" in others
    # A missing path is skipped like `rm -rf` does, not silenced into a failing exit.
    assert "Test-Path -LiteralPath $path" in others
    assert "SilentlyContinue" not in stages + others
    assert r"$verifier = 'C:\logs\verifier'" in result
    assert sanitized == [(True, None)]


@pytest.mark.asyncio
async def test_a_failed_stage_clearing_stops_retention_with_its_reason() -> None:
    calls: list[list[str]] = []

    async def exec(argv: list[str], **_: object) -> ExecResult:
        calls.append(list(argv))
        return ExecResult(exit_code=1, stderr="permission denied")

    sandbox = SimpleNamespace(
        request=_request(SandboxRole.SOLVER).model_copy(update={"retention": "keep"}),
        exec=exec,
    )
    sanitized: list[tuple[bool, str | None]] = []
    ctx = _retention_context(OperatingSystem.LINUX, "/home/user", sanitized)
    protocol = EpisodeProtocol(ClaudeCodeHarness())
    assert not await protocol._sanitize_for_retention(ctx, sandbox)  # type: ignore[arg-type]
    assert len(calls) == 1
    assert sanitized == [(False, "permission denied")]


# --- the trajectory segment ---


def _turn(builder: TrajectoryBuilder, prompt: str, call: str, child: str | None = None) -> None:
    builder.add(source="user", message=prompt)
    builder.add(
        source="agent",
        message="",
        tool_calls=[AtifToolCall(tool_call_id=call, function_name="bash", arguments={})],
        observation=AtifObservation(
            results=[
                AtifObservationResult(
                    source_call_id=call,
                    content="ok",
                    subagent_trajectory_ref=(
                        [AtifSubagentTrajectoryRef(trajectory_id=child)] if child else None
                    ),
                )
            ]
        ),
    )
    builder.add(source="agent", message=f"{prompt} done")


def _session(turns: int, *, children: bool = False) -> AtifTrajectory:
    builder = TrajectoryBuilder(agent=AGENT, trajectory_id="parsed", session_id="session")
    for index in range(turns):
        _turn(builder, f"turn {index}", f"call-{index}", f"child-{index}" if children else None)
    subagents = [_child(f"child-{index}") for index in range(turns)] if children else None
    return builder.build(subagent_trajectories=subagents)


def _child(trajectory_id: str) -> AtifTrajectory:
    builder = TrajectoryBuilder(agent=AGENT, trajectory_id=trajectory_id)
    builder.add(source="user", message=trajectory_id)
    return builder.build()


def _segment_of(
    parsed: AtifTrajectory, start: int, stop: int, *, prior: int | None = None
) -> AtifTrajectory:
    """What an earlier episode recorded: its steps of the session, renumbered."""
    steps = [
        step.model_copy(update={"step_id": index})
        for index, step in enumerate(parsed.steps[start:stop], start=1)
    ]
    extra = None if prior is None else {"ale": {"resumed_from": {"native_steps": prior}}}
    return parsed.model_copy(update={"steps": steps, "trajectory_id": "earlier", "extra": extra})


def _resume(trajectory: AtifTrajectory | None) -> Resume:
    return Resume(
        source=ResumedFrom(
            episode_id="earlier", retention="keep", handle="docker:kept", continuation=CONTINUATION
        ),
        attach=AsyncMock(),
        trajectory=trajectory,
    )


def test_a_continuation_keeps_only_its_own_turn_and_the_subagents_it_spawned() -> None:
    parsed = _session(2, children=True)
    segment = _continued_segment(parsed, _resume(_segment_of(parsed, 0, 3)))

    assert [step.step_id for step in segment.steps] == [1, 2, 3]
    assert segment.steps[0].message == "turn 1"
    assert [child.trajectory_id for child in segment.subagent_trajectories or ()] == ["child-1"]
    assert segment.session_id == "session"
    assert segment.final_metrics is not None and segment.final_metrics.total_steps == 3
    assert (segment.extra or {})["ale"]["resumed_from"] == {
        "episode_id": "earlier",
        "trajectory_id": "earlier",
        "native_steps": 3,
    }


def test_a_continuation_of_a_continuation_counts_every_earlier_segment() -> None:
    parsed = _session(3)
    second = _segment_of(parsed, 3, 6, prior=3)
    segment = _continued_segment(parsed, _resume(second))
    assert segment.steps[0].message == "turn 2"
    assert (segment.extra or {})["ale"]["resumed_from"]["native_steps"] == 6


@pytest.mark.parametrize("earlier", ["missing", "mismatch", "unknown"])
def test_an_unfound_boundary_keeps_the_whole_session_and_says_so(earlier: str) -> None:
    parsed = _session(2)
    source = {
        "missing": None,
        "mismatch": _segment_of(parsed, 1, 2),
        "unknown": _segment_of(parsed, 0, 3).model_copy(
            update={"extra": {"ale": {"resumed_from": {"episode_id": "older"}}}}
        ),
    }[earlier]
    segment = _continued_segment(parsed, _resume(source))
    assert segment.steps == parsed.steps
    ale = (segment.extra or {})["ale"]
    assert ale["parse_issues"][0]["reason"] == "continuation_boundary_not_found"
    assert "native_steps" not in ale["resumed_from"]


# --- the earlier episode's records ---


def _source(tmp_path: Path, **changes: Any) -> tuple[Path, Any]:
    task = load_tasks(scaffold_task(tmp_path / "continued"))[0]
    task.prepared_image = _request().prepared_image
    lock = make_lock(
        resources=ResourceProvenance(
            requested=task.spec.resources, effective=make_lock().resources.effective
        )
    )
    record = ResultRecord(
        episode_id="earlier",
        status=Status.COMPLETED,
        rewards={"reward": 1.0},
        started_at="2026-10-27T00:00:00Z",
        finished_at="2026-10-27T00:01:00Z",
        sandboxes=(
            SandboxOutcome(
                roles=("solver", "verifier"),
                requested="keep",
                outcome="retained",
                provider="docker",
                handle="docker:kept",
            ),
        ),
        continuation=CONTINUATION,
    )
    record = record.model_copy(update=changes.pop("record", {}))
    lock = lock.model_copy(update=changes.pop("lock", {}))
    episode = tmp_path / "earlier"
    episode.mkdir()
    (episode / "result.json").write_text(record.model_dump_json())
    (episode / "lock.json").write_text(lock.model_dump_json())
    return episode, task


def test_the_earlier_episode_names_its_solver_session_and_trajectory(tmp_path: Path) -> None:
    episode, task = _source(tmp_path)
    harness = SimpleNamespace(name="claude-code", version=lambda: "2.1.170")
    resume = _continuation(episode, [task], RunConfig(), harness)  # type: ignore[arg-type]
    assert resume.source == ResumedFrom(
        episode_id="earlier", retention="keep", handle="docker:kept", continuation=CONTINUATION
    )
    assert resume.trajectory is None
    assert resume.attach is not None


def test_a_snapshot_continues_in_a_new_solver_with_this_tasks_resources(tmp_path: Path) -> None:
    snapshot = SandboxOutcome(
        roles=("solver",),
        requested="snapshot",
        outcome="snapshotted",
        provider="docker",
        handle="docker:snapshot:kept",
    )
    resources = make_lock().resources.model_copy(update={"requested": Resources(cpus=2)})
    episode, task = _source(
        tmp_path, record={"sandboxes": (snapshot,)}, lock={"resources": resources}
    )
    harness = SimpleNamespace(name="claude-code", version=lambda: "2.1.170")
    resume = _continuation(episode, [task], RunConfig(), harness)  # type: ignore[arg-type]
    assert resume.source.retention == "snapshot"
    assert resume.source.handle == "docker:snapshot:kept"
    assert resume.attach is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"record": {"continuation": None}}, "no retained solver"),
        ({"record": {"sandboxes": ()}}, "no retained solver"),
        ({"lock": {"agent": make_lock().agent.model_copy(update={"version": "2.0"})}}, "CLI"),
        (
            {
                "lock": {
                    "image": make_lock().image.model_copy(
                        update={"prepared_identity": "sha256:" + "1" * 64}
                    )
                }
            },
            "image",
        ),
        (
            {
                "lock": {
                    "resources": make_lock().resources.model_copy(
                        update={"requested": Resources(cpus=2)}
                    )
                }
            },
            "resources",
        ),
        (
            {
                "record": {
                    "sandboxes": (
                        SandboxOutcome(
                            roles=("solver",),
                            requested="keep",
                            outcome="retained",
                            provider="docker",
                            handle="docker:kept",
                        ),
                    )
                }
            },
            "verification mode",
        ),
    ],
)
def test_an_episode_that_cannot_be_continued_here_is_refused(
    tmp_path: Path, changes: dict[str, Any], reason: str
) -> None:
    episode, task = _source(tmp_path, **changes)
    harness = SimpleNamespace(name="claude-code", version=lambda: "2.1.170")
    with pytest.raises(TaskDefinitionError, match=reason):
        _continuation(episode, [task], RunConfig(), harness)  # type: ignore[arg-type]
    other = SimpleNamespace(name="codex-cli", version=lambda: "2.1.170")
    with pytest.raises((TaskDefinitionError, ConfigError)):
        _continuation(episode, [task], RunConfig(), other)  # type: ignore[arg-type]


def test_only_one_episode_of_one_task_continues(tmp_path: Path) -> None:
    episode, task = _source(tmp_path)
    harness = SimpleNamespace(name="claude-code", version=lambda: "2.1.170")
    with pytest.raises(ConfigError, match="one episode of one Task"):
        _continuation(episode, [task, task], RunConfig(), harness)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="one episode of one Task"):
        _continuation(episode, [task], RunConfig(episodes=2), harness)  # type: ignore[arg-type]
