"""The standard episode flow.

Provision a sandbox, prepare it, let the agent work, score what it left behind. Most
tasks need exactly this; a domain that needs something else — two isolated containers, a
staged protocol, a human gate — subclasses :class:`~ale.core.environment.Environment`
instead of bending this one.

Three properties are enforced here rather than trusted to task authors:

* the workspace is created identically for every task, so no instruction depends on a
  task's name or folder;
* verification materials and the oracle never exist in the sandbox while the agent runs;
* every phase has a deadline, and teardown happens on every path out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import time
from pathlib import Path, PurePosixPath

from ale.core.environment import Environment, EpisodeContext, Phase
from ale.core.errors import PhaseTimeoutError, TaskError, VerifierOutputError
from ale.core.harness import AutonomousHarness, PolicyHarness
from ale.core.kit import KITS_ROOT
from ale.core.sandbox import Sandbox, SandboxRequest
from ale.core.task import Task
from ale.core.taskspec import AssetMount
from ale.core.trace import (
    ExecRecord,
    InstructionRecord,
    NoteRecord,
    PhaseSpan,
    TraceLayer,
    VerifierRecord,
)
from ale.core.verdict import Verdict
from ale.run.assets import stage_mounts
from ale.run.harnesses.builtin import ORACLE_DIR
from ale.run.images import resolve_ref

__all__ = ["StandardEnvironment"]

SETUP_DIR = PurePosixPath("/ale/setup")
VERIFY_DIR = PurePosixPath("/ale/verify")
VERDICT_PATH = PurePosixPath("/ale/verify/rewards.json")


class StandardEnvironment(Environment):
    """Linear provision → setup → agent → verify flow."""

    name = "core/standard"

    def __init__(self, harness: AutonomousHarness | PolicyHarness) -> None:
        self.harness = harness

    async def run(self, task: Task, ctx: EpisodeContext) -> Verdict:
        spec = task.spec
        sandbox = await self._timed(ctx, Phase.PROVISION, self._provision(ctx))
        try:
            await self._with_deadline(
                ctx, Phase.SETUP, spec.timeouts.setup, self._setup(task, ctx, sandbox)
            )
            await self._with_deadline(
                ctx, Phase.AGENT, spec.timeouts.agent, self._agent(task, ctx, sandbox)
            )
            rewards = await self._with_deadline(
                ctx, Phase.VERIFY, spec.timeouts.verify, self._verify(task, ctx, sandbox)
            )
        finally:
            # Teardown runs on every path, including cancellation.
            await asyncio.shield(self._teardown(ctx, sandbox))

        ctx.extras["rewards"] = rewards
        return Verdict.completed(await task.score(ctx))

    # --- phases ---

    async def _provision(self, ctx: EpisodeContext) -> Sandbox:
        spec = ctx.spec
        request = SandboxRequest(
            episode_id=ctx.episode_id,
            image_ref=resolve_ref(spec.image),
            resources=spec.resources,
            network=spec.network,
            gateway_url=ctx.session.gateway_url or None,
            env={"ALE_EPISODE_ID": ctx.episode_id},
            needs_gui=False,
        )
        return await ctx.sandboxes.acquire(request)

    async def _setup(self, task: Task, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        """Stage what the task declared, then run its own preparation.

        Only agent-visible material is uploaded: the manifest, the verify stage and the
        oracle stay on the host until they are needed, which is what keeps answers out
        of reach rather than merely out of sight.

        Directories come from the task — its asset destinations and the paths it wants
        collected — so a domain that needs a different layout simply declares one.
        """
        declared = [
            ctx.work_dir,
            *(mount.dest for mount in ctx.spec.setup.assets),
            *ctx.spec.artifacts,
        ]
        await sandbox.exec(["mkdir", "-p", *declared])

        folder = getattr(task, "folder", None)
        if folder is None:
            return

        if files := folder.visible_files():
            # A task's own small files land beside its first declared asset, or in the
            # first path it asked to have collected — whichever it declared.
            destination = _default_files_dest(ctx)
            await sandbox.exec(["mkdir", "-p", destination])
            await sandbox.upload_dir(str(files), destination)

        await self._stage_assets(ctx, sandbox, ctx.spec.setup.assets)
        await self._install_kits(ctx, sandbox, folder, ctx.spec.setup.kits)

        if setup_dir := folder.stage_dir("setup"):
            await sandbox.upload_dir(str(setup_dir), str(SETUP_DIR))
            if folder.stage_entry("setup"):
                await self._run_stage(ctx, sandbox, SETUP_DIR, Phase.SETUP)

    async def _agent(self, task: Task, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        spec = ctx.spec
        ctx.trace.write_semantic(
            InstructionRecord(
                seq=ctx.trace.next_seq(TraceLayer.SEMANTIC),
                text_digest=_digest(spec.instruction),
                chars=len(spec.instruction),
            )
        )

        harness = self.harness
        if not isinstance(harness, AutonomousHarness):
            raise NotImplementedError("the policy loop is not implemented yet")

        if harness.name == "oracle":
            await self._upload_oracle(task, sandbox)

        await harness.install(sandbox)
        # The session the agent gets carries the sandbox's own view of the gateway.
        session = ctx.session.model_copy(
            update={"gateway_url": sandbox.gateway_url or ctx.session.gateway_url}
        )
        run = await harness.launch(
            spec.instruction, sandbox, session, timeout_sec=spec.timeouts.agent
        )
        ctx.trace.write_semantic(
            NoteRecord(
                seq=ctx.trace.next_seq(TraceLayer.SEMANTIC),
                message="agent finished",
                data={"exit_code": run.exit_code, "harness": harness.name},
            )
        )

    async def _verify(self, task: Task, ctx: EpisodeContext, sandbox: Sandbox) -> dict[str, float]:
        """Score the episode using the task's own verify stage.

        A crash or malformed output here is a task defect, reported as ``task_error``:
        deliberately distinguishable from an agent that simply scored zero.
        """
        folder = getattr(task, "folder", None)
        verify_dir = folder.stage_dir("verify") if folder else None
        if verify_dir is None:
            raise TaskError("task has no verify stage")

        await sandbox.upload_dir(str(verify_dir), str(VERIFY_DIR))
        await self._stage_assets(ctx, sandbox, ctx.spec.verify.assets)
        await self._install_kits(ctx, sandbox, folder, ctx.spec.verify.kits)

        result = await self._run_stage(ctx, sandbox, VERIFY_DIR, Phase.VERIFY)
        rewards = await self._read_rewards(sandbox)

        ctx.trace.write_semantic(
            VerifierRecord(
                seq=ctx.trace.next_seq(TraceLayer.SEMANTIC),
                entry=str(VERIFY_DIR / "run.sh"),
                exit_code=result,
                rewards=rewards,
            )
        )
        return rewards

    async def _teardown(self, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        # Collection is best effort: a sandbox that died still has to be released.
        for index, path in enumerate(ctx.spec.artifacts):
            name = Path(path).name or f"artifact-{index}"
            with contextlib.suppress(Exception):
                await ctx.artifacts.collect(sandbox, path, name)
        await ctx.sandboxes.release(sandbox)

    # --- helpers ---

    async def _run_stage(
        self, ctx: EpisodeContext, sandbox: Sandbox, directory: PurePosixPath, phase: Phase
    ) -> int:
        entry = directory / "run.sh"
        env = {
            "ALE_STAGE_DIR": str(directory),
            "ALE_WORK_DIR": ctx.work_dir,
            "ALE_PARAMS_JSON": str(directory / "params.json"),
            "ALE_VERDICT_PATH": str(VERDICT_PATH),
            "PYTHONPATH": str(KITS_ROOT / "*"),
        }
        await sandbox.write_file(
            directory / "params.json", json.dumps(ctx.spec.params).encode("utf-8")
        )
        result = await sandbox.exec(
            ["bash", str(entry)],
            cwd=str(directory),
            env=env | {"PYTHONPATH": await self._pythonpath(sandbox)},
            timeout_sec=None,
        )
        ctx.trace.write_semantic(
            ExecRecord(
                seq=ctx.trace.next_seq(TraceLayer.SEMANTIC),
                argv_digest=_digest(shlex.join(["bash", str(entry)])),
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
            )
        )
        if result.exit_code != 0 and phase is Phase.SETUP:
            raise TaskError(
                f"setup failed with exit code {result.exit_code}: {result.stderr[-500:]}"
            )
        return result.exit_code

    async def _pythonpath(self, sandbox: Sandbox) -> str:
        """Every installed kit, so a verify script can simply import what it needs."""
        listing = await sandbox.exec(["sh", "-c", f"ls -d {KITS_ROOT}/* 2>/dev/null || true"])
        paths = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
        return ":".join(paths)

    async def _stage_assets(
        self, ctx: EpisodeContext, sandbox: Sandbox, mounts: tuple[AssetMount, ...]
    ) -> None:
        """Copy this stage's declared data into the sandbox.

        Keys and origins are kept for provenance: a run served from cache has to be as
        explainable as one that downloaded everything.
        """
        if not mounts:
            return
        staged = await stage_mounts(sandbox, mounts)
        ctx.extras.setdefault("assets", []).extend(  # type: ignore[union-attr]
            {"path": a.mount.path, "key": a.key, "origin": a.origin.value} for a in staged
        )

    async def _install_kits(
        self, ctx: EpisodeContext, sandbox: Sandbox, folder: object, kits: tuple[str, ...]
    ) -> None:
        if not kits:
            return
        repo_root: Path = folder.repo_root  # type: ignore[attr-defined]
        for name in kits:
            source = repo_root / "kits" / name
            if not source.is_dir():
                raise TaskError(f"kit {name!r} is declared but not present at {source}")
            await sandbox.exec(["mkdir", "-p", str(KITS_ROOT / name)])
            await sandbox.upload_dir(str(source), str(KITS_ROOT / name))

    async def _upload_oracle(self, task: Task, sandbox: Sandbox) -> None:
        folder = getattr(task, "folder", None)
        oracle_dir = folder.stage_dir("oracle") if folder else None
        if oracle_dir is None:
            raise TaskError("validation requires an oracle stage, and this task has none")
        await sandbox.upload_dir(str(oracle_dir), str(ORACLE_DIR))
        await sandbox.write_file(
            ORACLE_DIR / "params.json", json.dumps(task.spec.params).encode("utf-8")
        )

    async def _read_rewards(self, sandbox: Sandbox) -> dict[str, float]:
        try:
            raw = await sandbox.read_file(VERDICT_PATH)
        except Exception as exc:
            raise VerifierOutputError(f"verify wrote no rewards to {VERDICT_PATH}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
            rewards = payload["rewards"]
            return {str(key): float(value) for key, value in rewards.items()}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise VerifierOutputError(f"verify wrote malformed rewards: {exc}") from exc

    async def _with_deadline(self, ctx, phase: Phase, seconds: float, coro):  # type: ignore[no-untyped-def]
        try:
            return await self._timed(ctx, phase, asyncio.wait_for(coro, timeout=seconds))
        except TimeoutError as exc:
            raise PhaseTimeoutError(phase.value, seconds) from exc

    async def _timed(self, ctx: EpisodeContext, phase: Phase, coro):  # type: ignore[no-untyped-def]
        """Record how long a phase took, whether or not it succeeded.

        A phase that timed out or crashed is the one you most want the duration of, so
        the span is written on the way out rather than on success.
        """
        started = time.monotonic()
        try:
            return await coro
        finally:
            elapsed = int((time.monotonic() - started) * 1000)
            ctx.phases.append(PhaseSpan(name=phase.value, duration_ms=elapsed))


def _digest(text: str) -> str:
    from hashlib import sha256

    return f"sha256:{sha256(text.encode('utf-8')).hexdigest()}"


def _default_files_dest(ctx: EpisodeContext) -> str:
    """Where a task's own ``files/`` directory goes when it declared no home for it."""
    if ctx.spec.setup.assets:
        return ctx.spec.setup.assets[0].dest
    return ctx.work_dir
