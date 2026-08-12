"""Running one episode.

The smallest thing that can produce a verdict: lease a sandbox, hand the task to an
environment, map whatever comes back to the shared status taxonomy, and write the
episode's traces and artifacts.

Error mapping lives here rather than in the environment on purpose. An environment
should raise what actually went wrong and let one place decide what that means for a
result, so `task_error` means the same thing across every domain.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ale.core.config import LoggingPolicy, SandboxRetentionConfig, VerificationConfig
from ale.core.environment import ArtifactSink, Budget, Environment, EpisodeContext, Phase
from ale.core.errors import (
    AgentError,
    AgentRefusalError,
    ArtifactTransferError,
    BudgetExceededError,
    EnvironmentError_,
    PhaseTimeoutError,
    RetentionFinalizationError,
    TaskError,
)
from ale.core.harness import EffectiveAgentResources, HarnessSession
from ale.core.lock import AssetProvenance, LimitTermination, RunLock
from ale.core.result import FailureInfo as ResultFailureInfo
from ale.core.result import PhaseTiming, ResultRecord, SandboxOutcome
from ale.core.sandbox import RetainedSandbox, Sandbox, SandboxRequest, SandboxRole
from ale.core.task import Task
from ale.core.trace import ExecutionFailure, PhaseFinished, PhaseStarted
from ale.core.verdict import Status, Verdict
from ale.run.assets import observe_task_assets
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, Limits, SessionRegistry
from ale.run.provenance import ProvenanceInputs, build_lock, judge_provenance
from ale.run.providers import ProviderRegistry
from ale.run.recording import EpisodeRecording
from ale.run.task_images import prepare_task_image, prepare_verifier_image
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


@dataclass
class _LeaseEntry:
    sandbox: Sandbox
    provider_name: str
    roles: tuple[Literal["solver", "verifier"], ...]
    requested: Literal["destroy", "keep"]
    sanitation: Literal["not-run", "succeeded", "failed"] = "not-run"
    sanitation_reason: str | None = None


class _Lease:
    """Hands out sandboxes and guarantees they are reaped.

    An environment may hold several at once — a grader and a solver, say — so the lease
    tracks them rather than assuming one.
    """

    def __init__(self, providers: ProviderRegistry, retention: SandboxRetentionConfig) -> None:
        self.providers = providers
        self.retention = retention
        self.live: list[_LeaseEntry] = []
        self.outcomes: list[SandboxOutcome] = []

    async def acquire(self, request: SandboxRequest) -> Sandbox:
        roles: tuple[Literal["solver", "verifier"], ...]
        if request.role is SandboxRole.SHARED:
            roles = ("solver", "verifier")
            policy = (
                "keep"
                if self.retention.solver == "keep" or self.retention.verifier == "keep"
                else "destroy"
            )
        elif request.role is SandboxRole.VERIFIER:
            roles = ("verifier",)
            policy = self.retention.verifier
        else:
            roles = ("solver",)
            policy = self.retention.solver
        request = request.model_copy(update={"retention": policy})
        provider = self.providers.get(request.image_kind)
        sandbox = await provider.create(request)
        self.live.append(_LeaseEntry(sandbox, provider.name, roles, policy))
        return sandbox

    async def release(self, sandbox: Sandbox) -> None:
        entry = self._find(sandbox)
        if entry is None:
            return
        if entry.requested == "keep":
            if entry.sanitation == "succeeded":
                return
            await self._destroy(entry, outcome="retention-failed")
            return
        await self._destroy(entry, outcome="destroyed")

    def mark_sanitized(
        self, sandbox: Sandbox, *, succeeded: bool, reason: str | None = None
    ) -> None:
        entry = self._find(sandbox)
        if entry is not None:
            entry.sanitation = "succeeded" if succeeded else "failed"
            entry.sanitation_reason = reason

    async def finalize(self) -> None:
        for entry in list(self.live):
            if entry.requested != "keep" or entry.sanitation != "succeeded":
                outcome = "destroyed" if entry.requested == "destroy" else "retention-failed"
                await self._destroy(entry, outcome=outcome)
                continue
            try:
                retained = await entry.sandbox.retain(
                    roles=entry.roles,
                    reason="requested by sandbox_retention run configuration",
                )
            except Exception as exc:
                entry.sanitation_reason = str(exc)
                await self._destroy(entry, outcome="retention-failed")
            else:
                entry.sandbox.release_resources()
                self.live.remove(entry)
                self.outcomes.append(_retained_outcome(entry, retained))

    async def _destroy(
        self,
        entry: _LeaseEntry,
        *,
        outcome: Literal["destroyed", "retention-failed"],
    ) -> None:
        error: Exception | None = None
        try:
            await entry.sandbox.destroy()
        except Exception as exc:
            error = exc
            entry.sanitation_reason = str(exc)
        finally:
            entry.sandbox.release_resources()
            if entry in self.live:
                self.live.remove(entry)
        self.outcomes.append(
            SandboxOutcome(
                roles=entry.roles,
                requested=entry.requested,
                outcome=outcome,
                provider=entry.provider_name,
                reason=entry.sanitation_reason,
            )
        )
        if error is not None:
            raise RetentionFinalizationError(
                f"could not destroy sandbox {entry.sandbox.sandbox_id}: {error}"
            ) from error

    async def release_all(self) -> None:
        failure: RetentionFinalizationError | None = None
        for entry in list(self.live):
            try:
                await self._destroy(entry, outcome="destroyed")
            except RetentionFinalizationError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    def _find(self, sandbox: Sandbox) -> _LeaseEntry | None:
        return next((entry for entry in self.live if entry.sandbox is sandbox), None)


def _retained_outcome(entry: _LeaseEntry, retained: RetainedSandbox) -> SandboxOutcome:
    return SandboxOutcome(
        roles=entry.roles,
        requested=entry.requested,
        outcome="retained",
        provider=retained.provider,
        handle=retained.handle,
        reason=retained.reason,
        cleanup_command=retained.cleanup_command,
        gpu_devices=retained.gpu_devices,
    )


@dataclass
class _Artifacts(ArtifactSink):
    """Immutable solver evidence, collected only when requested."""

    run_dir: Path
    enabled: bool = True
    collected: list[str] = field(default_factory=list)
    entries: list[dict[str, object]] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.run_dir / "artifacts"

    async def collect(self, sandbox: Sandbox, source: str, name: str) -> Path:
        if not self.enabled:
            raise ArtifactTransferError("artifact collection is disabled")
        probe = await sandbox.exec(
            [
                "python3",
                "-c",
                (
                    "import json,os,stat,sys; s=os.lstat(sys.argv[1]); "
                    "print(json.dumps({'kind':'symlink' if stat.S_ISLNK(s.st_mode) else "
                    "'file' if stat.S_ISREG(s.st_mode) else 'directory' if "
                    "stat.S_ISDIR(s.st_mode) else 'special','mode':stat.S_IMODE(s.st_mode)}))"
                ),
                source,
            ]
        )
        if not probe.ok:
            raise ArtifactTransferError(f"declared artifact is missing: {source}")
        try:
            details = json.loads(probe.stdout)
        except json.JSONDecodeError as exc:
            raise ArtifactTransferError(f"could not inspect artifact {source}") from exc
        kind = details.get("kind")
        if kind not in {"file", "directory"}:
            raise ArtifactTransferError(f"artifact {source} is an unsupported {kind}")

        stored = name
        if (self.root / stored).exists():
            stored = f"{len(self.entries):04d}-{name}"
        target = self.root / stored
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if kind == "file":
                data = await sandbox.read_file(source)
                target.write_bytes(data)
                identity = f"sha256:{hashlib.sha256(data).hexdigest()}"
                size = len(data)
            else:
                target.mkdir()
                await sandbox.download_dir(source, str(target))
                identity, size = _artifact_tree_identity(target)
        except Exception as exc:
            raise ArtifactTransferError(f"could not capture artifact {source}: {exc}") from exc
        self.entries.append(
            {
                "source": source,
                "kind": kind,
                "stored_path": stored,
                "mode": int(details["mode"]),
                "content_identity": identity,
                "size_bytes": size,
            }
        )
        self.collected.append(name)
        self._write_manifest()
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
        return self.root / name

    async def restore(self, sandbox: Sandbox) -> None:
        for entry in self.entries:
            source = str(entry["source"])
            target = self.root / str(entry["stored_path"])
            await sandbox.exec(["rm", "-rf", "--", source])
            parent = str(Path(source).parent)
            created = await sandbox.exec(["mkdir", "-p", "--", parent])
            if not created.ok:
                raise ArtifactTransferError(f"could not create artifact parent {parent}")
            try:
                if entry["kind"] == "file":
                    await sandbox.write_file(source, target.read_bytes())
                else:
                    await sandbox.upload_dir(str(target), source)
                mode = await sandbox.exec(["chmod", f"{int(entry['mode']):o}", "--", source])
                if not mode.ok:
                    raise ArtifactTransferError(f"could not restore artifact mode: {source}")
            except ArtifactTransferError:
                raise
            except Exception as exc:
                raise ArtifactTransferError(f"could not restore artifact {source}: {exc}") from exc

    def cleanup(self) -> None:
        return None

    def _write_manifest(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "entries": self.entries, "retained": True}
        target = self.root / "snapshot.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(target)


def _artifact_tree_identity(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ArtifactTransferError(f"artifact directory contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ArtifactTransferError(f"artifact directory contains a special file: {relative}")
        data = path.read_bytes()
        size += len(data)
        digest.update(relative.encode() + b"\0" + data)
    return f"sha256:{digest.hexdigest()}", size


async def run_episode(
    task: Task,
    environment: Environment,
    providers: ProviderRegistry,
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
    session_registry: SessionRegistry | None = None,
    limits: Limits | None = None,
    allowed_hosts: frozenset[str] = frozenset(),
    agent_resources: EffectiveAgentResources | None = None,
    episode_id: str | None = None,
    trajectory_id: str | None = None,
    phase_callback: Callable[[Phase], None] | None = None,
    logging_policy: LoggingPolicy | None = None,
    verification_config: VerificationConfig | None = None,
    sandbox_retention: SandboxRetentionConfig | None = None,
    authentication: Literal["api-key", "subscription"] = "api-key",
    profile_slot_id: str | None = None,
) -> EpisodeResult:
    """Administer one task and return its verdict.

    Never raises for an episode that merely failed: a failure is a result with a status,
    and the caller decides what to do with it.
    """
    task.asset_observation = observe_task_assets(task)
    if task.prepared_image is None:
        task.prepared_image = await prepare_task_image(task, providers)
    if task.prepared_verifier_image is None:
        task.prepared_verifier_image = await prepare_verifier_image(task, providers)
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
    if gateway is not None or session_registry is not None:
        candidate = GatewaySession(
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
        )
        session = (
            gateway.open_session(
                candidate,
                trace=recording.transport,
            )
            if gateway is not None
            else session_registry.open(candidate)  # type: ignore[union-attr]
        )
        session_token = session.token

    sink = _Artifacts(episode_dir, enabled=collect_artifacts)
    lease = _Lease(providers, sandbox_retention or SandboxRetentionConfig())
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
            authentication=authentication,
            profile_slot_id=profile_slot_id,
        ),
        agent_resources=agent_resources or EffectiveAgentResources(),
        task_source=task.source,
        trajectory_id=trajectory_id,
        transport=recording.transport,
        execution=recording.execution,
        blobs=recording.blobs,
        trajectory=recording,
        result=recording,
        seed=seed,
        proxy_url=proxy_url,
        allowed_hosts=allowed_hosts,
        prepared_image=task.prepared_image,
        prepared_verifier_image=task.prepared_verifier_image,
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
        phase = _phase_of(exc, ctx.extras.get("failure_phase"))
        verdict = Verdict.failed(status_for(exc), exc, phase=phase)
        recording.execution.append(
            ExecutionFailure(
                episode_id=episode_id,
                phase=phase,
                component="episode",
                level="error",
                error_type=type(exc).__name__,
                message=str(exc),
            ),
            durable=True,
        )
    finally:
        if gateway is not None and session is not None:
            # Tokens die with their episode, so a leaked one is not a standing grant.
            gateway.close_session(session)
        elif session_registry is not None and session is not None:
            session_registry.close(session)
        finalization_error: RetentionFinalizationError | None = None
        try:
            await lease.finalize()
        except RetentionFinalizationError as exc:
            finalization_error = exc
        try:
            await lease.release_all()
        except RetentionFinalizationError as exc:
            finalization_error = finalization_error or exc
        ctx.extras["sandbox_outcomes"] = tuple(lease.outcomes)
        sink.cleanup()
        if finalization_error is not None and verdict.status is Status.COMPLETED:
            verdict = Verdict.failed(Status.ENV_ERROR, finalization_error, phase="teardown")

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
        sandboxes=tuple(lease.outcomes),
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
    if ctx.resolved_image is None or ctx.resource_allocation is None:
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
    observed_asset = task.asset_observation
    asset = (
        AssetProvenance(
            repository=observed_asset.repository,
            task_path=observed_asset.task_path,
            commit=observed_asset.commit,
            dirty=observed_asset.dirty,
        )
        if observed_asset is not None
        else None
    )
    lock = build_lock(
        inputs,
        ctx.spec,
        resolved_image=ctx.resolved_image,
        allocation=ctx.resource_allocation,
        task_digest=task.task_digest,
        prepared_image=ctx.prepared_image,
        sandbox=ctx.sandbox_identity,
        asset=asset,
        verifier_resolved_image=ctx.extras.get("verifier_resolved_image"),
        verifier_allocation=ctx.extras.get("verifier_resource_allocation"),
        prepared_verifier_image=ctx.prepared_verifier_image,
        ale_verify=ctx.extras.get("ale_verify_provenance"),
        termination=ctx.limit_termination,
        sandbox_outcomes=ctx.extras.get("sandbox_outcomes", ()),
        seed=seed,
    )
    return lock


def _phase_of(error: BaseException, recorded_phase: object = None) -> str | None:
    if isinstance(error, PhaseTimeoutError):
        return error.phase
    if isinstance(recorded_phase, str):
        return recorded_phase
    if isinstance(error, TaskError):
        return Phase.VERIFY.value
    return None
