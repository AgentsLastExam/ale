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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from ale.core.config import LoggingPolicy, VerificationConfig
from ale.core.environment import ArtifactSink, Budget, Environment, EpisodeContext, Phase
from ale.core.errors import (
    AgentError,
    AgentRefusalError,
    BudgetExceededError,
    EnvironmentError_,
    PhaseTimeoutError,
    TaskError,
)
from ale.core.harness import EffectiveAgentResources, HarnessSession
from ale.core.lock import LimitTermination, RunLock
from ale.core.result import FailureInfo as ResultFailureInfo
from ale.core.result import PhaseTiming, ResultRecord
from ale.core.sandbox import Provider, Sandbox, SandboxRequest
from ale.core.task import Task
from ale.core.trace import ExecutionFailure, PhaseFinished, PhaseStarted
from ale.core.verdict import Status, Verdict
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, Limits
from ale.run.provenance import ProvenanceInputs, build_lock, judge_provenance
from ale.run.recording import EpisodeRecording
from ale_verify import VerificationRecord

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
    record: ResultRecord
    lock: RunLock | None = None
    """Absent when the caller asked for no provenance, or the episode never provisioned."""


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

    async def collect_file(self, sandbox: Sandbox, source: str, name: str) -> Path:
        """One file rather than a directory, for a harness's own logs.

        Kept out of ``artifacts/``: what the agent produced and how the harness went about
        producing it are different things, and a reader who mixes them cannot tell which
        is which.
        """
        target = self.run_dir / "logs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await sandbox.read_file(source))
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

    async def collect_file(self, sandbox: Sandbox, source: str, name: str) -> Path:
        target = self.run_dir / "logs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await sandbox.read_file(source))
        return target

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
    collect_artifacts: bool = True,
    provenance: ProvenanceInputs | None = None,
    proxy_url: str = "",
    gateway: Gateway | None = None,
    limits: Limits | None = None,
    allowed_hosts: frozenset[str] = frozenset(),
    agent_resources: EffectiveAgentResources | None = None,
    episode_id: str | None = None,
    trajectory_id: str | None = None,
    phase_callback: Callable[[Phase], None] | None = None,
    logging_policy: LoggingPolicy | None = None,
    verification_config: VerificationConfig | None = None,
) -> EpisodeResult:
    """Administer one task and return its verdict.

    Never raises for an episode that merely failed: a failure is a result with a status,
    and the caller decides what to do with it.
    """
    episode_id = episode_id or f"{task.spec.id}-{uuid.uuid4().hex[:8]}"
    trajectory_id = trajectory_id or f"trajectory-{uuid.uuid4().hex}"
    episode_dir = run_dir / episode_id
    recording = EpisodeRecording(episode_dir)
    # The session is opened here, not by the caller, for two reasons that only show up
    # afterwards: it can carry this episode's own identifier instead of a placeholder,
    # and it can be given the trace the gateway writes model calls into — which does not
    # exist until the episode has a directory. Opened earlier, every transport record was
    # simply never written.
    session_token, session = token, None
    if gateway is not None:
        session = gateway.open_session(
            GatewaySession(
                episode_id=episode_id,
                model=model,
                limits=limits or Limits(),
                allowed_hosts=allowed_hosts,
                payload_dir=(
                    episode_dir / "logs" / "gateway"
                    if (logging_policy or LoggingPolicy()).transport_payloads == "debug"
                    else None
                ),
                exact_token_dir=(
                    episode_dir / "logs" / "gateway" / "tokens"
                    if (logging_policy or LoggingPolicy()).token_data == "exact"
                    else None
                ),
            ),
            trace=recording.transport,
        )
        session_token = session.token

    sink = _Artifacts(episode_dir) if collect_artifacts else _DiscardedArtifacts(episode_dir)
    lease = _Lease(provider)
    started_at = datetime.now(UTC)
    started = time.monotonic()
    ctx = EpisodeContext(
        episode_id=episode_id,
        spec=task.spec,
        task_dir=getattr(getattr(task, "folder", None), "root", run_dir),
        run_dir=episode_dir,
        sandboxes=lease,
        artifacts=sink,
        budget=Budget(deadline_sec=task.spec.timeouts.total, started_at=started),
        session=HarnessSession(
            episode_id=episode_id,
            gateway_url=gateway_url,
            token=session_token,
            model=model,
        ),
        agent_resources=agent_resources or EffectiveAgentResources(),
        trajectory_id=trajectory_id,
        transport=recording.transport,
        execution=recording.execution,
        blobs=recording.blobs,
        trajectory=recording,
        result=recording,
        seed=seed,
        proxy_url=proxy_url,
    )
    if phase_callback is not None:
        ctx.extras["phase_callback"] = phase_callback
    ctx.extras["logging_policy"] = logging_policy or LoggingPolicy()
    ctx.extras["verification_config"] = verification_config or VerificationConfig()

    try:
        verdict = await environment.run(task, ctx)
    except Exception as exc:  # every failure becomes a typed result, not a traceback
        if isinstance(exc, BudgetExceededError):
            ctx.limit_termination = LimitTermination(
                layer=exc.layer,  # type: ignore[arg-type]
                name=exc.limit,
                configured_value=exc.value,
                observed_value=exc.observed_value,
                reason=type(exc).__name__,
            )
        verdict = Verdict.failed(status_for(exc), exc, phase=_phase_of(exc))
        recording.execution.append(
            ExecutionFailure(
                episode_id=episode_id,
                phase=_phase_of(exc),
                component="episode",
                level="error",
                error_type=type(exc).__name__,
                message=str(exc),
            ),
            durable=True,
        )
    finally:
        await lease.release_all()
        if gateway is not None and session is not None:
            # Tokens die with their episode, so a leaked one is not a standing grant.
            gateway.close_session(session)

    finalize_started_at = datetime.now(UTC)
    finalize_started = time.monotonic()
    recording.execution.append(
        PhaseStarted(
            episode_id=episode_id,
            phase="finalize",
            component="episode",
        ),
        durable=True,
    )
    finalize_outcome = "succeeded"
    lock = _write_lock(ctx, task, provenance, seed=seed) if provenance else None
    if lock is not None:
        recording.write_lock(lock)
    try:
        recording.verify_blob_references()
    except Exception as exc:
        finalize_outcome = "failed"
        verdict = Verdict.failed(Status.ENV_ERROR, exc, phase="finalize")
        recording.execution.append(
            ExecutionFailure(
                episode_id=episode_id,
                phase="finalize",
                component="recording",
                level="error",
                error_type=type(exc).__name__,
                message=str(exc),
            ),
            durable=True,
        )
    finished_at = datetime.now(UTC)
    finalize_duration_ms = int((time.monotonic() - finalize_started) * 1000)
    ctx.phases.append(
        PhaseTiming(
            phase="finalize",
            started_at=finalize_started_at,
            finished_at=finished_at,
            duration_ms=finalize_duration_ms,
            outcome=finalize_outcome,
        )
    )
    record = ResultRecord(
        episode_id=episode_id,
        status=verdict.status,
        rewards=verdict.rewards,
        metrics=verdict.metrics,
        failure=(
            ResultFailureInfo(
                error_type=verdict.failure.error_class,
                message=verdict.failure.message,
                phase=verdict.failure.phase,
            )
            if verdict.failure is not None
            else None
        ),
        started_at=started_at,
        finished_at=finished_at,
        phases=tuple(ctx.phases),
    )
    recording.write_result(record)
    recording.execution.append(
        PhaseFinished(
            episode_id=episode_id,
            phase="finalize",
            component="episode",
            level="info" if finalize_outcome == "succeeded" else "error",
            outcome=finalize_outcome,
            duration_ms=finalize_duration_ms,
        ),
        durable=True,
    )
    duration = time.monotonic() - started

    return EpisodeResult(
        episode_id=episode_id,
        verdict=verdict,
        run_dir=episode_dir,
        duration_sec=duration,
        record=record,
        lock=lock,
    )


def _write_lock(
    ctx: EpisodeContext, task: Task, inputs: ProvenanceInputs, *, seed: int
) -> RunLock | None:
    """Bind the verdict to everything that produced it.

    An episode that died before provisioning has no image digest, and inventing one
    would be worse than having no lock: the point of the record is that every field in
    it was observed. Such a run simply cannot be reported, which is the correct outcome.
    """
    if ctx.image_digest is None:
        return None

    # What ran, not what was asked for. The harness resolves its version while installing,
    # which is after these inputs were built.
    if ctx.agent_version:
        inputs = replace(
            inputs, agent=inputs.agent.model_copy(update={"version": ctx.agent_version})
        )
    record = ctx.extras.get("verification_record")
    if isinstance(record, VerificationRecord):
        inputs = replace(inputs, judges=judge_provenance(record))
    lock = build_lock(
        inputs,
        ctx.spec,
        image_digest=ctx.image_digest,
        sandbox=ctx.sandbox_identity,
        assets=tuple(ctx.assets),
        kits=tuple(ctx.kits),
        ale_verify=ctx.extras.get("ale_verify_provenance"),
        termination=ctx.limit_termination,
        seed=seed,
        requires_core=getattr(getattr(task, "folder", None), "requires_core", None),
    )
    return lock


def _phase_of(error: BaseException) -> str | None:
    if isinstance(error, PhaseTimeoutError):
        return error.phase
    if isinstance(error, TaskError):
        return Phase.VERIFY.value
    return None
