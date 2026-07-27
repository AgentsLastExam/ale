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
from ale.core.lock import AssetProvenance, KitProvenance, SandboxProvenance
from ale.core.sandbox import Identity, Sandbox, SandboxRequest
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
from ale.run.envs import DEFAULT_MAX_STEPS, DEFAULT_STALL_LIMIT, SandboxEnv
from ale.run.harnesses.builtin import ORACLE_DIR
from ale.run.images import resolve_digest, resolve_ref
from ale.run.kits import hash_kit

__all__ = ["StandardEnvironment"]

SETUP_DIR = PurePosixPath("/ale/setup")
VERIFY_DIR = PurePosixPath("/ale/verify")
VERDICT_PATH = PurePosixPath("/ale/verify/rewards.json")


class StandardEnvironment(Environment):
    """Linear provision → setup → agent → verify flow."""

    name = "core/standard"

    def __init__(
        self,
        harness: AutonomousHarness | PolicyHarness,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        stall_limit: int = DEFAULT_STALL_LIMIT,
    ) -> None:
        self.harness = harness
        self.max_steps = max_steps
        self.stall_limit = stall_limit

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
        reference = resolve_ref(spec.image)
        request = SandboxRequest(
            episode_id=ctx.episode_id,
            image_ref=reference,
            resources=spec.resources,
            network=spec.network,
            gateway_url=ctx.session.gateway_url or None,
            proxy_url=ctx.proxy_url,
            env={"ALE_EPISODE_ID": ctx.episode_id},
            sudo=spec.resources.sudo,
        )
        sandbox = await ctx.sandboxes.acquire(request)
        # After acquisition: the image is present locally by now, whether it was already
        # there or had to be pulled.
        with contextlib.suppress(Exception):
            ctx.image_digest = await resolve_digest(reference)
        ctx.sandbox_identity = SandboxProvenance(
            user=_agent_user(sandbox), sudo=spec.resources.sudo
        )
        return sandbox

    async def _setup(self, task: Task, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        """Stage what the task declared, then run its own preparation.

        Only agent-visible material is uploaded: the manifest, the verify stage and the
        oracle stay on the host until they are needed, which is what keeps answers out
        of reach rather than merely out of sight.

        Directories come from the task — its asset destinations and the paths it wants
        collected — so a domain that needs a different layout simply declares one.
        """
        # The task's own preparation is trusted code and is not the thing being measured,
        # so it may reach the network. What the declared policy binds is the agent.
        await sandbox.open_egress()

        declared = [
            ctx.work_dir,
            *(mount.dest for mount in ctx.spec.setup.assets),
            *ctx.spec.artifacts,
        ]
        await sandbox.exec(["mkdir", "-p", *declared])
        # Created by the framework, used by the agent — so they are handed over at once.
        # What a task's own setup then produces is the task's to open up or not; the
        # framework does not revisit ownership afterwards.
        await sandbox.exec(["chown", _agent_user(sandbox), *declared])

        folder = getattr(task, "folder", None)
        if folder is None:
            return

        if files := folder.visible_files():
            # A task's own small files land beside its first declared asset, or in the
            # first path it asked to have collected — whichever it declared.
            destination = _default_files_dest(ctx)
            await sandbox.exec(["mkdir", "-p", destination])
            await sandbox.upload_dir(str(files), destination, identity=Identity.AGENT)

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
        if isinstance(harness, PolicyHarness):
            await self._rollout(harness, ctx, sandbox)
            return

        if harness.name == "oracle":
            await self._upload_oracle(task, sandbox)

        # Installed first, then sealed. Putting the agent's own CLI in place is our
        # preparation, not its work; an image that has not pre-baked one would otherwise
        # be unusable, since the only thing a sealed sandbox can reach is the gateway.
        await harness.install(sandbox)
        ctx.agent_version = harness.version()
        await self._seal(ctx, sandbox)

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

    async def _rollout(self, harness, ctx: EpisodeContext, sandbox: Sandbox) -> None:  # type: ignore[no-untyped-def]
        """Hand the agent a stepwise view of the sandbox and let it drive.

        The loop used to live here, which meant an agent that wanted to drive had to be
        inverted through queues to fit. Now it drives, and the environment is still the
        only thing touching the sandbox — so every observation and action is witnessed,
        and the ceilings hold for an agent that never heard of them.
        """
        # The session is used as-is, unlike the autonomous path. A stepwise harness runs
        # in this process and drives the sandbox from outside, so it reaches the gateway
        # at the host's own address; rewriting it to the sandbox's view hands a host-side
        # agent a hostname that only resolves inside a container.
        await harness.install(sandbox)
        ctx.agent_version = harness.version()
        await self._seal(ctx, sandbox)

        async with SandboxEnv(
            sandbox,
            instruction=ctx.spec.instruction,
            trace=ctx.trace,
            max_steps=self.max_steps,
            stall_limit=self.stall_limit,
        ) as env:
            closing = await harness.rollout(env, ctx.session)

        ctx.trace.write_semantic(
            NoteRecord(
                seq=ctx.trace.next_seq(TraceLayer.SEMANTIC),
                message="agent finished",
                data={"harness": harness.name, "steps": env.step_index, "closing": closing or ""},
            )
        )

    async def _seal(self, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        """Apply the declared policy, and record that it was applied.

        A failure here ends the episode. An agent that ran with a network it was never
        meant to have is not a result with a caveat — it is a different experiment, and
        the lock file would describe the one that was intended rather than the one that
        happened.
        """
        await sandbox.close_egress()
        ctx.trace.write_semantic(
            NoteRecord(
                seq=ctx.trace.next_seq(TraceLayer.SEMANTIC),
                message="network sealed for the agent",
                data={"mode": ctx.spec.network.mode.value},
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
        # Scoring is the framework's own work, and a verifier may need to reach something
        # the agent could not. The agent has already finished; nothing it does can follow.
        await sandbox.open_egress()

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
        }
        await sandbox.write_file(
            directory / "params.json", json.dumps(ctx.spec.params).encode("utf-8")
        )
        result = await sandbox.exec(
            ["bash", str(entry)],
            cwd=str(directory),
            env=env,
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

    async def _stage_assets(
        self, ctx: EpisodeContext, sandbox: Sandbox, mounts: tuple[AssetMount, ...]
    ) -> None:
        """Copy this stage's declared data into the sandbox.

        Keys and origins are kept for provenance: a run served from cache has to be as
        explainable as one that downloaded everything.
        """
        if not mounts:
            return
        for asset in await stage_mounts(sandbox, mounts):
            ctx.assets.append(
                AssetProvenance(
                    component=asset.mount.path,
                    repo=asset.mount.repo,
                    revision=asset.mount.revision,
                    data_key=asset.key,
                    origin=asset.origin,
                )
            )

    async def _install_kits(
        self, ctx: EpisodeContext, sandbox: Sandbox, folder: object, kits: tuple[str, ...]
    ) -> None:
        """Put a domain's shared libraries where the interpreter already searches.

        Not a framework directory plus a search path we set: that is a rule every task
        author has to learn, and ours was quietly wrong — it set a literal glob, which
        the variable does not expand, so one of its two implementations never worked.

        The destination is asked of the interpreter rather than assumed, since it depends
        on the image's Python version. It is the *system* location, not one account's:
        a kit is shared machinery that setup, verify and the agent all import, and those
        run as different users — installing into any one of their private locations would
        make it importable for that one and missing for the rest.
        """
        if not kits:
            return

        repo_root: Path = folder.repo_root  # type: ignore[attr-defined]
        destination = await self._site_packages(sandbox)

        for name in kits:
            source = repo_root / "kits" / name
            if not source.is_dir():
                raise TaskError(f"kit {name!r} is declared but not present at {source}")
            await sandbox.exec(["mkdir", "-p", destination])
            await sandbox.upload_dir(str(source), destination)
            ctx.kits.append(KitProvenance(name=name, content_hash=hash_kit(source)))

    async def _site_packages(self, sandbox: Sandbox) -> str:
        """Where this image's interpreter looks for installed packages."""
        result = await sandbox.exec(
            ["python3", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        )
        path = result.stdout.strip()
        if result.exit_code != 0 or not path:
            raise TaskError(
                "could not ask the image's interpreter where user packages go; "
                f"kits cannot be installed: {result.stderr.strip()}"
            )
        return path

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


def _agent_user(sandbox: Sandbox) -> str:
    """The account this sandbox calls the agent, for a plain `chown`."""
    return getattr(sandbox, "agent_user", "user")


def _default_files_dest(ctx: EpisodeContext) -> str:
    """Where a task's own ``files/`` directory goes when it declared no home for it."""
    if ctx.spec.setup.assets:
        return ctx.spec.setup.assets[0].dest
    return ctx.work_dir
