"""The episode protocol: how one Task becomes one Episode.

Provision a sandbox, prepare it, let the agent work, score what it left behind.

Three properties are enforced here rather than trusted to task authors: verification and
oracle material is absent while the evaluated agent runs, every phase has a deadline,
and teardown happens on every path out. Tasks own literal paths inside the image-declared
agent home; ALE owns only staging time, its root-only framework directory and the
verifier directory a result is written to.

An agent that receives validation rounds works in turns, each after the first in a solver
booted from the snapshot the turn before it left, with the result of ``validation/`` on
that snapshot as its prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Literal, cast

from ale.core.capability import round_verifier_resources
from ale.core.config import RunTimeouts
from ale.core.episode import ArtifactSink, EpisodeContext, Phase
from ale.core.errors import (
    AgentError,
    AgentUnsupportedError,
    BudgetExceededError,
    ConfigError,
    EnvironmentError_,
    GuestUnreachableError,
    NativeContinuationError,
    PhaseTimeoutError,
    ProviderCapabilityError,
    ProviderCapacityError,
    ProviderStartError,
    ResourceExhaustedError,
    RetentionFinalizationError,
    TaskError,
    TrajectoryConversionError,
    VerificationInfrastructureError,
    VerifierOutputError,
)
from ale.core.harness import (
    AutonomousHarness,
    EffectiveAgentResources,
    Harness,
    NativeContinuation,
    PolicyHarness,
    ResumeSupport,
    TrajectoryParseContext,
)
from ale.core.lock import AleVerifyProvenance, ResumedFrom, SandboxProvenance
from ale.core.result import FailureInfo, PhaseTiming, ValidationFeedback, ValidationRound
from ale.core.sandbox import (
    ExecOutputSink,
    ExecResult,
    Identity,
    PreparedTaskImage,
    ResolvedImage,
    ResourceAllocation,
    Sandbox,
    SandboxRequest,
    SandboxRole,
)
from ale.core.task import Stage, Task
from ale.core.taskspec import (
    NetworkMode,
    NetworkPolicy,
    OperatingSystem,
    StdioMcpServer,
    TaskSpec,
    VerificationMode,
)
from ale.core.trace import (
    PhaseFinished,
    PhaseStarted,
    PolicyApplied,
    TrajectoryLink,
    read_jsonl,
)
from ale.core.trajectory import (
    AtifAgent,
    AtifFinalMetrics,
    AtifMetrics,
    AtifStep,
    AtifTrajectory,
    TrajectoryBuilder,
)
from ale.core.verdict import Status, Verdict
from ale.run import guestd
from ale.run.envs import DEFAULT_MAX_STEPS, DEFAULT_STALL_LIMIT, SandboxEnv
from ale.run.harnesses import credential_files
from ale.run.harnesses.builtin import NopHarness, OracleHarness, solution_dir
from ale.run.protocol.log_mirror import (
    FINAL_SYNC_TIMEOUT_SEC,
    SYNC_INTERVAL_SEC,
    SYNC_TIMEOUT_SEC,
    LogMirror,
)
from ale.run.recording import (
    BlobStore,
    CommandRecorder,
    Redactor,
    atomic_write_json,
    copy_trajectory,
    execution_logging,
    redact_files,
)
from ale.run.verification import installed_ale_verify, verifier_judge_config
from ale_verify import MAX_FEEDBACK_BYTES, JudgeInvocation, read_result

__all__ = [
    "SETUP_TIMEOUT_SEC",
    "EpisodeProtocol",
    "Resume",
    "ReverificationProtocol",
    "SolverSnapshot",
    "validation_rounds",
]

#: How long past the agent deadline the Host waits. The guest enforces that deadline
#: itself and stops the agent's process tree before it replies, so evidence capture begins
#: after the agent stopped; this margin only bounds a guest that no longer replies.
AGENT_STOP_GRACE_SEC = 60.0

#: How an agent's run can end early. Its work is still verified, as in Harbor.
_AGENT_STOPS = (PhaseTimeoutError, BudgetExceededError, AgentError)

#: Upper bound on the setup phase: a guard against a hung setup, not a scored budget.
SETUP_TIMEOUT_SEC = 1800.0

#: Upper bound on waiting for room for the solver a validation round boots, such as GPUs
#: another Run took while validation/ ran, and how long it waits between tries; a framework
#: guard outside the agent's time.
BOOT_WAIT_SEC = 1800.0
BOOT_RETRY_SEC = 10.0

#: Upper bound on sanitizing a solver and saving it as the snapshot its verifier, or a
#: validation round, boots from: a guard against a hung save, outside the verifier's budget.
SNAPSHOT_TIMEOUT_SEC = 1800.0

#: Below the instruction, verbatim above it, when the agent receives validation rounds.
#: Framework-owned and neutral: it says how the episode goes on, never how to do the Task.
VALIDATION_PREAMBLE = (
    "\n\n---\n\n"
    "How your work is evaluated: each time you end your turn, your work is scored on a "
    "validation set and you receive the result, after which you continue working. This "
    "happens at most {rounds}. After that, your final state is scored once, on a separate "
    "hidden test set. Your time limit of {minutes} minutes covers all of your turns. "
    "Processes you leave running do not survive into your next turn.\n"
)


#: Where the framework's own machinery goes. Under a root-owned, root-only directory,
#: which is the whole point: the tests stage holds the scorer, and timing alone — being
#: uploaded only when needed — would keep it from the agent only by schedule. Permissions
#: are a better guarantee than a schedule.
#:
#: It also leaves exactly two roots in a sandbox: this one, which belongs to the framework
#: and which the agent cannot read, and the agent's home, which is entirely its own.
@dataclass(frozen=True)
class _StageLayout:
    root: PurePath

    def path(self, *parts: str) -> PurePath:
        return self.root.joinpath(*parts)


def _layout(operating_system: OperatingSystem) -> _StageLayout:
    root: PurePath = (
        PureWindowsPath("C:/ProgramData/ALE")
        if operating_system is OperatingSystem.WINDOWS
        else PurePosixPath("/opt/ale")
    )
    return _StageLayout(root)


def _verifier_dir(operating_system: OperatingSystem) -> PurePath:
    """Where verification writes ``reward.json``: Harbor's verifier directory.

    macOS seals its root volume, so the directory sits below the framework's own root there.
    """
    if operating_system is OperatingSystem.WINDOWS:
        return PureWindowsPath("C:/logs/verifier")
    if operating_system is OperatingSystem.MACOS:
        return PurePosixPath("/opt/ale/logs/verifier")
    return PurePosixPath("/logs/verifier")


#: Gives the verifier directory to the framework alone, empty. The agent may have made the
#: path, or a link at it, as its own account or with sudo, so a link or file where a
#: directory belongs is removed rather than followed, the parent becomes root's and only
#: root may change it, and the directory is made anew, readable by root alone.
_POSIX_VERIFIER_RESET = """set -eu
parent="$1" verifier="$2"
if [ -L "$parent" ] || { [ -e "$parent" ] && [ ! -d "$parent" ]; }; then rm -f -- "$parent"; fi
mkdir -p -- "$parent"
chown -- 0:0 "$parent"
chmod -- go-w "$parent"
rm -rf -- "$verifier"
mkdir -m 0700 -- "$verifier"
"""

#: Removes the verifier directory unless its parent is a link, which is the agent's own.
_POSIX_VERIFIER_REMOVE = """set -eu
if [ -d "$1" ] && [ ! -L "$1" ]; then rm -rf -- "$2"; fi
"""

#: The same on Windows, where any account may create a directory at the root of C:. An
#: entry's own attributes are read without following a link, a link is deleted as a link,
#: and a tree is walked without entering one. Both directories are owned by Administrators
#: and the verifier directory grants only SYSTEM and Administrators, which the agent's
#: limited token is not; it is created with that descriptor, so it never has another.
_WINDOWS_VERIFIER_ENTRIES = """$ErrorActionPreference = 'Stop'
$isDirectory = [int][IO.FileAttributes]::Directory
$isLink = [int][IO.FileAttributes]::ReparsePoint
function Get-Entry([string]$Path) {
    if ([IO.File]::Exists($Path) -or [IO.Directory]::Exists($Path)) {
        return [int][IO.File]::GetAttributes($Path)
    }
    return $null
}
function Test-Directory($Attributes) {
    if ($null -eq $Attributes) { return $false }
    return (($Attributes -band $isDirectory) -ne 0) -and (($Attributes -band $isLink) -eq 0)
}
function Remove-Entry([string]$Path) {
    $attributes = Get-Entry $Path
    if ($null -eq $attributes) { return }
    $directory = ($attributes -band $isDirectory) -ne 0
    if (($attributes -band $isLink) -ne 0) {
        if ($directory) { [IO.Directory]::Delete($Path) } else { [IO.File]::Delete($Path) }
        return
    }
    [IO.File]::SetAttributes($Path, [IO.FileAttributes]::Normal)
    if ($directory) {
        foreach ($child in [IO.Directory]::GetFileSystemEntries($Path)) { Remove-Entry $child }
        [IO.Directory]::Delete($Path)
    } else {
        [IO.File]::Delete($Path)
    }
}
"""

_WINDOWS_VERIFIER_RESET = """
$system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
function Protect-Directory([string]$Path, [bool]$Listed) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($admins)
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    foreach ($sid in @($system, $admins)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', $inherit, 'None', 'Allow'))
    }
    if ($Listed) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $users, 'ReadAndExecute', 'None', 'None', 'Allow'))
    }
    [IO.Directory]::CreateDirectory($Path, $acl) | Out-Null
    Set-Acl -LiteralPath $Path -AclObject $acl
}
if (-not (Test-Directory (Get-Entry $parent))) { Remove-Entry $parent }
Protect-Directory $parent $true
Remove-Entry $verifier
Protect-Directory $verifier $false
"""

_WINDOWS_VERIFIER_REMOVE = """
if (Test-Directory (Get-Entry $parent)) { Remove-Entry $verifier }
"""


def _verifier_dir_command(operating_system: OperatingSystem, *, remove: bool) -> list[str]:
    directory = _verifier_dir(operating_system)
    if operating_system is not OperatingSystem.WINDOWS:
        script = _POSIX_VERIFIER_REMOVE if remove else _POSIX_VERIFIER_RESET
        return ["sh", "-c", script, "sh", str(directory.parent), str(directory)]
    return _powershell(
        f"$parent = {_powershell_literal(str(directory.parent))}; "
        f"$verifier = {_powershell_literal(str(directory))}\n"
        + _WINDOWS_VERIFIER_ENTRIES
        + (_WINDOWS_VERIFIER_REMOVE if remove else _WINDOWS_VERIFIER_RESET)
    )


def _sandbox_path(operating_system: OperatingSystem, root: str, *parts: str) -> str:
    path_type = PureWindowsPath if operating_system is OperatingSystem.WINDOWS else PurePosixPath
    return str(path_type(root).joinpath(*parts))


def _powershell(command: str) -> list[str]:
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell_list(values: Sequence[str]) -> str:
    return "@(" + ",".join(_powershell_literal(value) for value in values) + ")"


@dataclass(frozen=True)
class Resume:
    """Continue an earlier episode in its retained solver instead of launching anew.

    ``trajectory`` is the earlier episode's, which tells where this episode's part of the
    native session begins. A kept solver is reconnected for this episode's request by
    ``attach``; a snapshot, named by ``source.handle``, boots a new solver.
    """

    source: ResumedFrom
    trajectory: AtifTrajectory | None
    attach: Callable[[SandboxRequest], Awaitable[Sandbox]] | None = None


@dataclass
class _Solver:
    """The Sandbox the agent's state is in: the first solver, or the one the last validation
    round booted, and the earlier ones, saved, whose snapshots the lease holds."""

    sandbox: Sandbox
    image: PreparedTaskImage | None
    """The image the Run's requests name, which a Sandbox booted from a round's snapshot
    names too, whatever a scheduler's Node prepared for it."""
    saved: bool = False
    """Set once it is being saved as a snapshot: saving stops it, and the lease then holds
    the snapshot in its place."""
    rounds: list[Sandbox] = field(default_factory=list)
    """The solver the last validation round saved, whose snapshot the current one runs on;
    the lease holds any earlier one whose snapshot could not be destroyed yet."""


@dataclass
class _AgentTime:
    """The agent's time: ``total`` across all its turns, of which ``used`` is spent."""

    total: float
    used: float = 0.0

    @property
    def remaining(self) -> float:
        return self.total - self.used


class EpisodeProtocol:
    """Linear provision → setup → agent → verify flow; with validation rounds, each turn of
    the agent but the last is followed by a save, ``validation/`` and a new solver."""

    component = "episode-protocol"

    def __init__(
        self,
        harness: AutonomousHarness | PolicyHarness,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        stall_limit: int = DEFAULT_STALL_LIMIT,
        agent_enabled: bool = True,
        timeouts: RunTimeouts | None = None,
        resume: Resume | None = None,
        validation_rounds: int = 0,
    ) -> None:
        self.harness = harness
        self.max_steps = max_steps
        self.stall_limit = stall_limit
        self.agent_enabled = agent_enabled
        self.timeouts = timeouts or RunTimeouts()
        self.resume = resume
        self.validation_rounds = validation_rounds
        """How many results of ``validation/`` the agent receives, each between two turns."""

    @property
    def validates(self) -> bool:
        """Whether its episodes run ``validation/``, each round saving the solver."""
        return self.validation_rounds > 0

    async def run(self, task: Task, ctx: EpisodeContext) -> Verdict:
        spec = task.spec
        mode = spec.verify.environment_mode
        if self.agent_enabled:
            self.harness.validate_os(spec.os)
            self.harness.validate_resources(ctx.agent_resources)
        if self.validates:
            _require_validation(task, self.harness if self.agent_enabled else None)
            if not self.agent_enabled:
                raise ConfigError("validation rounds need an agent to receive them")
        image = ctx.prepared_image
        solver = _Solver(await self._timed(ctx, Phase.PROVISION, self._provision(ctx)), image)
        logs = (
            LogMirror(
                solver.sandbox,
                home=ctx.home,
                os=spec.os,
                paths=self.harness.logs,
                target=ctx.run_dir / "logs" / self.harness.name,
            )
            if self.agent_enabled and self.harness.logs
            else None
        )
        try:
            await self._with_deadline(
                ctx, Phase.SETUP, SETUP_TIMEOUT_SEC, self._setup(task, ctx, solver.sandbox)
            )
            budget = _AgentTime(self.timeouts.agent or spec.timeouts.agent)
            if self.agent_enabled:
                await self._agent(task, ctx, solver.sandbox, logs=logs, budget=budget)
            if self.validates:
                await self._validation_rounds(task, ctx, solver, logs=logs, budget=budget)
            sandbox = solver.sandbox
            handle: str | None = None
            try:
                await self._capture_solver_evidence(ctx, sandbox, logs=logs)
                if mode is VerificationMode.SNAPSHOT:
                    solver.saved = True
                    handle = await self._save_solver(ctx, sandbox)
            except BaseException as error:
                # The solver's own: what the agent left is what fails to be copied or saved.
                ctx.failure_phase = Phase.AGENT
                if ctx.agent_stop is not None and isinstance(error, Exception):
                    raise ctx.agent_stop from error
                raise

            async def verify() -> dict[str, float]:
                if mode is VerificationMode.SHARED:
                    return await self._verify(task, ctx, sandbox)
                if handle is None:
                    await self._sanitize_for_retention(ctx, sandbox)
                    await ctx.sandboxes.release(sandbox)
                # A snapshot verifier has the agent's whole disk already: nothing is restored.
                verifier = await self._provision_verifier(ctx, snapshot=handle)
                try:
                    if handle is None:
                        await ctx.artifacts.restore(verifier)
                    return await self._verify(task, ctx, verifier)
                finally:
                    await self._sanitize_for_retention(ctx, verifier)
                    await ctx.sandboxes.release(verifier)

            try:
                rewards = await self._with_deadline(
                    ctx, Phase.VERIFY, self._verify_timeout(ctx), verify()
                )
            except Exception as error:
                if ctx.agent_stop is None:
                    raise
                # What stopped the agent stays the reason this Episode has no score.
                logging.getLogger("ale.execution").warning(
                    "verification after the agent stopped failed",
                    extra={"ale_data": {"error": type(error).__name__}},
                )
                ctx.failure_phase = Phase.AGENT
                raise ctx.agent_stop from error
        finally:
            # Teardown runs on every path, including cancellation.
            await asyncio.shield(
                self._timed(
                    ctx,
                    Phase.TEARDOWN,
                    self._teardown(
                        ctx,
                        solver.sandbox,
                        logs=logs,
                        snapshotted=solver.saved,
                        rounds=solver.rounds,
                    ),
                )
            )

        ctx.verified_rewards = rewards
        scored = await task.score(ctx)
        if ctx.agent_stop is not None:
            ctx.stopped_rewards = scored
            raise ctx.agent_stop
        return Verdict.completed(scored, metrics=ctx.metrics)

    # --- phases ---

    def _solver_request(
        self, ctx: EpisodeContext, *, validations: int, image: PreparedTaskImage | None = None
    ) -> SandboxRequest:
        """The solver's request; one that ``validations`` runs of ``validation/`` still to come
        save asks for a leaf that saves that many snapshots in a row, and more
        (:func:`~ale.core.capability.solver_saves`), and that serves the verifier that boots
        from its snapshot too."""
        spec = ctx.spec
        mode = spec.verify.environment_mode
        network = spec.network
        if ctx.proxy_url and ctx.allowed_hosts and spec.network.mode is not NetworkMode.OPEN:
            network = NetworkPolicy(
                mode=NetworkMode.ALLOWLIST,
                allowed_hosts=tuple(sorted(ctx.allowed_hosts)),
            )
        return SandboxRequest(
            episode_id=ctx.episode_id,
            os=spec.os,
            role=SandboxRole.SHARED if mode is VerificationMode.SHARED else SandboxRole.SOLVER,
            prepared_image=image or ctx.prepared_image,
            snapshot_verifier=spec.verify.resources.as_resources()
            if mode is VerificationMode.SNAPSHOT and spec.verify.resources is not None
            else None,
            round_verifier=round_verifier_resources(spec, validations),
            planned_saves=validations,
            resources=spec.resources,
            network=network,
            gateway_url=ctx.session.gateway_url or None,
            proxy_url=ctx.proxy_url,
            proxy_token=ctx.session.token,
            env={"ALE_EPISODE_ID": ctx.episode_id},
            sudo=spec.resources.sudo,
            user=spec.image.user,
        )

    async def _provision(self, ctx: EpisodeContext) -> Sandbox:
        spec = ctx.spec
        request = self._solver_request(ctx, validations=self.validation_rounds)
        resume = self.resume
        if resume is None:
            sandbox = await ctx.sandboxes.acquire(request)
        elif resume.attach is None:
            sandbox = await ctx.sandboxes.acquire(
                request.model_copy(update={"snapshot": resume.source.handle})
            )
            await _require_snapshot_episode(
                sandbox, spec.os, resume.source.handle, resume.source.episode_id
            )
        else:
            attach_retained = resume.attach

            async def attach(request: SandboxRequest) -> Sandbox:
                # Checked before the lease owns it: a refused Sandbox is not ours to destroy.
                attached = await attach_retained(request)
                await _require_agent_episode(attached, spec.os, resume.source.episode_id)
                return attached

            sandbox = await ctx.sandboxes.adopt(request, attach)
        if resume is not None:
            # Claimed before anything changes it, so the earlier episode's records no
            # longer lead to this Sandbox, whatever happens to this episode.
            await _write_agent_episode(sandbox, spec.os, ctx.episode_id)
            ctx.resumed_from = resume.source
        # The image the Sandbox runs: the request's, or one a Fleet Node prepared for it.
        ctx.prepared_image = sandbox.request.prepared_image
        ctx.resolved_image = sandbox.resolved_image
        ctx.resource_allocation = sandbox.allocation
        # Derived from the account the image declared, not configured anywhere. A run
        # that could choose its own working directory was a second answer to a question
        # the image had already answered, and two answers can disagree.
        ctx.home = sandbox.agent_home
        ctx.sandbox_identity = SandboxProvenance(user=sandbox.agent_user, sudo=spec.resources.sudo)
        return sandbox

    def _verifier_request(
        self, ctx: EpisodeContext, *, snapshot: str | None = None
    ) -> SandboxRequest:
        """A verifier of its own: of its image, or booted from the solver's ``snapshot``, in
        which case its image is the solver's."""
        resources = ctx.spec.verify.resources
        if resources is None or ctx.prepared_verifier_image is None:
            raise TaskError("the verifier's image and resources were not prepared")
        return SandboxRequest(
            episode_id=ctx.episode_id,
            os=ctx.spec.os,
            role=SandboxRole.VERIFIER,
            prepared_image=ctx.prepared_verifier_image,
            snapshot=snapshot,
            resources=resources.as_resources(),
            network=NetworkPolicy(mode=NetworkMode.OPEN),
            gateway_url=ctx.session.gateway_url or None,
            env={"ALE_EPISODE_ID": ctx.episode_id},
            sudo=False,
            user=(ctx.spec.verify.image or ctx.spec.image).user,
        )

    async def _provision_verifier(
        self, ctx: EpisodeContext, *, snapshot: str | None = None
    ) -> Sandbox:
        """The verifier of ``tests/``, recorded in the RunLock."""
        sandbox = await ctx.sandboxes.acquire(self._verifier_request(ctx, snapshot=snapshot))
        ctx.prepared_verifier_image = sandbox.request.prepared_image
        ctx.verifier_resolved_image = sandbox.resolved_image
        ctx.verifier_resource_allocation = sandbox.allocation
        return sandbox

    async def _provision_validation_verifier(
        self, ctx: EpisodeContext, handle: str, image: PreparedTaskImage | None
    ) -> Sandbox:
        """A validation round's verifier, as the Task's verification topology places one:
        booted from the round's snapshot, with the solver's resources and no sudo under
        shared verification and the verifier's under snapshot verification, or of the
        verifier's image under separate verification. Retention never keeps it."""
        spec = ctx.spec
        mode = spec.verify.environment_mode
        if mode is VerificationMode.SHARED:
            image = image or ctx.prepared_image
            assert image is not None, "the solver's image is prepared before the episode"
            resources = round_verifier_resources(spec)
            assert resources is not None
            request = SandboxRequest(
                episode_id=ctx.episode_id,
                os=spec.os,
                role=SandboxRole.VERIFIER,
                prepared_image=image,
                snapshot=handle,
                resources=resources,
                network=NetworkPolicy(mode=NetworkMode.OPEN),
                gateway_url=ctx.session.gateway_url or None,
                env={"ALE_EPISODE_ID": ctx.episode_id},
                sudo=False,
                user=spec.image.user,
            )
        else:
            request = self._verifier_request(
                ctx, snapshot=handle if mode is VerificationMode.SNAPSHOT else None
            )
        return await ctx.sandboxes.acquire(request, validation=True)

    async def _boot_solver(
        self,
        ctx: EpisodeContext,
        handle: str,
        image: PreparedTaskImage | None,
        *,
        validations: int,
    ) -> Sandbox:
        """The next turn's solver, booted from the snapshot a validation round saved, with
        the solver's resources and network; ``validations`` rounds still to come save it in
        turn."""
        request = self._solver_request(ctx, validations=validations, image=image)
        return await ctx.sandboxes.acquire(request.model_copy(update={"snapshot": handle}))

    async def _boot_when_there_is_room(
        self,
        ctx: EpisodeContext,
        handle: str,
        image: PreparedTaskImage | None,
        *,
        validations: int,
    ) -> Sandbox:
        """Boot the next turn's solver, waiting, up to ``BOOT_WAIT_SEC``, while what it
        needs is in use: another Run may take its GPUs while ``validation/`` runs."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BOOT_WAIT_SEC
        while True:
            try:
                return await self._boot_solver(ctx, handle, image, validations=validations)
            except ProviderCapacityError as error:
                if loop.time() + BOOT_RETRY_SEC > deadline:
                    raise ProviderCapacityError(
                        f"the next turn's solver found no room in {BOOT_WAIT_SEC:.0f} s: {error}"
                    ) from error
                logging.getLogger("ale.execution").info(
                    "the next turn's solver waits for room",
                    extra={"ale_data": {"error": str(error)}},
                )
                await asyncio.sleep(BOOT_RETRY_SEC)

    async def _save_solver(
        self, ctx: EpisodeContext, sandbox: Sandbox, *, validation: bool = False
    ) -> str:
        """Sanitize the solver and save its disk as the snapshot its verifier boots from, or
        a ``validation`` round's and the next turn's solver do, which stops it, and return
        the snapshot's handle.

        It runs under a framework bound of its own, as setup does, so that the verifier's
        budget covers only booting the verifier and its tests; the trace attributes it to
        the solver.
        """
        assert ctx.execution is not None
        with execution_logging(
            ctx.execution,
            episode_id=ctx.episode_id,
            phase=Phase.AGENT.value,
            component=self.component,
            secrets=ctx.secrets,
        ):
            bound = asyncio.timeout(SNAPSHOT_TIMEOUT_SEC)
            try:
                async with bound:
                    await self._sanitize_for_retention(ctx, sandbox, for_snapshot=True)
                    return await ctx.sandboxes.snapshot(sandbox, validation=validation)
            except TimeoutError as exc:
                if not bound.expired():
                    # A timeout of the save's own, which says what timed out.
                    raise
                raise ProviderStartError(
                    f"saving the solver's snapshot took more than {SNAPSHOT_TIMEOUT_SEC:.0f} s"
                ) from exc

    async def _setup(self, task: Task, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        """Stage what the task declared, then run its own preparation.

        Only agent-visible material is uploaded: the configuration, the tests and the
        calibration cases stay on the host until they are needed, which is what keeps
        answers out of reach rather than merely out of sight.

        Directories come from the task — its asset destinations and the paths it wants
        collected — so a domain that needs a different layout simply declares one.
        """
        # The task's own preparation is trusted code and is not the thing being measured,
        # so it may reach the network. What the declared policy binds is the agent.
        await sandbox.open_egress()

        folder = getattr(task, "folder", None)
        if folder is None:
            return

        if setup_dir := folder.stage_dir(Stage.SETUP):
            target = _layout(ctx.spec.os).path(Stage.SETUP)
            await sandbox.upload_dir(str(setup_dir), str(target))
            if folder.stage_entry(Stage.SETUP, ctx.spec.os):
                await self._run_stage(ctx, sandbox, target, Phase.SETUP)

    async def _agent(
        self,
        task: Task,
        ctx: EpisodeContext,
        sandbox: Sandbox,
        *,
        logs: LogMirror | None = None,
        budget: _AgentTime | None = None,
        prompt: str | None = None,
        continuation: NativeContinuation | None = None,
        round_index: int | None = None,
    ) -> None:
        """One turn of the agent, with what is left of its time: the first, launched with the
        instruction, or resumed with it in the session a continued episode left; or a later
        one, resumed with ``prompt``, validation round ``round_index``'s, in its own
        ``continuation``."""
        spec = ctx.spec
        ctx.agent_started = True
        budget = budget or _AgentTime(self.timeouts.agent or spec.timeouts.agent)
        timeout = budget.remaining
        first = prompt is None
        if first:
            prompt = self._instruction(spec)
            if self.resume is not None:
                continuation = self.resume.source.continuation

        harness = self.harness
        if isinstance(harness, PolicyHarness):
            try:
                await self._with_deadline(
                    ctx, Phase.AGENT, timeout, self._rollout(harness, ctx, sandbox)
                )
            except _AGENT_STOPS as error:
                ctx.agent_stop = error
            finally:
                if ctx.open_agent_session is not None:
                    ctx.open_agent_session(False)
            return

        oracle = harness if isinstance(harness, OracleHarness) else None
        if oracle is not None and first:
            await self._upload_oracle(ctx, task, sandbox, oracle.case)

        # Installed first, then sealed. Putting the agent's own CLI in place is our
        # preparation, not its work; an image that has not pre-baked one would otherwise
        # be unusable, since the only thing a sealed sandbox can reach is the gateway.
        # The session the agent gets carries the sandbox's own view of the gateway.
        session = ctx.session.model_copy(
            update={
                "gateway_url": sandbox.gateway_url or ctx.session.gateway_url,
                "home": ctx.home,
                "workdir": None if spec.workdir is None else spec.start_directory(ctx.home),
                "sandbox_id": sandbox.sandbox_id,
                "resources_digest": ctx.agent_resources.digest,
            }
        )
        if not first:
            # A booted solver has its CLI in place: sealed before anything is checked or
            # staged in it, so what the agent set to start at boot gets no open egress.
            await self._seal(ctx, sandbox)
        try:
            ctx.agent_version = await harness.install(
                _RecordedSandbox(ctx, sandbox, component="harness-install")
            )
            # Staged each turn from what the Task and Run declared, never from a copy an
            # earlier turn resolved.
            resources = await self._stage_mcp_files(ctx, sandbox)
            await harness.install_resources(
                _RecordedSandbox(ctx, sandbox, component="harness-resources"),
                session,
                resources,
            )
            await self._validate_stdio_mcp(ctx, sandbox, resources)
            if session.workdir is not None and not await self._is_directory(
                ctx, sandbox, session.workdir
            ):
                raise TaskError(f"[agent].workdir is not a directory: {session.workdir}")
        except (TaskError, AgentError, ConfigError) as error:
            if first:
                raise
            # After the first turn these check what the agent left, so what fails is its own
            # work: verified as it stands, as any agent that stopped early.
            stop = AgentError(
                f"{harness.name} could not start its next turn in what the agent left: {error}"
            )
            stop.__cause__ = error
            ctx.agent_stop = stop
            return
        if first:
            await self._seal(ctx, sandbox)

        variables = ctx.variables.values

        async def launch() -> None:
            recorded = _RecordedSandbox(
                ctx,
                sandbox,
                component="harness-launch",
                actor="task" if oracle is not None else "framework",
                env=variables.oracle() if oracle is not None else variables.environment,
            )
            started = time.monotonic()
            if round_index is not None:
                # Only a prompt the agent was given is looked for in its session.
                ctx.validation_prompts[round_index] = prompt
            with _limit_terminated(ctx):
                if ctx.open_agent_session is not None:
                    ctx.open_agent_session(True)
                try:
                    if continuation is None:
                        run = await harness.launch(prompt, recorded, session, timeout_sec=timeout)
                    else:
                        run = await harness.resume(
                            prompt, continuation, recorded, session, timeout_sec=timeout
                        )
                finally:
                    # Only the agent's own run spends its time and its model session: no
                    # save, validation or boot, and nothing its programs left running.
                    budget.used += time.monotonic() - started
                    if ctx.open_agent_session is not None:
                        ctx.open_agent_session(False)
                ctx.agent_run = run
                if run.exit_code != 0:
                    detail = run.final_message or "no diagnostic output"
                    raise AgentError(f"{harness.name} exited {run.exit_code}: {detail}")

        polling = asyncio.create_task(self._poll_logs(logs)) if logs is not None else None
        try:
            backstop = None if timeout is None else timeout + AGENT_STOP_GRACE_SEC
            await self._with_deadline(ctx, Phase.AGENT, backstop, launch())
        except _AGENT_STOPS as error:
            # Past the backstop the guest no longer replies, so nothing there can be verified.
            if isinstance(error, PhaseTimeoutError) and isinstance(error.__cause__, TimeoutError):
                raise
            ctx.agent_stop = error
        finally:
            if polling is not None:
                polling.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await polling

    async def _poll_logs(self, logs: LogMirror) -> None:
        """Copy what the agent appended to its logs while it runs; conversion waits."""
        while True:
            await asyncio.sleep(SYNC_INTERVAL_SEC)
            try:
                async with asyncio.timeout(SYNC_TIMEOUT_SEC):
                    await logs.sync()
            except Exception as exc:
                logging.getLogger("ale.execution").warning(
                    "periodic harness log copy failed; will retry",
                    extra={"ale_data": {"error": type(exc).__name__}},
                )

    def _instruction(self, spec: TaskSpec) -> str:
        """The first prompt: the instruction, verbatim, and, when the agent receives
        validation rounds, the preamble that says how its turns go."""
        if not self.validation_rounds:
            return spec.instruction
        rounds = "once" if self.validation_rounds == 1 else f"{self.validation_rounds} times"
        total = self.timeouts.agent or spec.timeouts.agent
        return spec.instruction + VALIDATION_PREAMBLE.format(rounds=rounds, minutes=_minutes(total))

    async def _validation_rounds(
        self,
        task: Task,
        ctx: EpisodeContext,
        solver: _Solver,
        *,
        logs: LogMirror | None,
        budget: _AgentTime,
    ) -> None:
        """Run ``validation/`` each time a turn of the agent ends normally, and give the
        agent each result as its next turn, in a solver booted from the snapshot the round
        saved; a turn that something else ended, a Gateway limit included, is the last.

        Each round copies the agent's logs, saves the solver, which stops it and every
        process in it, scores the snapshot with ``validation/`` as the Task's verification
        topology places a verifier, and boots the next solver from it without running
        setup again. Once the save succeeds nothing runs on the previous round's snapshot,
        which then goes, so at most one round's snapshot exists while ``validation/`` runs.
        """
        mode = ctx.spec.verify.environment_mode
        logger = logging.getLogger("ale.execution")
        for index in range(1, self.validation_rounds + 1):
            if ctx.agent_stop is not None:
                return
            refusal = ctx.limit_refusal() if ctx.limit_refusal is not None else None
            if refusal is not None:
                # A turn that exited normally after the Gateway refused one of its calls
                # was stopped by that limit.
                ctx.agent_stop = BudgetExceededError(
                    refusal.name, float(refusal.configured_value), refusal.observed_value
                )
                return
            run = ctx.agent_run
            if run is None or run.continuation is None:
                ctx.agent_stop = NativeContinuationError(
                    f"{self.harness.name} reported no native session to resume"
                )
                return
            if budget.remaining <= 0:
                return
            directory = ctx.run_dir / "validation" / str(index)
            artifacts: ArtifactSink | None = None
            # The turn's end is the solver's, as its save for a snapshot verifier is.
            with self._traced(ctx, Phase.AGENT):
                logger.info(
                    "the turn ended; a validation round begins",
                    extra={"ale_data": {"round": index}},
                )
                if logs is not None and ctx.agent_started:
                    await self._copy_logs(logs)
                if mode is VerificationMode.SEPARATE:
                    # Captured before the save, as for a separate verifier of tests/.
                    artifacts = ctx.artifacts.for_validation(directory)
                    await self._collect_artifacts(ctx, solver.sandbox, artifacts)
                solver.saved = True
                try:
                    handle = await self._save_solver(ctx, solver.sandbox, validation=True)
                except BaseException:
                    ctx.failure_phase = Phase.AGENT
                    raise
                earlier, solver.rounds = solver.rounds, [solver.sandbox]
                logger.info(
                    "a validation round saved the solver",
                    extra={"ale_data": {"round": index, "handle": handle}},
                )
                await self._release_rounds(ctx, earlier)
            try:
                feedback = await self._validate(task, ctx, index, handle, solver.image, artifacts)
            finally:
                if artifacts is not None:
                    artifacts.cleanup()

            async def boot(handle: str = handle, index: int = index) -> Sandbox:
                booted = await self._boot_when_there_is_room(
                    ctx, handle, solver.image, validations=max(self.validation_rounds - index, 0)
                )
                logger.info(
                    "a validation round booted the next solver",
                    extra={"ale_data": {"round": index, "handle": handle}},
                )
                return booted

            booted = await self._timed(ctx, Phase.PROVISION, boot())
            solver.sandbox, solver.saved = booted, False
            if logs is not None:
                logs.follow(booted)
            ctx.validation_rounds[-1] = ctx.validation_rounds[-1].model_copy(
                update={"solver_allocation": booted.allocation}
            )
            assert ctx.agent_run is not None and ctx.agent_run.continuation is not None
            prompt = _round_prompt(
                ctx.validation_rounds[-1], self.validation_rounds, feedback, budget.remaining
            )
            await self._agent(
                task,
                ctx,
                booted,
                logs=logs,
                budget=budget,
                prompt=prompt,
                continuation=ctx.agent_run.continuation,
                round_index=index,
            )

    async def _release_rounds(self, ctx: EpisodeContext, saved: Sequence[Sandbox]) -> None:
        """Release the solvers earlier rounds saved, whose snapshots nothing runs on any more.
        One that cannot be destroyed now is left to the lease, which tries again as the
        episode ends and then records it as not destroyed."""
        for sandbox in saved:
            try:
                await ctx.sandboxes.release(sandbox)
            except RetentionFinalizationError as exc:
                logging.getLogger("ale.execution").warning(
                    "a validation round's snapshot could not be destroyed yet",
                    extra={"ale_data": {"error": str(exc)}},
                )

    def _traced(self, ctx: EpisodeContext, phase: Phase) -> contextlib.AbstractContextManager[None]:
        """Record what is logged in the execution trace, attributed to ``phase``."""
        assert ctx.execution is not None
        return execution_logging(
            ctx.execution,
            episode_id=ctx.episode_id,
            phase=phase.value,
            component=self.component,
            secrets=ctx.secrets,
        )

    async def _copy_logs(self, logs: LogMirror) -> None:
        """Copy the agent's logs as a turn ends; the final copy confirms them later."""
        try:
            async with asyncio.timeout(FINAL_SYNC_TIMEOUT_SEC):
                await logs.sync()
        except Exception as exc:
            logging.getLogger("ale.execution").warning(
                "harness log copy at the end of a turn failed; the final copy retries",
                extra={"ale_data": {"error": type(exc).__name__}},
            )

    async def _validate(
        self,
        task: Task,
        ctx: EpisodeContext,
        index: int,
        handle: str,
        image: PreparedTaskImage | None,
        artifacts: ArtifactSink | None,
    ) -> str | None:
        """Score the snapshot ``handle`` with ``validation/`` under the verifier's budget and
        record the round; return the feedback text the agent receives.

        A round that produces no score is recorded with its failure, and the agent receives
        it as such: the round counts, and the episode goes on.
        """
        directory = ctx.run_dir / "validation" / str(index)
        started_at = datetime.now(UTC)
        failure_phase = ctx.failure_phase

        verifier: Sandbox | None = None

        async def run() -> tuple[float, str | None]:
            nonlocal verifier
            verifier = await self._provision_validation_verifier(ctx, handle, image)
            if artifacts is not None:
                await artifacts.restore(verifier)
            return await self._score(task, ctx, verifier, Stage.VALIDATION, directory)

        outcome: tuple[float, str | None] | Exception
        try:
            outcome = await self._with_deadline(
                ctx, Phase.VALIDATE, self._verify_timeout(ctx), run()
            )
        except Exception as error:
            outcome = error
        unreleased: RetentionFinalizationError | None = None
        if verifier is not None:
            # Outside what makes the round unscored: a verifier that cannot be destroyed ends
            # the episode, as the verifier of tests/ does, once the round is recorded.
            try:
                await ctx.sandboxes.release(verifier)
            except RetentionFinalizationError as exc:
                unreleased = exc
        if isinstance(outcome, RetentionFinalizationError):
            raise outcome
        feedback = self._record_round(ctx, index, directory, started_at, failure_phase, outcome)
        if unreleased is not None:
            raise unreleased
        return feedback

    def _record_round(
        self,
        ctx: EpisodeContext,
        index: int,
        directory: Path,
        started_at: datetime,
        failure_phase: Phase | None,
        outcome: tuple[float, str | None] | Exception,
    ) -> str | None:
        """Record round ``index``'s ``outcome``, its score or why it has none, and return the
        feedback text the agent receives."""
        logger = logging.getLogger("ale.execution")
        if isinstance(outcome, Exception):
            error = outcome
            # The episode goes on, so the failure is the round's, not the episode's.
            ctx.failure_phase = failure_phase
            status = _round_status(error)
            message = Redactor(ctx.secrets)(str(error))
            ctx.validation_rounds.append(
                ValidationRound(
                    index=index,
                    status=status,
                    failure=FailureInfo(
                        error_type=type(error).__name__,
                        message=message,
                        phase=Phase.VALIDATE.value,
                    ),
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )
            )
            with self._traced(ctx, Phase.VALIDATE):
                logger.warning(
                    "a validation round produced no score",
                    extra={"ale_data": {"round": index, "status": status.value, "error": message}},
                )
            return None
        reward, feedback = outcome
        record = None
        if feedback is not None:
            path = directory / "feedback.md"
            data = path.read_bytes()
            record = ValidationFeedback(
                path=path.relative_to(ctx.run_dir).as_posix(),
                digest=f"sha256:{hashlib.sha256(data).hexdigest()}",
                size_bytes=len(data),
            )
        ctx.validation_rounds.append(
            ValidationRound(
                index=index,
                status=Status.COMPLETED,
                reward=reward,
                feedback=record,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )
        )
        with self._traced(ctx, Phase.VALIDATE):
            logger.info(
                "a validation round scored", extra={"ale_data": {"round": index, "reward": reward}}
            )
        return feedback

    async def _stage_mcp_files(
        self, ctx: EpisodeContext, sandbox: Sandbox
    ) -> EffectiveAgentResources:
        """Stage the declared MCP servers' files and return the resources as the agent's
        Sandbox runs them, with ``{mcp}`` resolved; ``ctx.agent_resources`` stays as
        declared, so each turn stages it again from the start."""
        resolved = []
        for item in ctx.agent_resources.mcp_servers:
            server = item.server
            if item.staged_files is not None and isinstance(server, StdioMcpServer):
                target = _sandbox_path(ctx.spec.os, ctx.home, ".ale-mcp", item.name)
                await sandbox.upload_dir(
                    str(item.staged_files),
                    target,
                    identity=Identity.AGENT,
                )
                if ctx.spec.os in (
                    OperatingSystem.LINUX,
                    OperatingSystem.MACOS,
                ) and server.command.startswith("{mcp}/"):
                    # The declared command, rather than a Host mode, requests execution.
                    executable = server.command.replace("{mcp}", target)
                    result = await sandbox.exec(
                        ["chmod", "--", "u+x", executable], identity=Identity.AGENT
                    )
                    if not result.ok:
                        raise TaskError(f"could not prepare MCP command: {executable}")
                server = server.model_copy(
                    update={
                        "command": (
                            "python.exe"
                            if ctx.spec.os is OperatingSystem.WINDOWS and item.name == "cua-desktop"
                            else server.command.replace("{mcp}", target)
                        ),
                        "args": tuple(arg.replace("{mcp}", target) for arg in server.args),
                        "cwd": server.cwd.replace("{mcp}", target) if server.cwd else None,
                        "environment": {
                            key: value.replace("{mcp}", target)
                            for key, value in server.environment.items()
                        },
                    }
                )
                item = item.model_copy(update={"server": server})
            resolved.append(item)
        return ctx.agent_resources.model_copy(update={"mcp_servers": tuple(resolved)})

    async def _rollout(self, harness, ctx: EpisodeContext, sandbox: Sandbox) -> None:  # type: ignore[no-untyped-def]
        """Hand the agent a stepwise view of the sandbox and let it drive.

        The loop used to live here, which meant an agent that wanted to drive had to be
        inverted through queues to fit. Now it drives, and the protocol is still the
        only thing touching the sandbox — so every observation and action is witnessed,
        and the ceilings hold for an agent that never heard of them.
        """
        # The session is used as-is, unlike the autonomous path. A stepwise harness runs
        # in this process and drives the sandbox from outside, so it reaches the gateway
        # at the host's own address; rewriting it to the sandbox's view hands a host-side
        # agent a hostname that only resolves inside a container.
        ctx.agent_version = await harness.install(
            _RecordedSandbox(ctx, sandbox, component="harness-install")
        )
        await self._seal(ctx, sandbox)

        assert ctx.blobs is not None
        assert ctx.trajectory is not None
        builder = TrajectoryBuilder(
            trajectory_id=ctx.trajectory_id,
            session_id=ctx.episode_id,
            agent=AtifAgent(
                name=harness.name,
                version=ctx.agent_version or harness.version(),
                model_name=ctx.session.model or None,
            ),
        )
        builder.add(source="user", message=ctx.spec.instruction)
        async with SandboxEnv(
            sandbox,
            instruction=ctx.spec.instruction,
            trajectory=builder,
            blobs=ctx.blobs,
            max_steps=self.max_steps,
            stall_limit=self.stall_limit,
        ) as env:
            with _limit_terminated(ctx):
                closing = await harness.rollout(
                    env, ctx.session.model_copy(update={"home": ctx.home})
                )
        if closing:
            builder.add(source="agent", message=closing)
        self._persist_trajectory(ctx, builder.build())
        ctx.trajectory_written = True

    async def _seal(self, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        """Apply the declared policy, and record that it was applied.

        A failure here ends the episode. An agent that ran with a network it was never
        meant to have is not a result with a caveat — it is a different experiment, and
        the lock file would describe the one that was intended rather than the one that
        happened.
        """
        assert ctx.execution is not None
        try:
            await sandbox.close_egress()
        except BaseException:
            ctx.execution.append(
                PolicyApplied(
                    episode_id=ctx.episode_id,
                    phase=Phase.AGENT.value,
                    component="network-policy",
                    level="error",
                    policy="sealed",
                    requested_mode=ctx.spec.network.mode.value,
                    succeeded=False,
                ),
                durable=True,
            )
            raise
        else:
            ctx.execution.append(
                PolicyApplied(
                    episode_id=ctx.episode_id,
                    phase=Phase.AGENT.value,
                    component="network-policy",
                    policy="sealed",
                    requested_mode=ctx.spec.network.mode.value,
                    succeeded=True,
                ),
                durable=True,
            )

    async def _validate_stdio_mcp(
        self, ctx: EpisodeContext, sandbox: Sandbox, resources: EffectiveAgentResources
    ) -> None:
        for resolved in resources.mcp_servers:
            server = resolved.server
            if not isinstance(server, StdioMcpServer):
                continue
            command = server.command.replace("{home}", ctx.home)
            if ctx.spec.os is OperatingSystem.WINDOWS:
                quoted = command.replace("'", "''")
                result = await sandbox.exec(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"if (-not (Get-Command '{quoted}' -ErrorAction SilentlyContinue)) "
                        "{ exit 1 }",
                    ],
                    identity=Identity.AGENT,
                )
            elif command.startswith("/"):
                result = await sandbox.exec(["test", "-x", command], identity=Identity.AGENT)
            else:
                result = await sandbox.exec(
                    ["sh", "-c", 'command -v "$1" >/dev/null', "sh", command],
                    identity=Identity.AGENT,
                )
            if not result.ok:
                raise TaskError(
                    f"MCP server {resolved.name!r} command is not executable: {command}"
                )
            if server.cwd:
                cwd = server.cwd.replace("{home}", ctx.home)
                if not await self._is_directory(ctx, sandbox, cwd):
                    raise TaskError(f"MCP server {resolved.name!r} cwd is not a directory: {cwd}")

    async def _is_directory(self, ctx: EpisodeContext, sandbox: Sandbox, path: str) -> bool:
        if ctx.spec.os is OperatingSystem.WINDOWS:
            command = _powershell(
                f"if (-not (Test-Path -LiteralPath {_powershell_literal(path)} "
                "-PathType Container)) { exit 1 }"
            )
        else:
            command = ["test", "-d", path]
        return (await sandbox.exec(command, identity=Identity.AGENT)).ok

    async def _verify(self, task: Task, ctx: EpisodeContext, sandbox: Sandbox) -> dict[str, float]:
        """Score the episode using the task's own tests.

        A crash or malformed output here is a task defect, reported as ``task_error``:
        deliberately distinguishable from an agent that simply scored zero.
        """
        reward, _ = await self._score(task, ctx, sandbox, Stage.TESTS, ctx.run_dir)
        return {"reward": reward}

    async def _score(
        self, task: Task, ctx: EpisodeContext, sandbox: Sandbox, stage: Stage, records: Path
    ) -> tuple[float, str | None]:
        """Run a verifier stage, ``tests/`` or ``validation/``, in ``sandbox`` and return its
        reward and feedback text, recording its result and Judge logs in ``records``."""
        folder = getattr(task, "folder", None)
        stage_dir = folder.stage_dir(stage) if folder else None
        if stage_dir is None:
            raise TaskError(f"task has no {stage} stage")

        layout = _layout(ctx.spec.os)
        root = layout.path(stage)
        removed = await self._clear_stages(ctx, sandbox, (stage,))
        if not removed.ok:
            raise TaskError(
                f"could not clear the previous {stage} stage: "
                + (removed.stderr.strip() or f"exit {removed.exit_code}")
            )
        # Whatever the agent or an earlier attempt left there is no result of this one.
        reset = await sandbox.exec(_verifier_dir_command(ctx.spec.os, remove=False))
        if not reset.ok:
            raise TaskError(
                f"could not prepare {_verifier_dir(ctx.spec.os)}: "
                + (reset.stderr.strip() or f"exit {reset.exit_code}")
            )
        await sandbox.upload_dir(str(stage_dir), str(root))
        # Converted once, after the agent's last turn: a validation round has none yet.
        trajectory = ctx.run_dir / "trajectory.json"
        if stage is Stage.TESTS and trajectory.is_file():
            await sandbox.write_file(
                str(layout.path(stage, "trajectory.json")), trajectory.read_bytes()
            )
        await sandbox.write_file(
            str(layout.path(stage, "instruction.md")), ctx.spec.instruction.encode("utf-8")
        )
        await self._stage_verification_config(ctx, sandbox, stage)
        # Scoring is the framework's own work, and a verifier may need to reach something
        # the agent could not. The agent has already finished; nothing it does can follow.
        await sandbox.open_egress()

        python, destination = await self._site_packages(ctx, sandbox)
        framework_source, framework_version, framework_hash = installed_ale_verify()
        await sandbox.upload_dir(
            str(framework_source),
            f"{destination}/ale_verify",
        )
        # Stage the one pure-Python schema dependency with the verifier package.
        import fastjsonschema

        await sandbox.upload_dir(
            str(Path(fastjsonschema.__file__).parent), f"{destination}/fastjsonschema"
        )
        await self._probe_package(sandbox, python, "ale-verify", "import ale_verify")
        ctx.ale_verify_provenance = AleVerifyProvenance(
            version=framework_version,
            content_hash=framework_hash,
        )
        phase = Phase.VERIFY if stage is Stage.TESTS else Phase.VALIDATE
        invocations: tuple[JudgeInvocation, ...] = ()
        if ctx.open_judge_sessions is not None:
            ctx.open_judge_sessions(True)
        try:
            exit_code = await self._run_stage(ctx, sandbox, root, phase, verify_python=python)
        finally:
            if ctx.open_judge_sessions is not None:
                ctx.open_judge_sessions(False)
            # Keep Judge evidence even if the task exits, raises or times out.
            invocations = await self._collect_judge_logs(ctx, sandbox, stage, records)
            if stage is Stage.TESTS:
                ctx.judge_invocations = invocations
            else:
                ctx.validation_judge_invocations += invocations
        if exit_code != 0:
            if any(item.status == "failed" for item in invocations):
                raise VerificationInfrastructureError(
                    f"{stage}/'s Judge verification failed with exit code {exit_code}"
                )
            raise TaskError(f"{stage}/ failed with exit code {exit_code}")
        result = await self._read_result(ctx, sandbox, records, stage)
        # Only a validation round shows feedback; tests/'s is never read.
        feedback = (
            await self._read_feedback(ctx, sandbox, records) if stage is Stage.VALIDATION else None
        )
        return float(result["reward"]), feedback

    async def _stage_verification_config(
        self, ctx: EpisodeContext, sandbox: Sandbox, stage: Stage = Stage.TESTS
    ) -> None:
        """Point the verifier's Judges at this episode's Gateway sessions.

        The verifier gets the Gateway's address and a session token per Judge; the Judge's
        own key stays on the Host, where the Gateway adds it to each forwarded call.
        """
        payload, command_env = verifier_judge_config(
            ctx.verification_config, ctx.judge_tokens, sandbox.gateway_url
        )
        await sandbox.write_file(
            str(_layout(ctx.spec.os).path(stage, "config.json")),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        ctx.verification_command_env = command_env
        ctx.verification_secrets = tuple(command_env.values())

    async def _collect_judge_logs(
        self, ctx: EpisodeContext, sandbox: Sandbox, stage: Stage, records: Path
    ) -> tuple[JudgeInvocation, ...]:
        """Copy a verifier stage's Judge logs into ``records`` and return what they invoked."""
        redactor = Redactor(ctx.secrets)
        invocations = []
        for name in ("llm-judge.jsonl", "agent-judge.jsonl"):
            try:
                raw = await sandbox.read_file(str(_layout(ctx.spec.os).path(stage, name)))
            except Exception:
                continue
            text = redactor(raw.decode("utf-8", errors="replace"))
            target = records / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            for line in text.splitlines():
                try:
                    event = json.loads(line)
                    value = event.get("invocation")
                    if value:
                        invocations.append(JudgeInvocation.from_dict(value))
                except (ValueError, TypeError, AttributeError, KeyError):
                    # A killed process can leave a partial final line; preserve it.
                    continue
        return tuple(invocations)

    async def _read_result(
        self,
        ctx: EpisodeContext,
        sandbox: Sandbox,
        records: Path | None = None,
        stage: Stage = Stage.TESTS,
    ) -> dict[str, object]:
        """The result ``stage`` wrote: ``reward.json``, completed by ``verification.json`` if
        any, recorded in ``records``, the episode's directory unless a validation round's."""
        directory = _verifier_dir(ctx.spec.os)
        reward, record = str(directory / "reward.json"), str(directory / "verification.json")
        try:
            written = {item.path for item in await sandbox.list_files(str(directory))}
            raw = {
                path: await sandbox.read_file(path) for path in (reward, record) if path in written
            }
        except GuestUnreachableError:
            raise  # the Sandbox was lost, which is no defect of what the verifier wrote
        except Exception as exc:
            raise VerifierOutputError(f"{stage}/'s result could not be read: {exc}") from exc
        if reward not in raw:
            raise VerifierOutputError(f"{stage}/ wrote no result to {reward}")
        for value in raw.values():
            if any(secret.encode() in value for secret in ctx.verification_secrets if secret):
                raise VerifierOutputError(f"{stage}/'s result contains a configured credential")
        try:
            result = read_result(raw[reward], raw.get(record))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise VerifierOutputError(f"{stage}/ wrote an invalid result: {exc}") from exc
        result = Redactor(ctx.secrets, patterns=False).json(result)
        atomic_write_json((records or ctx.run_dir) / "verification.json", result, sort_keys=False)
        return result

    async def _read_feedback(
        self, ctx: EpisodeContext, sandbox: Sandbox, records: Path
    ) -> str | None:
        """A validation round's feedback text, ``feedback.md`` beside its result, recorded in
        ``records``; none when it wrote none.

        The agent is shown it, so it is held to what a result is: one that holds a Judge's
        token, is not UTF-8 or exceeds ``MAX_FEEDBACK_BYTES`` fails the round's verification
        as a Task error, and other credentials are redacted.
        """
        directory = _verifier_dir(ctx.spec.os)
        path = str(directory / "feedback.md")
        try:
            written = {item.path for item in await sandbox.list_files(str(directory))}
            if path not in written:
                return None
            raw = await sandbox.read_file(path)
        except GuestUnreachableError:
            raise
        except Exception as exc:
            raise VerifierOutputError(f"validation/'s feedback could not be read: {exc}") from exc
        if len(raw) > MAX_FEEDBACK_BYTES:
            raise VerifierOutputError(
                f"{path} holds {len(raw)} bytes, more than the {MAX_FEEDBACK_BYTES} it may"
            )
        if any(secret.encode() in raw for secret in ctx.verification_secrets if secret):
            raise VerifierOutputError("validation/'s feedback contains a configured credential")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VerifierOutputError(f"{path} is not UTF-8: {exc}") from exc
        text = Redactor(ctx.secrets, patterns=False)(text)
        target = records / "feedback.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return text

    async def _capture_solver_evidence(
        self,
        ctx: EpisodeContext,
        sandbox: Sandbox,
        *,
        logs: LogMirror | None = None,
    ) -> None:
        if ctx.solver_evidence_captured:
            return
        logger = logging.getLogger("ale.execution")
        # The harness's own logs, in their own place. What the agent produced and how the
        # harness went about producing it are different questions with different owners:
        # the task declares the first because only it knows what its output is, and the
        # harness declares the second because only it knows what it writes.
        collection_failed = False
        if logs is not None and ctx.agent_started:
            try:
                async with asyncio.timeout(FINAL_SYNC_TIMEOUT_SEC):
                    await logs.sync(final=True)
            except Exception as exc:
                collection_failed = True
                logger.warning(
                    "final harness log copy failed",
                    extra={"ale_data": {"error": type(exc).__name__}},
                )
            collection_failed = collection_failed or logs.incomplete
            # Copied while the agent ran, so only now can the copies lose what they held.
            redact_files(
                ctx.run_dir / "logs" / self.harness.name, Redactor(ctx.secrets, patterns=False)
            )

        if ctx.agent_started and not ctx.trajectory_written:
            self._parse_harness_trajectory(ctx, collection_failed=collection_failed)

        await self._collect_artifacts(ctx, sandbox, ctx.artifacts)
        ctx.solver_evidence_captured = True

    async def _collect_artifacts(
        self, ctx: EpisodeContext, sandbox: Sandbox, sink: ArtifactSink
    ) -> None:
        """Copy every declared artifact from ``sandbox`` into ``sink``, when it collects."""
        if not sink.enabled:
            return
        path_type = PureWindowsPath if ctx.spec.os is OperatingSystem.WINDOWS else PurePosixPath
        for index, artifact in enumerate(ctx.spec.artifacts):
            path = ctx.spec.artifact_path(artifact, ctx.home)
            name = path_type(path).name or f"artifact-{index}"
            await sink.collect(sandbox, path, name, artifact.exclude)
            logging.getLogger("ale.execution").info(
                "artifact collection attempted",
                extra={"ale_data": {"source": path, "name": name}},
            )

    async def _teardown(
        self,
        ctx: EpisodeContext,
        sandbox: Sandbox,
        *,
        logs: LogMirror | None = None,
        snapshotted: bool = False,
        rounds: Sequence[Sandbox] = (),
    ) -> None:
        """Capture what an interrupted agent left, then release the solver: sanitized first,
        unless it was ``snapshotted`` for its verifier or a validation round, which stopped
        it, and whose snapshot the lease releases in its place; then the solvers of earlier
        rounds, whose snapshots go once nothing runs on them."""
        # Best-effort interrupted evidence does not replace the primary failure.
        if ctx.agent_started and not ctx.solver_evidence_captured:
            try:
                await self._capture_solver_evidence(ctx, sandbox, logs=logs)
            except Exception as exc:
                logging.getLogger("ale.execution").warning(
                    "interrupted solver evidence capture failed",
                    extra={"ale_data": {"error": str(exc)}},
                )
        if not snapshotted:
            await self._sanitize_for_retention(ctx, sandbox)
        await ctx.sandboxes.release(sandbox)
        await self._release_rounds(ctx, rounds)
        logging.getLogger("ale.execution").info("sandbox released")

    async def _sanitize_for_retention(
        self, ctx: EpisodeContext, sandbox: Sandbox, *, for_snapshot: bool = False
    ) -> bool:
        """Leave only the agent's own state in a Sandbox that outlives its episode, or whose
        disk is saved ``for_snapshot`` verification, whatever its retention."""
        if sandbox.request.retention == "destroy" and not for_snapshot:
            ctx.sandboxes.mark_sanitized(sandbox, succeeded=True)
            return True
        # A retained solver may be handed to another agent, so no stage or result of this
        # Task stays behind, only the agent's own state.
        result = await self._clear_stages(ctx, sandbox, (Stage.SETUP, Stage.TESTS))
        if result.ok:
            result = await self._remove_paths(
                ctx,
                sandbox,
                (
                    str(solution_dir(ctx.home, ctx.spec.os)),
                    *(
                        _sandbox_path(ctx.spec.os, ctx.home, *path.split("/"))
                        # A reverification has no Harness of its own.
                        for path in sorted(
                            {*credential_files(), *getattr(self.harness, "credentials", ())}
                        )
                    ),
                ),
            )
        if result.ok:
            result = await sandbox.exec(_verifier_dir_command(ctx.spec.os, remove=True))
        reason = None if result.ok else result.stderr.strip() or "retention sanitation failed"
        solver = sandbox.request.role is not SandboxRole.VERIFIER
        if reason is None and self.agent_enabled and solver:
            try:
                await _write_agent_episode(sandbox, ctx.spec.os, ctx.episode_id)
            except Exception as exc:
                reason = f"could not record the agent's episode: {exc}"
        ctx.sandboxes.mark_sanitized(sandbox, succeeded=reason is None, reason=reason)
        return reason is None

    async def _clear_stages(
        self,
        ctx: EpisodeContext,
        sandbox: Sandbox,
        names: Sequence[str],
    ) -> ExecResult:
        """Empty the framework's stage directories.

        A Windows base creates each one with its own ACL under the protected ALE root, so
        there only its contents go.
        """
        paths = [str(_layout(ctx.spec.os).path(name)) for name in names]
        if ctx.spec.os is not OperatingSystem.WINDOWS:
            return await self._remove_paths(ctx, sandbox, paths)
        return await sandbox.exec(
            _powershell(
                f"foreach ($stage in {_powershell_list(paths)}) {{ "
                "if (Test-Path -LiteralPath $stage) { "
                "Get-ChildItem -LiteralPath $stage -Force -ErrorAction Stop | "
                "Remove-Item -Recurse -Force -ErrorAction Stop } }"
            )
        )

    async def _remove_paths(
        self,
        ctx: EpisodeContext,
        sandbox: Sandbox,
        paths: Sequence[str],
    ) -> ExecResult:
        """Remove paths, skipping missing ones as ``rm -rf`` does; any other failure fails."""
        if ctx.spec.os is OperatingSystem.WINDOWS:
            # Not -ErrorAction SilentlyContinue: it hides the error but still exits 1.
            return await sandbox.exec(
                _powershell(
                    f"foreach ($path in {_powershell_list(paths)}) {{ "
                    "if (Test-Path -LiteralPath $path) { "
                    "Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop } }"
                )
            )
        return await sandbox.exec(["rm", "-rf", "--", *paths])

    def _parse_harness_trajectory(
        self, ctx: EpisodeContext, *, collection_failed: bool = False
    ) -> None:
        """The one conversion, after the final log copy, with links, accounting and retention."""
        trajectory = self._convert_harness_trajectory(ctx, collection_failed=collection_failed)
        self._persist_trajectory(ctx, trajectory)
        ctx.trajectory_written = True
        ale = (trajectory.extra or {}).get("ale") or {}
        if (
            ctx.agent_run is not None
            and not ale.get("incomplete")
            and ctx.logging_policy.native_logs == "minimal"
        ):
            shutil.rmtree(ctx.run_dir / "logs" / self.harness.name, ignore_errors=True)

    def _convert_harness_trajectory(
        self, ctx: EpisodeContext, *, collection_failed: bool = False
    ) -> AtifTrajectory:
        assert ctx.blobs is not None
        run = ctx.agent_run
        incomplete_reason: str | None = None
        if run is None:
            incomplete_reason = "agent_interrupted"
        elif collection_failed:
            incomplete_reason = "collection_failed"
        context = TrajectoryParseContext(
            episode_id=ctx.episode_id,
            trajectory_id=ctx.trajectory_id,
            # The first prompt as sent, the preamble of validation rounds included.
            instruction=self._instruction(ctx.spec),
            logs_dir=ctx.run_dir / "logs" / self.harness.name,
            model=ctx.session.model,
            agent_version=ctx.agent_version or self.harness.version(),
            blobs=ctx.blobs,
            session_id=run.continuation.native_session_id if run and run.continuation else None,
            final_message=run.final_message if run else None,
            incomplete=incomplete_reason is not None,
            incomplete_reason=incomplete_reason,
        )
        try:
            trajectory = self.harness.parse_trajectory(context)
            if self.resume is not None:
                trajectory = _continued_segment(trajectory, self.resume)
            trajectory = _with_validation_rounds(trajectory, ctx.validation_prompts)
        except Exception as exc:
            # The agent finished; a converter defect must not decide its score. Keep the
            # minimal truthful trajectory, say why, and keep the native evidence.
            detail = Redactor(ctx.secrets)(f"{type(exc).__name__}: {exc}")[:500]
            logging.getLogger("ale.execution").warning(
                "trajectory conversion failed",
                extra={"ale_data": {"harness": self.harness.name, "error": detail}},
            )
            fallback = Harness.parse_trajectory(
                self.harness,
                replace(context, incomplete=True, incomplete_reason="conversion_failed"),
            )
            # The fallback is what the parser could not replace, whatever else happened.
            return _with_parse_issue(
                _mark_incomplete(fallback, "conversion_failed"),
                {"reason": "conversion_failed", "detail": detail},
            )
        if incomplete_reason is not None:
            trajectory = _mark_incomplete(trajectory, incomplete_reason)
        return trajectory

    def _persist_trajectory(self, ctx: EpisodeContext, trajectory: AtifTrajectory) -> None:
        """Correlate only observed provider/native IDs, then persist before links."""
        assert ctx.trajectory is not None
        calls = {
            record["provider_response_id"]: record["call_id"]
            for record in read_jsonl(ctx.run_dir / "trace.transport.jsonl").records
            if record.get("kind") == "call"
            and record.get("purpose", "agent") == "agent"
            and record.get("provider_response_id")
            and record.get("call_id")
        }
        links: list[tuple[str, int]] = []
        steps = []
        for step in trajectory.steps:
            extra = dict(step.extra or {})
            ale = dict(extra.get("ale") or {})
            call_ids = [
                calls[native_id]
                for native_id in ale.get("native_event_ids", ())
                if native_id in calls
            ]
            if call_ids:
                ale["transport_call_ids"] = call_ids
                extra["ale"] = ale
                updates: dict[str, object] = {"extra": extra}
                if len(call_ids) == 1:
                    exact = _exact_token_metrics(ctx.run_dir, call_ids[0])
                    if exact is not None:
                        updates["metrics"] = _merge_metrics(step.metrics, exact)
                step = step.model_copy(update=updates)
                links.extend((call_id, step.step_id) for call_id in call_ids)
            steps.append(step)
        trajectory = AtifTrajectory.model_validate(
            Redactor(ctx.secrets, patterns=False).json(
                trajectory.model_copy(update={"steps": steps}).model_dump(
                    mode="json", exclude_none=True
                )
            )
        )
        ctx.trajectory.write_trajectory(trajectory)
        if ctx.transport is not None:
            for call_id, step_id in links:
                ctx.transport.append(
                    TrajectoryLink(
                        episode_id=ctx.episode_id,
                        call_id=call_id,
                        trajectory_id=ctx.trajectory_id,
                        step_id=step_id,
                    ),
                    durable=True,
                )

    # --- helpers ---

    async def _run_stage(
        self,
        ctx: EpisodeContext,
        sandbox: Sandbox,
        directory: PurePath,
        phase: Phase,
        *,
        verify_python: str | None = None,
    ) -> int:
        layout = _layout(ctx.spec.os)
        stage = {Phase.SETUP: Stage.SETUP, Phase.VALIDATE: Stage.VALIDATION}.get(phase, Stage.TESTS)
        entry = directory / stage.entry(ctx.spec.os)
        variables = ctx.variables.values
        env = {
            **(
                variables.environment
                if phase is Phase.SETUP
                else variables.verification(ctx.spec.verify.environment_mode)
            ),
            "ALE_STAGE_DIR": str(directory),
            "ALE_HOME": ctx.home,
        }
        if phase is not Phase.SETUP:
            env.update(
                {
                    "ALE_EPISODE_ID": ctx.episode_id,
                    "ALE_LLM_JUDGE_LOG_PATH": str(layout.path(stage, "llm-judge.jsonl")),
                    "ALE_VERIFY_CONFIG_PATH": str(layout.path(stage, "config.json")),
                    "ALE_TASK_INSTRUCTION_PATH": str(layout.path(stage, "instruction.md")),
                    "ALE_AGENT_JUDGE_LOG_PATH": str(layout.path(stage, "agent-judge.jsonl")),
                    **ctx.verification_command_env,
                }
            )
            if verify_python is not None:
                env["ALE_VERIFY_PYTHON"] = verify_python
            if stage is Stage.TESTS and (ctx.run_dir / "trajectory.json").is_file():
                env["ALE_TRAJECTORY_PATH"] = str(layout.path(stage, "trajectory.json"))
            # Judges retry provider failures until this budget runs out, so a verdict is
            # recorded as failed before the phase deadline kills the verifier.
            if ctx.phase_deadline is not None:
                remaining = max(0, int(ctx.phase_deadline - time.monotonic()))
                env["ALE_VERIFY_DEADLINE_SECONDS"] = str(remaining)
        assert ctx.execution is not None
        assert ctx.blobs is not None
        execution_id = f"{phase.value}-{len(ctx.phases) + 1}"
        argv = (
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                entry.name,
            ]
            if ctx.spec.os is OperatingSystem.WINDOWS
            else ["bash", entry.name]
        )
        recorder = CommandRecorder(
            execution=ctx.execution,
            blobs=cast(BlobStore, ctx.blobs),
            episode_id=ctx.episode_id,
            phase=phase.value,
            component="task-stage",
            execution_id=execution_id,
            actor="task",
            argv=argv,
            cwd=str(directory),
            secrets=ctx.secrets,
        )
        started = time.monotonic()
        timeout_sec = ctx.phase_timeout_sec
        try:
            result = await sandbox.exec(
                argv,
                cwd=str(directory),
                env=env,
                timeout_sec=timeout_sec,
                output_sink=recorder.write,
            )
        except asyncio.CancelledError:
            recorder.finish(
                ExecResult(
                    exit_code=None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                ),
                outcome="cancelled",
            )
            raise
        recorder.finish(result)
        if result.timed_out:
            raise PhaseTimeoutError(phase.value, timeout_sec or 0)
        if result.exit_code != 0 and phase is Phase.SETUP:
            raise TaskError(
                f"setup failed with exit code {result.exit_code}: {result.stderr[-500:]}"
            )
        return result.exit_code

    async def _probe_package(
        self, sandbox: Sandbox, python: str, name: str, statement: str
    ) -> None:
        result = await sandbox.exec([python, "-c", statement])
        if not result.ok:
            raise TaskError(
                f"package {name!r} is incompatible with the selected image: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    async def _site_packages(self, ctx: EpisodeContext, sandbox: Sandbox) -> tuple[str, str]:
        """Select the verifier interpreter and its package destination."""
        destination = (
            "site.getusersitepackages()"
            if ctx.spec.os is OperatingSystem.WINDOWS
            else "sysconfig.get_paths()['purelib']"
        )
        script = (
            "import sys,sysconfig,site;"
            "assert sys.version_info >= (3,12), "
            "f'ale_verify requires Python 3.12+, got {sys.version.split()[0]}';"
            f"print({destination})"
        )
        # The image's own Python first, since that is where the Task's packages are; the
        # framework's own when the image has none recent enough.
        candidates = (
            ("python.exe",) if ctx.spec.os is OperatingSystem.WINDOWS else ("python3", "python3.12")
        ) + (guestd.PYTHON[ctx.spec.os.value],)
        failures = []
        for python in candidates:
            argv = [python, "-c", script]
            if ctx.spec.os is not OperatingSystem.WINDOWS:
                # A missing executable must be a normal probe failure on every Provider.
                argv = ["sh", "-c", 'command -v "$1" >/dev/null 2>&1 && exec "$@"', "sh", *argv]
            result = await sandbox.exec(argv)
            path = result.stdout.strip()
            if result.ok and path:
                return python, path
            failures.append(f"{python}: {result.stderr.strip() or path or 'not available'}")
        raise TaskError(
            "verify-stage Python must be Python 3.12+ and expose site-packages: "
            + "; ".join(failures)
        )

    async def _upload_oracle(
        self, ctx: EpisodeContext, task: Task, sandbox: Sandbox, case: str
    ) -> None:
        folder = getattr(task, "folder", None)
        source = folder.case_dir(case) if folder else None
        if source is None:
            raise TaskError(
                f"the oracle runs the {case} case, and this task has no solution for it"
            )
        # As the agent, since the agent is the account that will run it.
        root = solution_dir(ctx.home, ctx.spec.os)
        await sandbox.upload_dir(str(source), str(root), identity=Identity.AGENT)

    def _verify_timeout(self, ctx: EpisodeContext) -> float | None:
        if self.timeouts.verify == "unlimited":
            return None
        return self.timeouts.verify or ctx.spec.timeouts.verify

    async def _with_deadline(self, ctx, phase: Phase, seconds: float | None, coro):  # type: ignore[no-untyped-def]
        return await self._timed(ctx, phase, coro, timeout_sec=seconds)

    async def _timed(
        self,
        ctx: EpisodeContext,
        phase: Phase,
        coro,
        *,
        timeout_sec: float | None = None,
    ):  # type: ignore[no-untyped-def]
        """Record how long a phase took, whether or not it succeeded.

        A phase that timed out or crashed is the one you most want the duration of, so
        the span is written on the way out rather than on success.
        """
        assert ctx.execution is not None
        started_at = datetime.now(UTC)
        started = time.monotonic()
        ctx.current_phase = phase
        if ctx.phase_callback is not None:
            ctx.phase_callback(phase)
        ctx.execution.append(
            PhaseStarted(
                episode_id=ctx.episode_id,
                phase=phase.value,
                component=self.component,
            ),
            durable=True,
        )
        outcome = "succeeded"
        previous_timeout = ctx.phase_timeout_sec
        previous_deadline = ctx.phase_deadline
        ctx.phase_timeout_sec = timeout_sec
        ctx.phase_deadline = None if timeout_sec is None else started + timeout_sec
        try:
            with execution_logging(
                ctx.execution,
                episode_id=ctx.episode_id,
                phase=phase.value,
                component=self.component,
                secrets=ctx.secrets,
            ):
                if timeout_sec is None:
                    return await coro
                async with asyncio.timeout(timeout_sec):
                    return await coro
        except TimeoutError as exc:
            outcome = "timed_out"
            ctx.failure_phase = phase
            raise PhaseTimeoutError(phase.value, timeout_sec or 0) from exc
        except PhaseTimeoutError:
            outcome = "timed_out"
            ctx.failure_phase = phase
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "failed"
            ctx.failure_phase = phase
            raise
        finally:
            finished_at = datetime.now(UTC)
            elapsed = int((time.monotonic() - started) * 1000)
            timing = PhaseTiming(
                phase=phase.value,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=elapsed,
                outcome=outcome,  # type: ignore[arg-type]
            )
            ctx.phases.append(timing)
            ctx.execution.append(
                PhaseFinished(
                    episode_id=ctx.episode_id,
                    phase=phase.value,
                    component=self.component,
                    level="info" if outcome == "succeeded" else "error",
                    outcome=outcome,  # type: ignore[arg-type]
                    duration_ms=elapsed,
                ),
                durable=True,
            )
            ctx.current_phase = None
            ctx.phase_timeout_sec = previous_timeout
            ctx.phase_deadline = previous_deadline


@dataclass(frozen=True)
class SolverSnapshot:
    """A source episode's solver as the snapshot it was kept as: the handle a verifier boots
    from, the episode whose agent state it holds, and the image and allocation its solver
    had, as recorded."""

    handle: str
    episode_id: str
    resolved_image: ResolvedImage
    allocation: ResourceAllocation


class ReverificationProtocol(EpisodeProtocol):
    """Run current verification against a retained episode without rerunning its agent: in
    or beside its kept solver, or, for a snapshot verifier, in a new Sandbox booted from its
    solver's snapshot, which is left as it was."""

    def __init__(
        self,
        solver: Sandbox | SolverSnapshot,
        source_run_dir: Path,
        sandbox_identity: SandboxProvenance | None,
        *,
        timeouts: RunTimeouts | None = None,
    ) -> None:
        self.solver = solver
        self.source_run_dir = source_run_dir
        self.sandbox_identity = sandbox_identity
        self.harness = None  # type: ignore[assignment]
        self.max_steps = 0
        self.stall_limit = 0
        self.agent_enabled = False
        self.timeouts = timeouts or RunTimeouts()
        self.resume = None
        # It runs tests/ alone: what the source episode's agent received is its own.
        self.validation_rounds = 0

    async def run(self, task: Task, ctx: EpisodeContext) -> Verdict:
        if isinstance(self.solver, SolverSnapshot):
            return await self._from_snapshot(task, ctx, self.solver)
        await _require_agent_episode(self.solver, ctx.spec.os, self.solver.request.episode_id)
        ctx.resolved_image = self.solver.resolved_image
        ctx.resource_allocation = self.solver.allocation
        ctx.sandbox_identity = self.sandbox_identity
        ctx.home = self.solver.agent_home
        copy_trajectory(self.source_run_dir, ctx.run_dir)

        if ctx.spec.verify.environment_mode is VerificationMode.SHARED:
            try:
                await self._capture_solver_evidence(ctx, self.solver)
                rewards = await self._with_deadline(
                    ctx,
                    Phase.VERIFY,
                    self._verify_timeout(ctx),
                    self._verify(task, ctx, self.solver),
                )
            finally:
                if await self._sanitize_for_retention(ctx, self.solver):
                    await self.solver.retain(
                        roles=("solver", "verifier"),
                        reason="preserved after shared reverification",
                    )
                else:
                    await self.solver.destroy()
                    self.solver.release_resources()
            ctx.verified_rewards = rewards
            return Verdict.completed(await task.score(ctx), metrics=ctx.metrics)

        try:
            await self._capture_solver_evidence(ctx, self.solver)
        finally:
            await self.solver.retain(
                roles=("solver",),
                reason="preserved after artifact capture for reverification",
            )

        async def verify() -> dict[str, float]:
            verifier = await self._provision_verifier(ctx)
            try:
                await ctx.artifacts.restore(verifier)
                return await self._verify(task, ctx, verifier)
            finally:
                await self._sanitize_for_retention(ctx, verifier)
                await ctx.sandboxes.release(verifier)

        rewards = await self._with_deadline(
            ctx,
            Phase.VERIFY,
            self._verify_timeout(ctx),
            verify(),
        )
        ctx.verified_rewards = rewards
        return Verdict.completed(await task.score(ctx), metrics=ctx.metrics)

    async def _from_snapshot(
        self, task: Task, ctx: EpisodeContext, source: SolverSnapshot
    ) -> Verdict:
        ctx.resolved_image = source.resolved_image
        ctx.resource_allocation = source.allocation
        ctx.sandbox_identity = self.sandbox_identity
        copy_trajectory(self.source_run_dir, ctx.run_dir)

        async def verify() -> dict[str, float]:
            verifier = await self._provision_verifier(ctx, snapshot=source.handle)
            try:
                await _require_snapshot_episode(
                    verifier, ctx.spec.os, source.handle, source.episode_id
                )
                ctx.home = verifier.agent_home
                # The agent's output as the snapshot holds it, before tests/ arrive.
                await self._capture_solver_evidence(ctx, verifier)
                return await self._verify(task, ctx, verifier)
            finally:
                await self._sanitize_for_retention(ctx, verifier)
                await ctx.sandboxes.release(verifier)

        ctx.verified_rewards = await self._with_deadline(
            ctx, Phase.VERIFY, self._verify_timeout(ctx), verify()
        )
        return Verdict.completed(await task.score(ctx), metrics=ctx.metrics)


def _agent_episode_path(operating_system: OperatingSystem) -> str:
    """Names the episode whose agent last ran in a solver Sandbox."""
    return str(_layout(operating_system).path("agent-episode"))


async def _write_agent_episode(
    sandbox: Sandbox, operating_system: OperatingSystem, episode_id: str
) -> None:
    await sandbox.write_file(_agent_episode_path(operating_system), episode_id.encode())


async def _agent_episode(sandbox: Sandbox, operating_system: OperatingSystem) -> str | None:
    try:
        return (await sandbox.read_file(_agent_episode_path(operating_system))).decode()
    except Exception:
        return None


async def _require_agent_episode(
    sandbox: Sandbox, operating_system: OperatingSystem, episode_id: str
) -> None:
    """A retained solver serves only the episode whose agent state it still holds.

    Once a later episode continued it, the earlier episode's handle no longer describes it.
    A refused Sandbox is left retained as it was, only disconnected.
    """
    holder = await _agent_episode(sandbox, operating_system)
    if holder == episode_id:
        return
    await sandbox.retain(roles=("solver",), reason="refused: it holds another episode's state")
    raise ProviderCapabilityError(
        f"retained sandbox {sandbox.sandbox_id} holds "
        + (f"episode {holder}'s" if holder else "no recorded episode's")
        + f" agent state, not {episode_id}'s"
    )


async def _require_snapshot_episode(
    sandbox: Sandbox, operating_system: OperatingSystem, handle: str, episode_id: str
) -> None:
    """A Sandbox booted from a snapshot serves only the episode whose agent state the
    snapshot holds; the lease destroys a refused one."""
    holder = await _agent_episode(sandbox, operating_system)
    if holder != episode_id:
        raise ProviderCapabilityError(
            f"snapshot {handle} holds "
            + (f"episode {holder}'s" if holder else "no recorded episode's")
            + f" agent state, not {episode_id}'s"
        )


def validation_rounds(task: Task, harness: Harness, override: int | None) -> int:
    """How many validation rounds an agent of ``harness`` receives in an episode of ``task``:
    the Run's ``override``, else the Task's; none for the oracle and nop, which never loop.

    A count that cannot hold is refused, before anything is prepared.
    """
    if isinstance(harness, (OracleHarness, NopHarness)):
        return 0
    rounds = task.spec.validation_rounds if override is None else override
    if rounds:
        asked = (
            f"its [agent].validation_rounds = {rounds}"
            if override is None
            else f"the Run's validation_rounds = {rounds}"
        )
        _require_validation(task, harness, asked=asked)
    return rounds


def _require_validation(
    task: Task, harness: Harness | None, *, asked: str = "validation_rounds"
) -> None:
    """Refuse validation that cannot run: a Task without ``validation/``, or rounds for a
    ``harness`` that cannot resume its native session, which is how every turn after the
    first begins; ``asked`` names the setting that asked for them."""
    folder = task.folder
    if folder is None or folder.stage_dir(Stage.VALIDATION) is None:
        raise ConfigError(
            f"{task.spec.name} has no validation/, so its agent can receive none of the "
            f"validation rounds {asked} asks for; set validation_rounds = 0"
        )
    if harness is None:
        return
    if (
        not isinstance(harness, AutonomousHarness)
        or isinstance(harness, (OracleHarness, NopHarness))
        or harness.resume_support is not ResumeSupport.NATIVE
    ):
        raise AgentUnsupportedError(
            f"{harness.name} cannot resume its native session, which each turn after a "
            f"validation round does: {asked} needs a Harness with native resume, or set "
            "validation_rounds = 0"
        )


def _round_status(error: Exception) -> Status:
    """What a validation round's failure is, as an Episode's would be: a verifier that runs
    out of time, crashes or writes no result, or whose Sandbox the Task's code loses to its
    memory or storage, is the Task's; anything else the environment's."""
    if isinstance(error, PhaseTimeoutError):
        return Status.TIMEOUT
    if isinstance(error, (TaskError, ResourceExhaustedError)):
        return Status.TASK_ERROR
    return Status.ENV_ERROR


def _minutes(seconds: float) -> str:
    return f"{max(seconds, 0.0) / 60:.1f}".removesuffix(".0")


def _round_prompt(
    record: ValidationRound, rounds: int, feedback: str | None, seconds_left: float
) -> str:
    """The prompt that gives the agent one validation round's result: framework-owned and
    neutral, with the verifier's feedback verbatim."""
    result = (
        f"reward {record.reward:.2f}"
        if record.reward is not None
        else f"no score ({record.status.value})"
    )
    lines = [f"Validation round {record.index} of {rounds}: {result}.", ""]
    if feedback is None:
        lines.append("The validation set gave no feedback.")
    else:
        lines += ["Feedback from the validation set:", "<feedback>"]
        lines += [feedback.rstrip("\n"), "</feedback>"]
    left = rounds - record.index
    lines += [
        "",
        f"Validation rounds left: {left or 'none'}. Time left: {_minutes(seconds_left)} "
        "minutes. When you end this turn, "
        + (
            "your work is validated again."
            if left
            else "your final state is scored once, on the hidden test set."
        ),
    ]
    return "\n".join(lines)


def _with_validation_rounds(trajectory: AtifTrajectory, prompts: dict[int, str]) -> AtifTrajectory:
    """Mark the user step of each validation round's prompt with ``validation_round``, which
    is where the agent's next turn begins; a prompt the parsed session lacks is a parse
    issue."""
    if not prompts:
        return trajectory
    by_prompt = {prompt.strip(): index for index, prompt in prompts.items()}
    found: set[int] = set()
    steps = []
    for step in trajectory.steps:
        index = by_prompt.get(_text(step).strip()) if step.source == "user" else None
        if index is not None and index not in found:
            found.add(index)
            extra = dict(step.extra or {})
            extra["ale"] = {**(extra.get("ale") or {}), "validation_round": index}
            step = step.model_copy(update={"extra": extra})
        steps.append(step)
    trajectory = trajectory.model_copy(update={"steps": steps})
    for index in sorted(set(prompts) - found):
        issue = {
            "reason": "validation_round_not_found",
            "detail": f"no user message of the session is validation round {index}'s prompt",
        }
        trajectory = _with_parse_issue(trajectory, issue)
    return trajectory


def _text(step: AtifStep) -> str:
    if isinstance(step.message, str):
        return step.message
    return "".join(part.text or "" for part in step.message if part.type == "text")


def _continued_segment(trajectory: AtifTrajectory, resume: Resume) -> AtifTrajectory:
    """This episode's part of a native session an earlier episode began.

    The native session holds every turn so far, and the parser converts all of them. The
    earlier steps are the earlier episodes' and are dropped, so each episode's trajectory,
    like its transport trace and limits, covers what it ran itself.
    """
    source = resume.trajectory
    link: dict[str, object] = {
        "episode_id": resume.source.episode_id,
        "trajectory_id": source.trajectory_id if source is not None else None,
    }
    before: int | None = None
    if source is not None:
        # A first episode began the session; a continuation says where its own part
        # began, unless it could not tell.
        earlier = ((source.extra or {}).get("ale") or {}).get("resumed_from")
        prior = 0 if earlier is None else earlier.get("native_steps")
        if prior is not None and len(trajectory.steps) > prior + len(source.steps):
            end = prior + len(source.steps)
            if _same_step(trajectory.steps[end - 1], source.steps[-1]):
                before = end
    if before is None:
        issue = {
            "reason": "continuation_boundary_not_found",
            "detail": "the earlier episodes' steps are kept: their end was not found",
        }
        return _with_ale(_with_parse_issue(trajectory, issue), resumed_from=link)

    steps = [
        step.model_copy(update={"step_id": index})
        for index, step in enumerate(trajectory.steps[before:], start=1)
    ]
    referenced = {
        ref.trajectory_id
        for step in steps
        for result in (step.observation.results if step.observation else ())
        for ref in result.subagent_trajectory_ref or ()
    }
    subagents = [
        child
        for child in trajectory.subagent_trajectories or ()
        if child.trajectory_id in referenced
    ]
    segment = trajectory.model_copy(
        update={
            "steps": steps,
            "subagent_trajectories": subagents or None,
            "final_metrics": _segment_metrics(steps),
        }
    )
    return _with_ale(segment, resumed_from={**link, "native_steps": before})


def _same_step(parsed: AtifStep, recorded: AtifStep) -> bool:
    def calls(step: AtifStep) -> list[str]:
        return [call.tool_call_id for call in step.tool_calls or ()]

    return (parsed.source, parsed.message, calls(parsed)) == (
        recorded.source,
        recorded.message,
        calls(recorded),
    )


def _segment_metrics(steps: list[AtifStep]) -> AtifFinalMetrics:
    """Token totals only when every model-backed step reported its usage."""
    agent = [step for step in steps if step.source == "agent" and step.llm_call_count != 0]
    if not agent or any(step.metrics is None for step in agent):
        return AtifFinalMetrics(total_steps=len(steps))

    def total(name: str) -> int | None:
        values = [getattr(step.metrics, name) for step in agent]
        return None if any(value is None for value in values) else sum(values)

    return AtifFinalMetrics(
        total_prompt_tokens=total("prompt_tokens"),
        total_completion_tokens=total("completion_tokens"),
        total_cached_tokens=total("cached_tokens"),
        total_steps=len(steps),
    )


def _with_ale(trajectory: AtifTrajectory, **fields: object) -> AtifTrajectory:
    extra = dict(trajectory.extra or {})
    extra["ale"] = {**(extra.get("ale") or {}), **fields}
    return trajectory.model_copy(update={"extra": extra})


@contextlib.contextmanager
def _limit_terminated(ctx: EpisodeContext) -> Iterator[None]:
    """Attribute an agent failure to the Gateway limit that caused it.

    Agent programs report a refused call as their own error: an API error, a provider
    limit, a failed turn. Whatever they say, an agent phase that fails after the Gateway
    refused a call was stopped by that limit, which only the Gateway knows.
    """
    try:
        yield
    except (AgentError, EnvironmentError_) as error:
        refusal = ctx.limit_refusal() if ctx.limit_refusal is not None else None
        if refusal is None:
            raise
        raise BudgetExceededError(
            refusal.name, float(refusal.configured_value), refusal.observed_value
        ) from error


def _with_parse_issue(trajectory: AtifTrajectory, issue: dict[str, object]) -> AtifTrajectory:
    extra = dict(trajectory.extra or {})
    ale = dict(extra.get("ale") or {})
    ale["parse_issues"] = [*ale.get("parse_issues", ()), issue]
    extra["ale"] = ale
    return trajectory.model_copy(update={"extra": extra})


def _mark_incomplete(trajectory: AtifTrajectory, reason: str) -> AtifTrajectory:
    extra = dict(trajectory.extra or {})
    ale = dict(extra.get("ale") or {})
    ale.update(incomplete=True, incomplete_reason=reason)
    extra["ale"] = ale
    return trajectory.model_copy(update={"extra": extra})


def _exact_token_metrics(run_dir: Path, call_id: str) -> AtifMetrics | None:
    path = run_dir / "logs" / "gateway" / "tokens" / f"{call_id}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise TypeError("token evidence must be an object")
        known = {
            name: payload.get(name)
            for name in (
                "prompt_token_ids",
                "completion_token_ids",
                "logprobs",
            )
            if name in payload
        }
        extra = {
            name: value
            for name, value in payload.items()
            if name
            not in {
                "prompt_token_ids",
                "completion_token_ids",
                "logprobs",
            }
        }
        metrics = AtifMetrics(
            **known,
            extra={"ale": extra} if extra else None,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise TrajectoryConversionError(
            f"invalid exact token evidence for {call_id}: {exc}"
        ) from exc
    path.unlink()
    for directory in (path.parent, path.parent.parent):
        with contextlib.suppress(OSError):
            directory.rmdir()
    return metrics


def _merge_metrics(
    current: AtifMetrics | None,
    exact: AtifMetrics,
) -> AtifMetrics:
    if current is None:
        return exact
    values = current.model_dump(exclude_none=True)
    exact_values = exact.model_dump(exclude_none=True)
    if "extra" in exact_values:
        values["extra"] = {
            **(values.get("extra") or {}),
            **exact_values.pop("extra"),
        }
    values.update(exact_values)
    return AtifMetrics.model_validate(values)


class _RecordedSandbox:
    """Record framework-issued harness commands while delegating every other operation."""

    def __init__(
        self,
        ctx: EpisodeContext,
        sandbox: Sandbox,
        *,
        component: str,
        actor: Literal["framework", "task"] = "framework",
        env: dict[str, str] | None = None,
    ) -> None:
        self._ctx = ctx
        self._sandbox = sandbox
        self._component = component
        self._actor = actor
        self._env = env or {}
        """The Task's variables, beneath each command's own."""

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._sandbox, name)

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        identity: Identity = Identity.FRAMEWORK,
        output_sink: ExecOutputSink | None = None,
    ) -> ExecResult:
        assert self._ctx.execution is not None
        assert self._ctx.blobs is not None
        self._ctx.harness_execution_counter += 1
        counter = self._ctx.harness_execution_counter
        recorder = CommandRecorder(
            execution=self._ctx.execution,
            blobs=cast(BlobStore, self._ctx.blobs),
            episode_id=self._ctx.episode_id,
            phase=Phase.AGENT.value,
            component=self._component,
            execution_id=f"agent-harness-{counter}",
            actor=self._actor,
            argv=[str(part) for part in argv],
            cwd=cwd,
            secrets=self._ctx.secrets,
        )

        async def emit(stream: Literal["stdout", "stderr"], data: bytes) -> None:
            await recorder.write(stream, data)
            if output_sink is not None:
                await output_sink(stream, data)

        started = time.monotonic()
        try:
            result = await self._sandbox.exec(
                argv,
                cwd=cwd,
                env={**self._env, **(env or {})} or None,
                timeout_sec=timeout_sec,
                identity=identity,
                output_sink=emit,
            )
        except asyncio.CancelledError:
            recorder.finish(
                ExecResult(
                    exit_code=None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                ),
                outcome="cancelled",
            )
            raise
        except BaseException:
            recorder.finish(
                ExecResult(
                    exit_code=None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                ),
                outcome="failed",
            )
            raise
        recorder.finish(result)
        if result.timed_out and self._component == "harness-launch":
            raise PhaseTimeoutError(Phase.AGENT.value, timeout_sec or 0)
        return result
