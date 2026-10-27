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
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal
from urllib.parse import urlsplit

from ale.core.config import LoggingPolicy, SandboxRetentionConfig, VerificationConfig
from ale.core.environment import ArtifactSink, Budget, Environment, EpisodeContext, Phase
from ale.core.errors import (
    AgentError,
    AgentRefusalError,
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
from ale.core.sandbox import Identity, RetainedSandbox, Sandbox, SandboxRequest, SandboxRole
from ale.core.task import Task
from ale.core.taskspec import NetworkMode, OperatingSystem
from ale.core.trace import ExecutionFailure, PhaseFinished, PhaseStarted
from ale.core.verdict import Status, Verdict
from ale.run import artifact_files
from ale.run.assets import observe_task_assets
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, Limits, SessionRegistry
from ale.run.provenance import ProvenanceInputs, build_lock, judge_provenance
from ale.run.providers import ProviderRegistry
from ale.run.recording import EpisodeRecording
from ale.run.task_images import prepare_task_image, prepare_verifier_image
from ale.run.verification import judge_endpoints, open_judge_sessions
from ale_verify import JudgeInvocation

__all__ = ["EpisodeResult", "run_episode", "run_episode_with_retries"]

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


#: Outcomes another attempt must not replace. A completed zero is a score, and a limit
#: termination is the measured agent's own result: retrying it would grant a fresh budget.
_FINAL_STATUSES = frozenset({Status.COMPLETED, Status.TIMEOUT, Status.BUDGET_EXCEEDED})


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


async def run_episode_with_retries(
    execute: Callable[[], Awaitable[EpisodeResult]],
    *,
    retries: int,
    on_retry: Callable[[EpisodeResult, int, int], None] | None = None,
) -> EpisodeResult:
    """Keep one final episode result and archive superseded failed executions."""
    if retries < 0:
        raise ValueError("episode retries must be non-negative")
    for attempt in range(retries + 1):
        result = await execute()
        failure = result.record.failure
        if (
            result.record.status in _FINAL_STATUSES
            or attempt == retries
            or (
                failure is not None
                and failure.error_type
                in {
                    "VerificationInfrastructureError",
                    "VerificationConfigurationError",
                    "VerificationProviderError",
                    "VerificationAdapterError",
                    "VerificationVerdictError",
                }
            )
        ):
            return result
        history = result.run_dir / "attempts"
        archive = history / str(attempt + 1)
        archive.mkdir(parents=True, exist_ok=False)
        # Move each complete record with its relative blobs and artifacts intact.
        for path in result.run_dir.iterdir():
            if path != history:
                path.rename(archive / path.name)
        if on_retry is not None:
            on_retry(result, attempt + 1, retries)
    raise AssertionError("episode retry loop did not return")


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
        self.retained: list[tuple[_LeaseEntry, SandboxOutcome]] = []

    async def acquire(self, request: SandboxRequest) -> Sandbox:
        roles, policy = self._policy(request)
        provider = self.providers.get(request.image_kind)
        sandbox = await provider.create(request.model_copy(update={"retention": policy}))
        self.live.append(_LeaseEntry(sandbox, provider.name, roles, policy))
        return sandbox

    async def adopt(
        self, request: SandboxRequest, attach: Callable[[SandboxRequest], Awaitable[Sandbox]]
    ) -> Sandbox:
        """Take over a retained Sandbox: this episode's retention decides its fate."""
        roles, policy = self._policy(request)
        sandbox = await attach(request.model_copy(update={"retention": policy}))
        self.live.append(_LeaseEntry(sandbox, sandbox.allocation.provider, roles, policy))
        return sandbox

    def _policy(
        self, request: SandboxRequest
    ) -> tuple[tuple[Literal["solver", "verifier"], ...], Literal["destroy", "keep"]]:
        if request.role is SandboxRole.SHARED:
            keep = self.retention.solver == "keep" or self.retention.verifier == "keep"
            return ("solver", "verifier"), "keep" if keep else "destroy"
        if request.role is SandboxRole.VERIFIER:
            return ("verifier",), self.retention.verifier
        return ("solver",), self.retention.solver

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

    async def finalize(self, *, allow_retention: bool = True) -> None:
        for entry in list(self.live):
            if not allow_retention or entry.requested != "keep" or entry.sanitation != "succeeded":
                outcome = (
                    "destroyed"
                    if not allow_retention or entry.requested == "destroy"
                    else "retention-failed"
                )
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
                outcome = _retained_outcome(entry, retained)
                self.outcomes.append(outcome)
                self.retained.append((entry, outcome))

    async def destroy_retained(self) -> None:
        """Undo retention if terminal recording fails after Sandbox finalization."""
        failure: RetentionFinalizationError | None = None
        for entry, outcome in self.retained:
            self.outcomes.remove(outcome)
            try:
                await self._destroy(entry, outcome="destroyed")
            except RetentionFinalizationError as exc:
                failure = failure or exc
        self.retained.clear()
        if failure is not None:
            raise failure

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
        gateway_port=(
            urlsplit(entry.sandbox.request.gateway_url).port
            if entry.sandbox.request.gateway_url
            else None
        ),
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

    async def collect(self, sandbox: Sandbox, source: str, name: str) -> None:
        if not self.enabled:
            return
        logger = logging.getLogger("ale.execution")
        python = "python.exe" if sandbox.request.os is OperatingSystem.WINDOWS else "python3"
        script = Path(artifact_files.__file__).read_text()
        path_type = (
            PureWindowsPath if sandbox.request.os is OperatingSystem.WINDOWS else PurePosixPath
        )
        base = path_type(source).parent
        while any(char in str(base) for char in "*?["):
            base = base.parent
        # A separate directory for each declaration prevents basename collisions and
        # keeps snapshot.json reserved for the inventory, even for that literal filename.
        batch = self.root / f"{len(self.collected):04d}"
        self.collected.append(name)
        captured = {entry["source"] for entry in self.entries}
        try:
            probe = await sandbox.exec(
                [python, "-c", script, "inspect", source], identity=Identity.FRAMEWORK
            )
            if not probe.ok:
                raise OSError(probe.stderr or "artifact inspection failed")
            details = json.loads(probe.stdout)
            for reason in details["skipped"]:
                logger.warning("artifact skipped: %s", reason)
            for item in details["entries"]:
                remote = str(item["source"])
                try:
                    relative = path_type(remote).relative_to(base)
                    if ".." in relative.parts or path_type(remote) == path_type(remote).parent:
                        raise ValueError("artifact path escapes its declaration")
                    if remote in captured:
                        continue
                    target = batch.joinpath(*relative.parts)
                    kind = item["kind"]
                    if kind == "directory":
                        target.mkdir(parents=True, exist_ok=True)
                        identity, size = None, 0
                    elif kind == "file":
                        target.parent.mkdir(parents=True, exist_ok=True)
                        digest = hashlib.sha256()
                        size = 0
                        with target.open("wb") as output:

                            async def receive(stream: str, data: bytes, digest=digest) -> None:
                                nonlocal size
                                if stream == "stdout":
                                    output.write(data)
                                    digest.update(data)
                                    size += len(data)

                            copied = await sandbox.exec(
                                [python, "-c", script, "read", remote],
                                identity=Identity.FRAMEWORK,
                                output_sink=receive,
                            )
                        if not copied.ok:
                            target.unlink(missing_ok=True)
                            raise OSError(copied.stderr or "artifact read failed")
                        identity = f"sha256:{digest.hexdigest()}"
                    else:
                        raise ValueError(f"unsupported artifact type: {kind}")
                    self.entries.append(
                        {
                            "source": remote,
                            "kind": kind,
                            "stored_path": target.relative_to(self.root).as_posix(),
                            "mode": int(item["mode"]),
                            "content_identity": identity,
                            "size_bytes": size,
                        }
                    )
                    captured.add(remote)
                except Exception as exc:
                    logger.warning("artifact skipped: %s: %s", remote, exc)
            self._write_manifest()
        except Exception as exc:
            logger.warning("artifact skipped: %s: %s", source, exc)

    def path(self, name: str) -> Path:
        return self.root / name

    async def restore(self, sandbox: Sandbox) -> None:
        if not self.enabled or not self.entries:
            return
        python = "python.exe" if sandbox.request.os is OperatingSystem.WINDOWS else "python3"
        roots: list[PurePosixPath | PureWindowsPath] = []
        path_type = (
            PureWindowsPath if sandbox.request.os is OperatingSystem.WINDOWS else PurePosixPath
        )
        for entry in sorted(
            self.entries, key=lambda item: len(path_type(str(item["source"])).parts)
        ):
            source = str(entry["source"])
            target = self.root / str(entry["stored_path"])
            try:
                path = path_type(source)
                clear = not any(path == root or root in path.parents for root in roots)
                if clear:
                    roots.append(path)
                prepared = await sandbox.exec(
                    [
                        python,
                        "-c",
                        (
                            "import os,pathlib,shutil,sys; p=pathlib.Path(sys.argv[1]); "
                            "directory=sys.argv[2]=='directory'; clear=sys.argv[3]=='True'; "
                            "shutil.rmtree(p) if p.is_dir() and not p.is_symlink() and clear "
                            "else p.unlink() if p.is_symlink() or (p.exists() and clear) "
                            "else None; "
                            "(p if directory else p.parent).mkdir(parents=True,exist_ok=True)"
                        ),
                        source,
                        str(entry["kind"]),
                        str(clear),
                    ],
                    identity=Identity.FRAMEWORK,
                )
                if not prepared.ok:
                    raise OSError(prepared.stderr or "cannot prepare artifact destination")
                if entry["kind"] == "file":
                    await sandbox.write_file(
                        source, target.read_bytes(), identity=Identity.FRAMEWORK
                    )
                if sandbox.request.os is OperatingSystem.LINUX:
                    mode = await sandbox.exec(
                        ["chmod", f"{int(entry['mode']):o}", "--", source],
                        identity=Identity.FRAMEWORK,
                    )
                    if not mode.ok:
                        raise OSError(mode.stderr or "cannot restore artifact mode")
            except Exception as exc:
                logging.getLogger("ale.execution").warning(
                    "artifact restore skipped: %s: %s", source, exc
                )

    def cleanup(self) -> None:
        return None

    def _write_manifest(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "entries": self.entries, "retained": True}
        target = self.root / "snapshot.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(target)


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
    collect_artifacts: bool = False,
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
    destroy_failed_sandboxes: bool = False,
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
    episode_id = episode_id or f"{task.spec.name}-{uuid.uuid4().hex[:8]}"
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
            provider_web_tools=task.spec.network.mode is NetworkMode.OPEN,
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
    judge_sessions = (
        open_judge_sessions(
            gateway,
            verification_config,
            episode_id=episode_id,
            trace=recording.transport,
        )
        if gateway is not None and verification_config is not None
        else {}
    )

    sink = _Artifacts(episode_dir, enabled=collect_artifacts)
    lease = _Lease(providers, sandbox_retention or SandboxRetentionConfig())
    started_at = datetime.now(UTC)
    started = time.monotonic()
    ctx = EpisodeContext(
        episode_id=episode_id,
        spec=task.spec,
        task_dir=task.folder.root if task.folder is not None else run_dir,
        run_dir=episode_dir,
        sandboxes=lease,
        artifacts=sink,
        budget=Budget(deadline_sec=task.spec.timeouts.total, started_at=started),
        session=HarnessSession(
            episode_id=episode_id,
            gateway_url=gateway_url,
            token=session_token,
            model=model,
            os=task.spec.os,
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
        phase_callback=phase_callback,
        limit_refusal=lambda: _gateway_refusal(session),
        logging_policy=logging_policy or LoggingPolicy(),
        verification_config=verification_config or VerificationConfig(),
        judge_tokens={kind: opened.token for kind, opened in judge_sessions.items()},
    )
    verdict: Verdict | None = None
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
        phase = _phase_of(exc, ctx.failure_phase)
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
        if gateway is not None:
            # Tokens die with their episode, so a leaked one is not a standing grant.
            for opened in (session, *judge_sessions.values()):
                if opened is not None:
                    gateway.close_session(opened)
        elif session_registry is not None and session is not None:
            session_registry.close(session)
        finalization_error: RetentionFinalizationError | None = None
        try:
            await lease.finalize(
                allow_retention=verdict is not None
                and verdict.status is not Status.TIMEOUT
                and (verdict.status is Status.COMPLETED or not destroy_failed_sandboxes)
            )
        except RetentionFinalizationError as exc:
            finalization_error = exc
        try:
            await lease.release_all()
        except RetentionFinalizationError as exc:
            finalization_error = finalization_error or exc
        ctx.sandbox_outcomes = tuple(lease.outcomes)
        sink.cleanup()
        if (
            finalization_error is not None
            and verdict is not None
            and verdict.status is Status.COMPLETED
        ):
            verdict = Verdict.failed(Status.ENV_ERROR, finalization_error, phase="teardown")

    assert verdict is not None
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
    lock = None
    try:
        recording.verify_blob_references()
        lock = _write_lock(ctx, task, provenance, seed=seed) if provenance else None
        if lock is not None:
            recording.write_lock(lock)
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
    if destroy_failed_sandboxes and verdict.status is not Status.COMPLETED:
        try:
            await lease.destroy_retained()
        except RetentionFinalizationError as exc:
            verdict = Verdict.failed(Status.ENV_ERROR, exc, phase="teardown")
        ctx.sandbox_outcomes = tuple(lease.outcomes)
        if lock is not None:
            lock = lock.model_copy(update={"sandbox_outcomes": ctx.sandbox_outcomes})
            recording.write_lock(lock)
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
        continuation=ctx.agent_run.continuation if ctx.agent_run is not None else None,
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
    invocations = tuple(item for item in ctx.judge_invocations if isinstance(item, JudgeInvocation))
    if invocations:
        endpoints = judge_endpoints(ctx.verification_config)
        inputs = replace(inputs, judges=judge_provenance(invocations, endpoints))
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
        non_verifier_digest=task.non_verifier_digest,
        prepared_image=ctx.prepared_image,
        sandbox=ctx.sandbox_identity,
        asset=asset,
        verifier_resolved_image=ctx.verifier_resolved_image,
        verifier_allocation=ctx.verifier_resource_allocation,
        prepared_verifier_image=ctx.prepared_verifier_image,
        ale_verify=ctx.ale_verify_provenance,
        termination=ctx.limit_termination,
        sandbox_outcomes=ctx.sandbox_outcomes,
        resumed_from=ctx.resumed_from,
        seed=seed,
    )
    return lock


def _gateway_refusal(session: GatewaySession | None) -> LimitTermination | None:
    refusal = session.refusal if session is not None else None
    if refusal is None:
        return None
    return LimitTermination(
        layer="gateway",
        name=refusal.limit,
        configured_value=refusal.value,
        observed_value=refusal.observed,
        reason="the Gateway refused a model call",
    )


def _phase_of(error: BaseException, recorded_phase: object = None) -> str | None:
    if isinstance(error, PhaseTimeoutError):
        return error.phase
    if isinstance(recorded_phase, str):
        return recorded_phase
    if isinstance(error, TaskError):
        return Phase.VERIFY.value
    return None
