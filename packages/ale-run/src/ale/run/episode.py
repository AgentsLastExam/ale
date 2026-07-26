"""Running one episode.

The smallest thing that can produce a verdict: lease a sandbox, hand the task to an
environment, map whatever comes back to the shared status taxonomy, and write the
episode's traces and artifacts.

Error mapping lives here rather than in the environment on purpose. An environment
should raise what actually went wrong and let one place decide what that means for a
result, so `task_error` means the same thing across every domain.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ale.core.environment import ArtifactSink, Budget, Environment, EpisodeContext, Phase
from ale.core.errors import (
    AgentError,
    AgentRefusalError,
    BudgetExceededError,
    EnvironmentError_,
    PhaseTimeoutError,
    TaskError,
)
from ale.core.harness import HarnessSession
from ale.core.sandbox import Provider, Sandbox, SandboxRequest
from ale.core.task import Task
from ale.core.trace import PhaseSpan, TimingRecord, TraceLayer, TraceWriter, read_records
from ale.core.verdict import Status, Verdict

__all__ = ["EpisodeResult", "run_episode"]

#: Which status an error class implies. Order matters: the first match wins, so
#: subclasses must precede their bases.
_STATUS_BY_ERROR: tuple[tuple[type[BaseException], Status], ...] = (
    (PhaseTimeoutError, Status.TIMEOUT),
    (BudgetExceededError, Status.BUDGET_EXCEEDED),
    (AgentRefusalError, Status.REFUSED),
    (AgentError, Status.AGENT_ERROR),
    (TaskError, Status.TASK_ERROR),
    (EnvironmentError_, Status.ENV_ERROR),
)


def status_for(error: BaseException) -> Status:
    """Map a raised error to the status a result should carry."""
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(error, error_type):
            return status
    return Status.ENV_ERROR


@dataclass
class EpisodeResult:
    """One episode's outcome and where its evidence landed."""

    episode_id: str
    verdict: Verdict
    run_dir: Path
    duration_sec: float


class _Lease:
    """Hands out sandboxes and guarantees they are reaped.

    An environment may hold several at once — a grader and a solver, say — so the lease
    tracks them rather than assuming one.
    """

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.live: list[Sandbox] = []

    async def acquire(self, request: SandboxRequest) -> Sandbox:
        sandbox = await self.provider.create(request)
        self.live.append(sandbox)
        return sandbox

    async def release(self, sandbox: Sandbox) -> None:
        await sandbox.destroy()
        if sandbox in self.live:
            self.live.remove(sandbox)

    async def release_all(self) -> None:
        for sandbox in list(self.live):
            await self.release(sandbox)


@dataclass
class _Artifacts(ArtifactSink):
    """Collects files out of a sandbox into the episode's run directory."""

    run_dir: Path
    collected: list[str] = field(default_factory=list)

    async def collect(self, sandbox: Sandbox, source: str, name: str) -> Path:
        target = self.path(name)
        target.mkdir(parents=True, exist_ok=True)
        await sandbox.download_dir(source, str(target))
        self.collected.append(name)
        return target

    def path(self, name: str) -> Path:
        return self.run_dir / "artifacts" / name


@dataclass
class _DiscardedArtifacts(ArtifactSink):
    """Honours the declaration and keeps nothing.

    A run that only wants scores should not pay to copy gigabytes back, but the task
    still declared where its output lives — so the paths stay declared and this sink
    drops them. Environments need no branch for it.
    """

    run_dir: Path

    async def collect(self, sandbox: Sandbox, source: str, name: str) -> Path:
        return self.path(name)

    def path(self, name: str) -> Path:
        return self.run_dir / "artifacts" / name


async def run_episode(
    task: Task,
    environment: Environment,
    provider: Provider,
    *,
    run_dir: Path,
    gateway_url: str = "",
    token: str = "",
    model: str = "",
    seed: int = 0,
    work_dir: str = "/ale/work",
    collect_artifacts: bool = True,
) -> EpisodeResult:
    """Administer one task and return its verdict.

    Never raises for an episode that merely failed: a failure is a result with a status,
    and the caller decides what to do with it.
    """
    episode_id = f"{task.spec.id}-{uuid.uuid4().hex[:8]}"
    episode_dir = run_dir / episode_id
    episode_dir.mkdir(parents=True, exist_ok=True)

    sink = _Artifacts(episode_dir) if collect_artifacts else _DiscardedArtifacts(episode_dir)
    lease = _Lease(provider)
    started = time.monotonic()
    ctx = EpisodeContext(
        episode_id=episode_id,
        spec=task.spec,
        task_dir=getattr(getattr(task, "folder", None), "root", run_dir),
        run_dir=episode_dir,
        sandboxes=lease,
        artifacts=sink,
        trace=TraceWriter(episode_dir),
        budget=Budget(deadline_sec=task.spec.timeouts.total, started_at=started),
        session=HarnessSession(
            episode_id=episode_id, gateway_url=gateway_url, token=token, model=model
        ),
        seed=seed,
        work_dir=work_dir,
    )

    try:
        verdict = await environment.run(task, ctx)
    except Exception as exc:  # every failure becomes a typed result, not a traceback
        verdict = Verdict.failed(status_for(exc), exc, phase=_phase_of(exc))
    finally:
        await lease.release_all()

    duration = time.monotonic() - started
    _write_timing(ctx.trace, episode_dir, duration, tuple(ctx.phases))

    return EpisodeResult(
        episode_id=episode_id,
        verdict=verdict,
        run_dir=episode_dir,
        duration_sec=duration,
    )


def _write_timing(
    trace: TraceWriter,
    episode_dir: Path,
    duration_sec: float,
    phases: tuple[PhaseSpan, ...] = (),
) -> None:
    """Record where the wall clock went, from evidence already on disk.

    One duration says almost nothing: twenty minutes could be a slow model, a slow
    sandbox or slow framework code, and each has a different fix. Model time comes from
    the gateway's own records and sandbox time from executed commands, so the split
    cannot drift from what happened; the remainder is framework time, which is the
    honest way to report what nobody accounted for.
    """
    total_ms = int(duration_sec * 1000)
    model_ms = sum(
        int(record.get("latency_ms") or 0)
        for record in read_records(episode_dir / "trace.transport.jsonl")
    )
    sandbox_ms = sum(
        int(record.get("duration_ms") or 0)
        for record in read_records(episode_dir / "trace.semantic.jsonl")
        if record.get("kind") == "exec"
    )
    # Clamp before taking the remainder: concurrent work can otherwise sum past the wall
    # clock and produce a negative framework share.
    model_ms = min(model_ms, total_ms)
    sandbox_ms = min(sandbox_ms, total_ms - model_ms)

    trace.write_semantic(
        TimingRecord(
            seq=trace.next_seq(TraceLayer.SEMANTIC),
            total_ms=total_ms,
            model_ms=model_ms,
            sandbox_ms=sandbox_ms,
            framework_ms=total_ms - model_ms - sandbox_ms,
            phases=phases,
        )
    )


def _phase_of(error: BaseException) -> str | None:
    if isinstance(error, PhaseTimeoutError):
        return error.phase
    if isinstance(error, TaskError):
        return Phase.VERIFY.value
    return None
