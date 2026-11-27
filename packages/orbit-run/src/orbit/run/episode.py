"""Running one episode.

The smallest thing that can produce a verdict: lease a sandbox, hand the task to the
episode protocol, map whatever comes back to the shared status taxonomy, and write the
episode's traces and artifacts.

Error mapping lives here rather than in the protocol on purpose. The protocol should
raise what actually went wrong and let one place decide what that means for a result,
so `task_error` means the same thing for every Task.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from orbit.core.capability import unmet
from orbit.core.config import LoggingPolicy, SandboxRetentionConfig, VerificationConfig
from orbit.core.episode import ArtifactSink, Budget, EpisodeContext, EpisodeContinuation, Phase
from orbit.core.errors import (
    AgentError,
    AgentRefusalError,
    BudgetExceededError,
    ConfigError,
    EnvironmentError_,
    MemoryExhaustedError,
    PhaseTimeoutError,
    ProviderCapabilityError,
    ResourceExhaustedError,
    RetentionFinalizationError,
    SandboxHandOff,
    StorageExhaustedError,
    TaskDefinitionError,
    TaskError,
)
from orbit.core.harness import EffectiveAgentResources, HarnessSession
from orbit.core.ids import task_component
from orbit.core.lock import AssetProvenance, LimitTermination, RunLock
from orbit.core.result import FailureInfo as ResultFailureInfo
from orbit.core.result import PhaseTiming, ResultRecord, SandboxOutcome
from orbit.core.sandbox import (
    DesktopResolution,
    Identity,
    Provider,
    RetainedSandbox,
    Sandbox,
    SandboxRequest,
    SandboxRole,
)
from orbit.core.task import ResolvedVariables, Stage, Task
from orbit.core.taskspec import (
    NetworkMode,
    NetworkPolicy,
    OperatingSystem,
    Resources,
    TaskSpec,
    VerificationMode,
)
from orbit.core.trace import ExecutionFailure, PhaseFinished, PhaseStarted
from orbit.core.verdict import FINAL_STATUSES, Status, Verdict
from orbit.run import artifact_files, guestd
from orbit.run.assets import observe_task_assets
from orbit.run.gateway.server import Gateway
from orbit.run.gateway.session import GatewaySession, Limits, SessionRegistry
from orbit.run.protocol.episode_protocol import SETUP_TIMEOUT_SEC, EpisodeProtocol, HandedOff
from orbit.run.provenance import ProvenanceInputs, build_lock, judge_provenance
from orbit.run.providers import ProviderRegistry
from orbit.run.recording import EpisodeRecording, Redactor
from orbit.run.task_images import (
    prepare_task_image,
    prepare_validation_image,
    prepare_verifier_image,
    resolve_image_kinds,
)
from orbit.run.verification import judge_endpoints, open_judge_sessions
from orbit_verify import JudgeInvocation

__all__ = ["EpisodeResult", "check_retention", "run_episode", "run_episode_with_retries"]

#: Which status an error class implies. Order matters: the first match wins, so
#: subclasses must precede their bases.
_STATUS_BY_ERROR: tuple[tuple[type[BaseException], Status], ...] = (
    (PhaseTimeoutError, Status.TIMEOUT),
    (BudgetExceededError, Status.BUDGET_EXCEEDED),
    (AgentRefusalError, Status.REFUSED),
    (AgentError, Status.AGENT_ERROR),
    (TaskError, Status.TASK_ERROR),
    (MemoryExhaustedError, Status.MEMORY_EXHAUSTED),
    (StorageExhaustedError, Status.STORAGE_EXHAUSTED),
    (EnvironmentError_, Status.ENV_ERROR),
)


def status_for(error: BaseException, *, agents: bool = True) -> Status:
    """Map a raised error to the status a result should carry.

    A Sandbox lost to memory or storage is the agent's result only where ``agents``, that
    is, the Sandbox the agent ran in, from its phase on; elsewhere the code that ran there
    was the Task's.
    """
    if isinstance(error, ResourceExhaustedError) and not agents:
        return Status.TASK_ERROR
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
            result.record.status in FINAL_STATUSES
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
    sandbox: Sandbox | None
    """None for a snapshot an earlier process of the episode saved, whose Sandbox it was."""
    provider: Provider
    roles: tuple[Literal["solver", "verifier"], ...]
    requested: Literal["destroy", "keep", "snapshot"]
    sanitation: Literal["not-run", "succeeded", "failed"] = "not-run"
    sanitation_reason: str | None = None
    saved: RetainedSandbox | None = None
    """The snapshot of its disk a verifier, a validation round and the next turn's solver,
    or the solver a move boots, boots from, once saved; its Sandbox is gone then, and this is
    what remains of it."""
    booted_from: _LeaseEntry | None = None
    """The saved solver whose snapshot this Sandbox booted from, which outlasts it: a snapshot
    verifier's, a validation round's verifier's or next solver's, or a moved solver's."""
    carried: bool = False
    """Saved for the next solver, after a validation round or for a move: its snapshot holds
    the agent's state in the middle of its episode."""


class _Lease:
    """Hands out sandboxes and guarantees they are reaped.

    An episode may lease several — a solver and a separate verifier, say — so the lease
    tracks them rather than assuming one. Each comes from the Provider the Run selected for
    its role: ``solver`` by provider id, and ``verifier`` for a verifier of its own; one
    booted from a snapshot the lease holds comes from the Provider that saved it. A solver
    saved for a snapshot verifier or a validation round is held as its snapshot, which
    outlasts the Sandboxes booted from it. A snapshot of a move or a round can pass to and from
    another process of the same episode, whose lease owns it from then on.
    """

    def __init__(
        self,
        providers: ProviderRegistry,
        retention: SandboxRetentionConfig,
        desktop_resolution: DesktopResolution = DesktopResolution(),
        *,
        solver: str,
        verifier: str | None = None,
    ) -> None:
        self.providers = providers
        self.solver = solver
        self.verifier = verifier
        self.retention = retention
        self.desktop_resolution = desktop_resolution
        self.live: list[_LeaseEntry] = []
        self.undestroyed: list[_LeaseEntry] = []
        """Sandboxes that could not be destroyed, which may still run on a saved snapshot."""
        self.outcomes: list[SandboxOutcome] = []
        self.retained: list[tuple[_LeaseEntry, SandboxOutcome]] = []

    async def acquire(self, request: SandboxRequest, *, validation: bool = False) -> Sandbox:
        roles, policy = self._policy(request)
        if validation:
            # A validation round's verifier: retention concerns the final verification only.
            policy = "destroy"
        # A separate verifier mounts views of its solver's snapshot's volumes.
        saved = request.snapshot or request.volumes_from
        origin = self._saved(saved) if saved is not None else None
        if origin is not None:
            # Only the Provider that saved a snapshot holds it.
            provider = origin.provider
        else:
            selected = self.verifier if request.role is SandboxRole.VERIFIER else self.solver
            if selected is None:
                raise ProviderCapabilityError(
                    f"no Provider was selected for a {request.role} Sandbox"
                )
            provider = self.providers.get(selected)
        sandbox = await provider.create(self._configured(request, policy))
        self.live.append(_LeaseEntry(sandbox, provider, roles, policy, booted_from=origin))
        return sandbox

    async def serves_views(
        self, sandbox: Sandbox, verifier: SandboxRequest, *, saved: bool
    ) -> bool:
        """Whether the Provider of ``sandbox``, a solver, serves ``verifier``, which mounts
        views of the volumes the solver's snapshot holds, and, unless the solver is ``saved``
        anyway, saves it once more for it. Otherwise its volumes are copied."""
        entry = self._find(sandbox)
        if entry is None or not any(
            profile.volumes and not unmet(profile, verifier.needs)
            for profile in await entry.provider.profiles()
        ):
            return False
        if not saved:
            needs = sandbox.request.needs
            if await entry.provider.unserved(
                needs.model_copy(update={"snapshot": True, "saves": needs.saves + 1}),
                user=sandbox.request.user,
                role=sandbox.request.role,
            ):
                return False
        return not await entry.provider.unserved(
            verifier.needs, user=verifier.user, role=SandboxRole.VERIFIER
        )

    async def adopt(
        self, request: SandboxRequest, attach: Callable[[SandboxRequest], Awaitable[Sandbox]]
    ) -> Sandbox:
        """Take over a retained Sandbox: this episode's retention decides its fate."""
        roles, policy = self._policy(request)
        sandbox = await attach(self._configured(request, policy))
        provider = self.providers.get(sandbox.allocation.provider)
        self.live.append(_LeaseEntry(sandbox, provider, roles, policy))
        return sandbox

    def _configured(
        self, request: SandboxRequest, policy: Literal["destroy", "keep", "snapshot"]
    ) -> SandboxRequest:
        """The request with what the Run decides for every Sandbox it leases."""
        return request.model_copy(
            update={
                "retention": policy,
                "desktop_resolution": self.desktop_resolution,
            }
        )

    def _policy(
        self, request: SandboxRequest
    ) -> tuple[tuple[Literal["solver", "verifier"], ...], Literal["destroy", "keep", "snapshot"]]:
        if request.role is SandboxRole.SHARED:
            # The solver's policy wins: the one Sandbox holds the agent's work.
            policy = self.retention.solver
            if policy == "destroy":
                policy = self.retention.verifier
            return ("solver", "verifier"), policy
        if request.role is SandboxRole.VERIFIER:
            return ("verifier",), self.retention.verifier
        return ("solver",), self.retention.solver

    async def release(self, sandbox: Sandbox) -> None:
        entry = self._find(sandbox)
        if entry is None:
            return
        if entry.saved is not None:
            await self._release_saved(entry)
            return
        if entry.requested != "destroy":
            if entry.sanitation == "succeeded":
                return
            await self._destroy(entry, outcome="retention-failed")
            return
        await self._destroy(entry, outcome="destroyed")

    async def release_snapshot(self, handle: str) -> None:
        entry = self._saved(handle)
        if entry is not None:
            await self._release_saved(entry)

    async def _release_saved(self, entry: _LeaseEntry) -> None:
        # Its snapshot is what remains, kept only as the snapshot its retention asks for, and
        # kept while a Sandbox booted from it lives, which finalize then reaps first.
        if entry.requested != "snapshot" and not self._booted_from(entry):
            await self._destroy_saved(entry)

    def hand_off(self, handle: str) -> tuple[str, RetainedSandbox]:
        entry = self._saved(handle)
        assert entry is not None and entry.saved is not None, "only a leased snapshot passes on"
        self.live.remove(entry)
        return entry.provider.id, entry.saved

    def take_over(self, provider: str, snapshot: RetainedSandbox) -> None:
        """Own ``snapshot``, which the Provider ``provider`` names holds for this episode from
        an earlier process, as a snapshot this lease saved for a move: a Sandbox leased from
        it comes from that Provider, and it goes as such a snapshot does."""
        self.live.append(
            _LeaseEntry(
                None,
                self.providers.get(provider),
                snapshot.roles,
                "destroy",
                sanitation="succeeded",
                saved=snapshot,
                carried=True,
            )
        )

    async def snapshot(
        self, sandbox: Sandbox, *, carried: Literal["round", "move"] | None = None
    ) -> str:
        """Save a sanitized solver's disk as the snapshot its verifier boots from, destroy
        the solver, and return the snapshot's handle. One ``carried`` to the next solver, a
        validation ``round``'s, which the next turn's solver boots from too, or a ``move``'s,
        which the solver goes on in, is destroyed whatever the Run's retention, which
        concerns the final solver only."""
        entry = self._find(sandbox)
        assert entry is not None, "only a leased Sandbox is saved"
        purpose = {
            "round": "a validation round and the next turn boot from",
            "move": "the solver moves to",
            None: "its verifier boots from",
        }[carried]
        if entry.sanitation != "succeeded":
            raise RetentionFinalizationError(
                f"could not sanitize the solver for the snapshot {purpose}: "
                + (entry.sanitation_reason or "sanitation did not run")
            )
        try:
            entry.saved = await sandbox.snapshot(
                roles=entry.roles,
                reason=f"saved as the snapshot {purpose}"
                if carried or entry.requested != "snapshot"
                else "requested by sandbox_retention run configuration",
            )
        except BaseException as exc:
            # Never tried again, as the episode ends: the solver goes as a retention that
            # failed, with why.
            entry.sanitation = "failed"
            entry.sanitation_reason = str(exc) or "the save was cut short"
            raise
        if carried:
            # The Run's retention concerns the final solver: this one's snapshot goes once
            # nothing runs on it.
            entry.requested, entry.carried = "destroy", True
        logging.getLogger("orbit.execution").info(
            f"the solver was saved as the snapshot {purpose}",
            extra={"orbit_data": {"handle": entry.saved.handle}},
        )
        # The snapshot stopped the Sandbox; only the snapshot is left. A Sandbox that
        # cannot be destroyed now is destroyed again as the episode ends, whether its
        # snapshot goes then or is kept.
        try:
            await sandbox.destroy()
        except Exception as exc:
            entry.sanitation_reason = str(exc)
            raise RetentionFinalizationError(
                f"could not destroy sandbox {sandbox.sandbox_id}: {exc}"
            ) from exc
        finally:
            sandbox.release_resources()
        return entry.saved.handle

    def mark_sanitized(
        self, sandbox: Sandbox, *, succeeded: bool, reason: str | None = None
    ) -> None:
        entry = self._find(sandbox)
        if entry is not None:
            entry.sanitation = "succeeded" if succeeded else "failed"
            entry.sanitation_reason = reason

    async def finalize(self, *, allow_retention: bool = True) -> None:
        """Keep what retention asks for and destroy the rest, every entry whatever another's
        failure, which is raised once all are done: an earlier round's snapshot that cannot
        be destroyed never costs the final one its retention."""
        failure: RetentionFinalizationError | None = None
        for entry in self._in_order():
            try:
                await self._finalize(entry, allow_retention=allow_retention)
            except RetentionFinalizationError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    async def _finalize(self, entry: _LeaseEntry, *, allow_retention: bool) -> None:
        if entry.saved is not None:
            if not (allow_retention and entry.requested == "snapshot"):
                await self._destroy_saved(entry)
                return
            # Saved for its verifier already: that snapshot is the one kept.
            retained = entry.saved
        elif not allow_retention or entry.requested == "destroy" or entry.sanitation != "succeeded":
            outcome = (
                "destroyed"
                if not allow_retention or entry.requested == "destroy"
                else "retention-failed"
            )
            await self._destroy(entry, outcome=outcome)
            return
        else:
            reason = "requested by sandbox_retention run configuration"
            try:
                if entry.requested == "snapshot":
                    retained = await entry.sandbox.snapshot(roles=entry.roles, reason=reason)
                else:
                    retained = await entry.sandbox.retain(roles=entry.roles, reason=reason)
            except Exception as exc:
                entry.sanitation_reason = str(exc)
                await self._destroy(entry, outcome="retention-failed")
                return
        outcome = _retained_outcome(entry, retained)
        self.outcomes.append(outcome)
        self.retained.append((entry, outcome))
        if entry.requested == "snapshot":
            # The snapshot holds the agent's state now; its Sandbox goes, again if its save
            # for its verifier could not destroy it.
            await self._destroy(entry, outcome=None)
        else:
            entry.sandbox.release_resources()
            self.live.remove(entry)

    async def destroy_retained(self) -> None:
        """Undo retention if terminal recording fails after Sandbox finalization."""
        failure: RetentionFinalizationError | None = None
        for entry, outcome in self.retained:
            self.outcomes.remove(outcome)
            try:
                if outcome.outcome == "snapshotted" and outcome.handle is not None:
                    await self._destroy_snapshot(entry, outcome)
                else:
                    await self._destroy(entry, outcome="destroyed")
            except RetentionFinalizationError as exc:
                failure = failure or exc
        self.retained.clear()
        if failure is not None:
            raise failure

    async def _destroy_snapshot(self, entry: _LeaseEntry, kept: SandboxOutcome) -> None:
        """Destroy a snapshot retention kept; one that cannot be is recorded as not destroyed,
        with its handle and cleanup command."""
        assert kept.handle is not None
        try:
            # The Provider that made it, which alone holds it.
            await entry.provider.destroy_retained(kept.handle)
        except Exception as exc:
            entry.sanitation_reason = f"could not destroy snapshot {kept.handle}: {exc}"
            self.outcomes.append(
                kept.model_copy(
                    update={"outcome": "not-destroyed", "reason": entry.sanitation_reason}
                )
            )
            raise RetentionFinalizationError(entry.sanitation_reason) from exc
        self.outcomes.append(
            SandboxOutcome(
                roles=entry.roles,
                requested=entry.requested,
                outcome="destroyed",
                provider=entry.provider.name,
                reason=entry.sanitation_reason,
            )
        )

    async def _destroy_saved(self, entry: _LeaseEntry, *, last: bool = False) -> None:
        """Destroy the snapshot a solver was saved as, its Sandbox again first, idempotently.

        Only once that succeeds does the entry go, recorded destroyed, so a later call tries
        again; the ``last`` call records a snapshot it cannot destroy as the snapshot it is,
        with why, so that it is found and removed by hand rather than lost. One carried to
        the next solver, a validation round's or a move's, is recorded as not destroyed
        instead: the agent's state in the middle of its episode is no state a Run continues
        from.
        """
        saved = entry.saved
        assert saved is not None
        try:
            if any(other.booted_from is entry for other in self.undestroyed):
                raise RetentionFinalizationError(
                    f"could not destroy snapshot {saved.handle}: a Sandbox that could not be "
                    "destroyed still runs on it"
                )
            if entry.sandbox is not None:
                try:
                    await entry.sandbox.destroy()
                except Exception as exc:
                    raise RetentionFinalizationError(
                        f"could not destroy sandbox {entry.sandbox.sandbox_id}: {exc}"
                    ) from exc
            try:
                await entry.provider.destroy_retained(saved.handle)
            except Exception as exc:
                raise RetentionFinalizationError(
                    f"could not destroy snapshot {saved.handle}: {exc}"
                ) from exc
        except RetentionFinalizationError as exc:
            entry.sanitation_reason = str(exc)
            if last:
                self.live.remove(entry)
                kept = _retained_outcome(entry, saved).model_copy(
                    update={
                        "outcome": "not-destroyed" if entry.carried else "snapshotted",
                        "reason": entry.sanitation_reason,
                    }
                )
                self.outcomes.append(kept)
            raise
        self.live.remove(entry)
        self.outcomes.append(
            SandboxOutcome(
                roles=entry.roles,
                requested=entry.requested,
                outcome="destroyed",
                provider=entry.provider.name,
            )
        )
        logging.getLogger("orbit.execution").info(
            "the solver's snapshot was destroyed", extra={"orbit_data": {"handle": saved.handle}}
        )

    def _booted_from(self, entry: _LeaseEntry) -> bool:
        """Whether a Sandbox booted from ``entry``'s snapshot still runs, since no Provider
        destroys a snapshot under one; one saved as a snapshot in turn runs no more."""
        return any(
            other.booted_from is entry and other.saved is None
            for other in (*self.live, *self.undestroyed)
        )

    def _in_order(self) -> list[_LeaseEntry]:
        """The live entries, a saved solver last: the Sandboxes booted from its snapshot go
        first, since no Provider destroys a snapshot one still runs on."""
        return sorted(self.live, key=lambda entry: entry.saved is not None)

    async def _destroy(
        self,
        entry: _LeaseEntry,
        *,
        outcome: Literal["destroyed", "retention-failed"] | None,
    ) -> None:
        """Destroy an entry's Sandbox, recording ``outcome`` unless one is recorded already;
        one that cannot be destroyed is recorded as not destroyed, with why, never as gone."""
        assert entry.sandbox is not None, "a snapshot taken over goes as a saved one"
        error: Exception | None = None
        left: RetainedSandbox | None = None
        try:
            await entry.sandbox.destroy()
        except Exception as exc:
            error = exc
            entry.sanitation_reason = f"could not destroy sandbox {entry.sandbox.sandbox_id}: {exc}"
            # Still running, perhaps on a snapshot the lease holds, which stays while it does.
            self.undestroyed.append(entry)
            with contextlib.suppress(Exception):
                left = await entry.sandbox.retain(roles=entry.roles, reason=entry.sanitation_reason)
        finally:
            entry.sandbox.release_resources()
            if entry in self.live:
                self.live.remove(entry)
        if outcome is not None or error is not None:
            recorded = SandboxOutcome(
                roles=entry.roles,
                requested=entry.requested,
                outcome=outcome or "destroyed",
                provider=entry.provider.name,
                reason=entry.sanitation_reason,
            )
            if error is not None:
                recorded = recorded.model_copy(
                    update={
                        "outcome": "not-destroyed",
                        "handle": left.handle if left else None,
                        "cleanup_command": left.cleanup_command if left else None,
                    }
                )
            self.outcomes.append(recorded)
        if error is not None:
            assert entry.sanitation_reason is not None
            raise RetentionFinalizationError(entry.sanitation_reason) from error

    async def release_all(self) -> None:
        failure: RetentionFinalizationError | None = None
        for entry in self._in_order():
            try:
                if entry.saved is not None:
                    await self._destroy_saved(entry, last=True)
                else:
                    await self._destroy(entry, outcome="destroyed")
            except RetentionFinalizationError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    def _find(self, sandbox: Sandbox) -> _LeaseEntry | None:
        return next((entry for entry in self.live if entry.sandbox is sandbox), None)

    def _saved(self, handle: str) -> _LeaseEntry | None:
        """The entry of the snapshot ``handle`` it holds."""
        return next(
            (
                entry
                for entry in self.live
                if entry.saved is not None and entry.saved.handle == handle
            ),
            None,
        )


def _open_sessions(sessions: tuple[GatewaySession, ...], opened: bool) -> None:
    for session in sessions:
        session.suspended = not opened
        if opened:
            # A turn's program answers for its own run's calls, not for an earlier turn's.
            session.failure = None


def _open_egress(session: GatewaySession, policy: NetworkPolicy) -> None:
    session.policy = policy
    session.suspended = not policy.proxied


def _retained_outcome(entry: _LeaseEntry, retained: RetainedSandbox) -> SandboxOutcome:
    return SandboxOutcome(
        roles=entry.roles,
        requested=entry.requested,
        outcome="snapshotted" if entry.requested == "snapshot" else "retained",
        provider=retained.provider,
        handle=retained.handle,
        reason=retained.reason,
        cleanup_command=retained.cleanup_command,
        gpu_devices=retained.gpu_devices,
    )


def _artifact_python(operating_system: OperatingSystem) -> str:
    """The Python the framework copies artifacts with: its own, so the image needs none."""
    return guestd.PYTHON[operating_system.value]


@dataclass
class _Artifacts(ArtifactSink):
    """Immutable solver output, captured for the Host or for a separate verifier.

    A separate verifier receives the solver's output only through the Host, so separate
    verification always captures it, as in Harbor. Unless run policy collects it, that copy
    is discarded with the episode.
    """

    run_dir: Path
    enabled: bool = True
    retained: bool = True
    collected: list[str] = field(default_factory=list)
    entries: list[dict[str, object]] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.run_dir / "artifacts"

    async def collect(
        self,
        sandbox: Sandbox,
        source: str,
        name: str,
        exclude: tuple[str, ...] = (),
        destination: str | None = None,
        *,
        restored: bool = True,
    ) -> None:
        if not self.enabled:
            return
        logger = logging.getLogger("orbit.execution")
        python = _artifact_python(sandbox.request.os)
        script = Path(artifact_files.__file__).read_text()
        path_type = (
            PureWindowsPath if sandbox.request.os is OperatingSystem.WINDOWS else PurePosixPath
        )
        pattern = any(char in source for char in "*?[")
        base = path_type(source).parent
        while any(char in str(base) for char in "*?["):
            base = base.parent
        # A separate directory for each declaration prevents basename collisions and
        # keeps snapshot.json reserved for the inventory, even for that literal filename.
        batch = self.root / f"{len(self.collected):04d}"
        if destination is not None:
            # As in Harbor, a path is kept as the destination itself, and a pattern's matches
            # below it, each where it is below the pattern's fixed part.
            batch = self.root.joinpath(*PurePosixPath(destination).parts)
            if not pattern:
                base = path_type(source)
        self.collected.append(name)
        captured = {entry["source"] for entry in self.entries}
        try:
            probe = await sandbox.exec(
                [python, "-c", script, "inspect", source, *exclude], identity=Identity.FRAMEWORK
            )
            if not probe.ok:
                raise OSError(probe.stderr or "artifact inspection failed")
            details = json.loads(probe.stdout)
            if details["truncated"] and restored:
                self.truncated.append(source)
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
                            "restored": restored,
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

    def for_validation(self, directory: Path) -> _Artifacts:
        """A sink of its own for one validation round's separate verifier, below
        ``directory``, whose copy goes once it is cleaned up."""
        return _Artifacts(directory, enabled=True, retained=False)

    async def restore(self, sandbox: Sandbox) -> None:
        if self.truncated:
            # A verifier scoring part of the output gives a wrong score, not a low one.
            raise TaskError(f"artifacts too large to copy whole: {', '.join(self.truncated)}")
        if not self.enabled or not self.entries:
            return
        python = _artifact_python(sandbox.request.os)
        roots: list[PurePosixPath | PureWindowsPath] = []
        path_type = (
            PureWindowsPath if sandbox.request.os is OperatingSystem.WINDOWS else PurePosixPath
        )
        for entry in sorted(
            (item for item in self.entries if item.get("restored", True)),
            key=lambda item: len(path_type(str(item["source"])).parts),
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
                if sandbox.request.os is not OperatingSystem.WINDOWS:
                    mode = await sandbox.exec(
                        ["chmod", "--", f"{int(entry['mode']):o}", source],
                        identity=Identity.FRAMEWORK,
                    )
                    if not mode.ok:
                        raise OSError(mode.stderr or "cannot restore artifact mode")
            except Exception as exc:
                logging.getLogger("orbit.execution").warning(
                    "artifact restore skipped: %s: %s", source, exc
                )

    def cleanup(self) -> None:
        if not self.retained:
            shutil.rmtree(self.root, ignore_errors=True)

    def _write_manifest(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "entries": self.entries, "retained": True}
        target = self.root / "snapshot.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(target)


def check_retention(
    spec: TaskSpec, retention: SandboxRetentionConfig, *, validates: bool = False
) -> None:
    """Refuse, before anything is prepared, a retention that cannot hold for ``spec``, whose
    episodes validate when it ``validates``.

    A snapshot verifier boots from a snapshot of the solver, and saving the snapshot stops
    the solver, so it cannot be kept; a kept verifier still runs on the snapshot, which no
    Provider destroys under it, so the snapshot must be kept too. A solver that validates
    boots its next turn's solver from a snapshot of its own, which is never kept and which a
    kept solver would still run on, whichever setting keeps it.
    """
    shared = spec.verify.environment_mode is VerificationMode.SHARED
    # The Sandbox that holds the solver: under shared verification the verifier's policy
    # applies to it when the solver's is to destroy.
    solver = retention.verifier if shared and retention.solver == "destroy" else retention.solver
    if validates and solver == "keep":
        setting = (
            "sandbox_retention.solver"
            if retention.solver == "keep"
            else ("sandbox_retention.verifier, which under shared verification is the solver's,")
        )
        raise ConfigError(
            f"{spec.name} validates on a snapshot of its solver, from which the "
            f"solver after it boots, and which a kept solver would still run on: {setting} "
            "cannot be 'keep'; sandbox_retention.solver = 'snapshot' keeps its disk"
        )
    if spec.verify.environment_mode is not VerificationMode.SNAPSHOT:
        return
    if retention.solver == "keep":
        raise ConfigError(
            f"{spec.name} is verified in a Sandbox booted from a snapshot of its solver, and "
            "saving the snapshot stops the solver: sandbox_retention.solver cannot be 'keep'; "
            "'snapshot' keeps its disk"
        )
    if retention.verifier == "keep" and retention.solver != "snapshot":
        raise ConfigError(
            f"{spec.name} is verified in a Sandbox booted from a snapshot of its solver, which "
            "a kept verifier still runs on: sandbox_retention.verifier = 'keep' needs "
            "sandbox_retention.solver = 'snapshot'"
        )


async def run_episode(
    task: Task,
    protocol: EpisodeProtocol,
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
    agent_resources: EffectiveAgentResources | None = None,
    episode_id: str | None = None,
    trajectory_id: str | None = None,
    phase_callback: Callable[[Phase], None] | None = None,
    logging_policy: LoggingPolicy | None = None,
    verification_config: VerificationConfig | None = None,
    sandbox_retention: SandboxRetentionConfig | None = None,
    desktop_resolution: DesktopResolution = DesktopResolution(),
    destroy_failed_sandboxes: bool = False,
    authentication: Literal["api-key", "subscription"] = "api-key",
    profile_slot_id: str | None = None,
    continuation: EpisodeContinuation | None = None,
) -> EpisodeResult | EpisodeContinuation:
    """Administer one task and return its verdict.

    Never raises for an episode that merely failed: a failure is a result with a status,
    and the caller decides what to do with it.

    Where the Provider holds the agent's next solver only in a later process, the episode is
    handed off: this process's part of it ends without verification or a result, and the
    record a later process continues it from is returned instead. Given that
    ``continuation``, an episode goes on in the same episode directory, with the same ids,
    and its result describes it whole.
    """
    if task.variables is None:
        # Resolved by the caller for every Task at once, so a missing one fails the whole
        # selection before anything is built, started or spent.
        declared = task.spec.variables
        if declared.mcp_headers or any(env for _, env in declared.tables):
            raise TaskDefinitionError(f"{task.spec.name}: its variables were not resolved")
        task.variables = ResolvedVariables()
    variables = task.variables
    task.asset_observation = observe_task_assets(task)
    await resolve_image_kinds([task])
    sandbox_retention = sandbox_retention or SandboxRetentionConfig()
    # A caller that selected Providers for its Tasks has also checked them on this Host, and
    # their retention.
    alone = task.provider is None
    if alone:
        task.validations = protocol.validations
        check_retention(task.spec, sandbox_retention, validates=task.validates)
        await providers.select([task], snapshots=sandbox_retention.solver == "snapshot")
    assert task.provider is not None
    if task.prepared_image is None:
        task.prepared_image = await prepare_task_image(task, providers.get(task.provider))
    if task.prepared_verifier_image is None:
        task.prepared_verifier_image = await prepare_verifier_image(
            task, providers.get(task.verifier_provider or task.provider)
        )
    if task.prepared_validation_image is None:
        task.prepared_validation_image = await prepare_validation_image(
            task, providers.get(task.verifier_provider or task.provider)
        )
    if alone:
        await providers.preflight([task])
    if continuation is not None:
        if episode_id not in (None, continuation.episode_id) or trajectory_id not in (
            None,
            continuation.trajectory_id,
        ):
            raise ConfigError("an episode goes on under its own episode and trajectory ids")
        episode_id, trajectory_id = continuation.episode_id, continuation.trajectory_id
    episode_id = episode_id or f"{task_component(task.spec.name)}-{uuid.uuid4().hex[:8]}"
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
            provider_web_tools=task.spec.network.mode is NetworkMode.OPEN,
            redacted=variables.secrets,
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
        if continuation is not None and continuation.gateway is not None:
            # Its limits cover the whole episode, so they go on counting from there; closed
            # until the agent's next run opens it.
            session.restore(continuation.gateway)
            session.suspended = True
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
    for opened in judge_sessions.values():
        # Usable only while a verifier runs: a token that reaches the agent is no use to it.
        opened.suspended = True
    # The relays' credential at the egress proxy, when a phase of this episode has a proxied
    # policy, the agent's or the verifier's: its policy is that of the phase that runs, and
    # the agent's model token, which its CLI holds, is never one.
    egress: GatewaySession | None = None
    proxied = (task.spec.network, task.spec.verify.network, task.spec.validation_spec.network)
    if proxy_url and any(policy.proxied for policy in proxied):
        candidate = GatewaySession(
            episode_id=episode_id, model="", purpose="egress", suspended=True
        )
        if gateway is not None:
            egress = gateway.open_session(candidate)
        elif session_registry is not None:
            egress = session_registry.open(candidate)

    separate = task.spec.verify.environment_mode is VerificationMode.SEPARATE
    sink = _Artifacts(
        episode_dir, enabled=collect_artifacts or separate, retained=collect_artifacts
    )
    lease = _Lease(
        providers,
        sandbox_retention,
        desktop_resolution,
        solver=task.provider,
        verifier=task.verifier_provider,
    )
    if continuation is not None:
        # The snapshot is this episode's from the start, whatever ends it.
        lease.take_over(continuation.provider, continuation.snapshot)
        lease.outcomes = list(continuation.sandbox_outcomes)
    started_at = continuation.started_at if continuation is not None else datetime.now(UTC)
    started = time.monotonic()
    ctx = EpisodeContext(
        episode_id=episode_id,
        spec=task.spec,
        task_dir=task.folder.root if task.folder is not None else run_dir,
        validation_stage=task.validation_stage,
        shown_stage=(
            task.folder.stage_dir(task.validation_stage)
            if task.spec.validation_visible and task.folder is not None
            else None
        ),
        run_dir=episode_dir,
        sandboxes=lease,
        artifacts=sink,
        budget=Budget(
            deadline_sec=SETUP_TIMEOUT_SEC + task.spec.timeouts.agent + task.spec.timeouts.verify,
            started_at=started,
        ),
        session=HarnessSession(
            episode_id=episode_id,
            gateway_url=gateway_url,
            token=session_token,
            model=model,
            os=task.spec.os,
            authentication=authentication,
            profile_slot_id=profile_slot_id,
            provider_web_tools=task.spec.network.mode is NetworkMode.OPEN,
        ),
        agent_resources=agent_resources or EffectiveAgentResources(),
        task_source=task.source,
        variables=variables,
        trajectory_id=trajectory_id,
        transport=recording.transport,
        execution=recording.execution,
        blobs=recording.blobs,
        trajectory=recording,
        result=recording,
        seed=seed,
        proxy_url=proxy_url,
        egress_token=egress.token if egress is not None else "",
        open_egress_session=None if egress is None else partial(_open_egress, egress),
        prepared_image=task.prepared_image,
        prepared_verifier_image=task.prepared_verifier_image,
        prepared_validation_image=task.prepared_validation_image,
        phase_callback=phase_callback,
        limit_refusal=lambda: _gateway_refusal(session),
        model_provider_failure=lambda: _model_provider_failure(session),
        open_agent_session=None if session is None else partial(_open_sessions, (session,)),
        logging_policy=logging_policy or LoggingPolicy(),
        verification_config=verification_config or VerificationConfig(),
        judge_tokens={kind: opened.token for kind, opened in judge_sessions.items()},
        open_judge_sessions=partial(_open_sessions, tuple(judge_sessions.values())),
    )
    if continuation is not None:
        _continue(ctx, continuation)
    redact = Redactor(ctx.secrets)
    verdict: Verdict | None = None
    handed: HandedOff | None = None
    try:
        verdict = await protocol.run(task, ctx)
    except HandedOff as handing_off:
        handed = handing_off
    except Exception as exc:  # every failure becomes a typed result, not a traceback
        if isinstance(exc, SandboxHandOff):
            # Only the agent's work goes on in a later process; a Sandbox its Provider could
            # not hold anywhere else ends the episode.
            exc = ProviderCapabilityError(
                f"the Provider holds no Sandbox booted from snapshot {exc.handle} long enough "
                "here, and only the agent's work goes on in the episode's next process"
            )
        if isinstance(exc, BudgetExceededError):
            ctx.limit_termination = LimitTermination(
                layer=exc.layer,  # type: ignore[arg-type]
                name=exc.limit,
                configured_value=exc.value,
                observed_value=exc.observed_value,
                reason=type(exc).__name__,
            )
        phase = _phase_of(exc, ctx.failure_phase)
        status = status_for(exc, agents=_agents_sandbox(task, ctx, phase))
        if (exhausted := _exhausted(status, task.spec.resources)) is not None:
            name, declared = exhausted
            ctx.limit_termination = LimitTermination(
                layer="sandbox", name=name, configured_value=declared, reason=type(exc).__name__
            )
        verdict = Verdict.failed(
            status,
            exc,
            phase=phase,
            message=redact(str(exc)),
            rewards=ctx.stopped_rewards,
        )
        recording.execution.append(
            ExecutionFailure(
                episode_id=episode_id,
                phase=phase,
                component="episode",
                level="error",
                error_type=type(exc).__name__,
                message=redact(str(exc)),
            ),
            durable=True,
        )
    finally:
        if gateway is not None:
            # Tokens die with their episode, so a leaked one is not a standing grant.
            for opened in (session, *judge_sessions.values(), egress):
                if opened is not None:
                    gateway.close_session(opened)
        elif session_registry is not None:
            for opened in (session, egress):
                if opened is not None:
                    session_registry.close(opened)
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
        if finalization_error is None and continuation is not None:
            # As one this process could not destroy, one an earlier process could not.
            lost = next(
                (item for item in continuation.sandbox_outcomes if item.outcome == "not-destroyed"),
                None,
            )
            if lost is not None:
                finalization_error = RetentionFinalizationError(
                    lost.reason or "a Sandbox of the episode's earlier process was not destroyed"
                )
        if (
            finalization_error is not None
            and verdict is not None
            and verdict.status is Status.COMPLETED
        ):
            verdict = Verdict.failed(
                Status.ENV_ERROR,
                finalization_error,
                phase="teardown",
                message=redact(str(finalization_error)),
            )

    if handed is not None:
        # No result: the episode goes on, and its next process writes it.
        return _continuation(ctx, handed, started_at=started_at, session=session)
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
        verdict = Verdict.failed(Status.ENV_ERROR, exc, phase="finalize", message=redact(str(exc)))
        recording.execution.append(
            ExecutionFailure(
                episode_id=episode_id,
                phase="finalize",
                component="recording",
                level="error",
                error_type=type(exc).__name__,
                message=redact(str(exc)),
            ),
            durable=True,
        )
    if destroy_failed_sandboxes and verdict.status is not Status.COMPLETED:
        try:
            await lease.destroy_retained()
        except RetentionFinalizationError as exc:
            verdict = Verdict.failed(
                Status.ENV_ERROR, exc, phase="teardown", message=redact(str(exc))
            )
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
        validation_rounds=tuple(ctx.validation_rounds),
        segments=tuple(ctx.segments),
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
    duration = (
        time.monotonic() - started
        if continuation is None
        else (finished_at - continuation.started_at).total_seconds()
    )
    return EpisodeResult(
        episode_id=episode_id,
        verdict=verdict,
        run_dir=episode_dir,
        duration_sec=duration,
        record=record,
        lock=lock,
    )


def _continuation(
    ctx: EpisodeContext,
    handed: HandedOff,
    *,
    started_at: datetime,
    session: GatewaySession | None,
) -> EpisodeContinuation:
    """What the episode's next process continues it from, once this one handed it off."""
    tokens = (
        ctx.session.token,
        ctx.egress_token,
        *ctx.verification_secrets,
        *ctx.judge_tokens.values(),
        *ctx.earlier_secrets,
    )
    return EpisodeContinuation(
        episode_id=ctx.episode_id,
        trajectory_id=ctx.trajectory_id,
        started_at=started_at,
        snapshot=handed.snapshot,
        provider=handed.provider,
        image=handed.image,
        planned_saves=handed.planned_saves,
        turn=handed.turn,
        agent_time=handed.agent_time,
        session=handed.session,
        agent_started=ctx.agent_started,
        agent_version=ctx.agent_version,
        agent_run=ctx.agent_run,
        failure_phase=ctx.failure_phase,
        prepared_image=ctx.prepared_image,
        resolved_image=ctx.resolved_image,
        resource_allocation=ctx.resource_allocation,
        sandbox_identity=ctx.sandbox_identity,
        home=ctx.home,
        resumed_from=ctx.resumed_from,
        prepared_validation_image=ctx.prepared_validation_image,
        validation_resolved_image=ctx.validation_resolved_image,
        validation_resource_allocation=ctx.validation_resource_allocation,
        orbit_verify_provenance=ctx.orbit_verify_provenance,
        phases=tuple(ctx.phases),
        segments=tuple(ctx.segments),
        validation_rounds=tuple(ctx.validation_rounds),
        validation_prompts=dict(ctx.validation_prompts),
        move_prompts=dict(ctx.move_prompts),
        # As JSON holds them, which the next process reads.
        validation_judge_invocations=tuple(
            json.loads(json.dumps(asdict(item)))
            for item in ctx.validation_judge_invocations
            if isinstance(item, JudgeInvocation)
        ),
        harness_execution_counter=ctx.harness_execution_counter,
        sandbox_outcomes=ctx.sandbox_outcomes,
        gateway=session.accounting() if session is not None else None,
        # The Task's own secrets are its variables', which every process resolves again.
        redacted=tuple(dict.fromkeys(token for token in tokens if token)),
    )


def _continue(ctx: EpisodeContext, continuation: EpisodeContinuation) -> None:
    """Go on with what the episode's earlier process recorded and observed."""
    ctx.continued = continuation
    ctx.earlier_secrets = continuation.redacted
    ctx.agent_started = continuation.agent_started
    ctx.agent_version = continuation.agent_version
    ctx.agent_run = continuation.agent_run
    ctx.failure_phase = continuation.failure_phase
    ctx.prepared_image = continuation.prepared_image
    ctx.resolved_image = continuation.resolved_image
    ctx.resource_allocation = continuation.resource_allocation
    ctx.sandbox_identity = continuation.sandbox_identity
    ctx.home = continuation.home
    ctx.resumed_from = continuation.resumed_from
    ctx.prepared_validation_image = continuation.prepared_validation_image
    ctx.validation_resolved_image = continuation.validation_resolved_image
    ctx.validation_resource_allocation = continuation.validation_resource_allocation
    ctx.orbit_verify_provenance = continuation.orbit_verify_provenance
    ctx.phases = list(continuation.phases)
    ctx.segments = list(continuation.segments)
    ctx.validation_rounds = list(continuation.validation_rounds)
    ctx.validation_prompts = dict(continuation.validation_prompts)
    ctx.move_prompts = dict(continuation.move_prompts)
    ctx.validation_judge_invocations = tuple(
        JudgeInvocation.from_dict(item) for item in continuation.validation_judge_invocations
    )
    ctx.harness_execution_counter = continuation.harness_execution_counter


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
    validations = tuple(
        item for item in ctx.validation_judge_invocations if isinstance(item, JudgeInvocation)
    )
    if invocations or validations:
        endpoints = judge_endpoints(ctx.verification_config)
        judges = judge_provenance(validations, endpoints, placement="validate")
        inputs = replace(inputs, judges=judges + judge_provenance(invocations, endpoints))
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
        validated=bool(inputs.validation_rounds) or bool(ctx.validation_rounds),
        validation_resolved_image=ctx.validation_resolved_image,
        validation_allocation=ctx.validation_resource_allocation,
        prepared_validation_image=ctx.prepared_validation_image,
        orbit_verify=ctx.orbit_verify_provenance,
        termination=ctx.limit_termination,
        sandbox_outcomes=ctx.sandbox_outcomes,
        resumed_from=ctx.resumed_from,
        variables=ctx.variables.provenance,
        harbor=task.harbor,
        warnings=task.warnings,
        seed=seed,
        # The agent was to receive rounds, or calibration's validation check ran.
        validation_stage="tests"
        if ctx.validation_stage is Stage.TESTS
        and (bool(inputs.validation_rounds) or bool(ctx.validation_rounds))
        else "validation",
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


def _model_provider_failure(session: GatewaySession | None) -> str | None:
    failure = session.failure if session is not None else None
    if failure is None:
        return None
    return failure.describe(asyncio.get_running_loop().time())


def _exhausted(status: Status, resources: Resources) -> tuple[str, int] | None:
    """The resource ``status`` records the agent's Sandbox was lost to, with the value its
    Task declares, which the RunLock's termination names; None for any other status."""
    if status is Status.MEMORY_EXHAUSTED:
        return "memory_mb", resources.memory_mb
    if status is Status.STORAGE_EXHAUSTED and resources.storage_mb is not None:
        return "storage_mb", resources.storage_mb
    return None


def _agents_sandbox(task: Task, ctx: EpisodeContext, phase: str | None) -> bool:
    """Whether a Sandbox lost in ``phase`` holds the agent's work: the one it ran in, from
    its phase on, installing its Harness included, holds what it left there, processes and
    files in memory too. Setup and a verifier of its own run the Task's code alone, beside
    the agent's files at most."""
    own = task.spec.verify.environment_mode is not VerificationMode.SHARED
    return ctx.agent_started and not (own and phase == Phase.VERIFY.value)


def _phase_of(error: BaseException, recorded_phase: object = None) -> str | None:
    if isinstance(error, PhaseTimeoutError):
        return error.phase
    if isinstance(recorded_phase, str):
        return recorded_phase
    if isinstance(error, TaskError):
        return Phase.VERIFY.value
    return None
