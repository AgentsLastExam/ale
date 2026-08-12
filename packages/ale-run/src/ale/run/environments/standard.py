"""The standard episode flow.

Provision a sandbox, prepare it, let the agent work, score what it left behind. Most
tasks need exactly this; a domain that needs something else — two isolated containers, a
staged protocol, a human gate — subclasses :class:`~ale.core.environment.Environment`
instead of bending this one.

Three properties are enforced here rather than trusted to task authors: verification and
oracle material is absent while the evaluated agent runs, every phase has a deadline,
and teardown happens on every path out. Tasks own literal paths inside the image-declared
agent home; ALE owns only staging time and its root-only framework directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import shutil
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from ale.core.environment import Environment, EpisodeContext, Phase
from ale.core.errors import (
    AgentError,
    PhaseTimeoutError,
    TaskError,
    TrajectoryConversionError,
    VerificationInfrastructureError,
    VerifierOutputError,
)
from ale.core.harness import (
    AutonomousHarness,
    EffectiveAgentResources,
    PolicyHarness,
    TrajectoryParseContext,
)
from ale.core.lock import AleVerifyProvenance, SandboxProvenance
from ale.core.result import PhaseTiming
from ale.core.sandbox import (
    ExecOutputSink,
    ExecResult,
    Identity,
    Sandbox,
    SandboxRequest,
    SandboxRole,
)
from ale.core.task import Task
from ale.core.taskspec import NetworkMode, NetworkPolicy, StdioMcpServer, VerificationMode
from ale.core.trace import (
    PhaseFinished,
    PhaseStarted,
    PolicyApplied,
    TrajectoryLink,
    read_jsonl,
)
from ale.core.trajectory import AtifAgent, AtifMetrics, AtifTrajectory, TrajectoryBuilder
from ale.core.verdict import Verdict
from ale.run.envs import DEFAULT_MAX_STEPS, DEFAULT_STALL_LIMIT, SandboxEnv
from ale.run.harnesses.builtin import oracle_dir
from ale.run.recording import (
    BlobStore,
    CommandRecorder,
    Redactor,
    atomic_write_json,
    execution_logging,
)
from ale.run.subscription import ProfileLease
from ale.run.verification import installed_ale_verify
from ale_verify import VerificationRecord

__all__ = ["StandardEnvironment"]

#: Where the framework's own machinery goes. Under a root-owned, root-only directory,
#: which is the whole point: the verify stage holds the scorer and the oracle holds the
#: answer, and until now they were kept from the agent by *timing* alone — uploaded only
#: when needed. Permissions are a better guarantee than a schedule.
#:
#: It also leaves exactly two roots in a sandbox: this one, which belongs to the framework
#: and which the agent cannot read, and the agent's home, which is entirely its own.
SETUP_DIR = PurePosixPath("/opt/ale/setup")
VERIFY_DIR = PurePosixPath("/opt/ale/verify")
VERDICT_PATH = PurePosixPath("/opt/ale/verify/rewards.json")
VERIFICATION_PATH = PurePosixPath("/opt/ale/verify/verification.json")
TRAJECTORY_STAGE_PATH = PurePosixPath("/opt/ale/verify/trajectory.json")
VERIFY_CONFIG_PATH = PurePosixPath("/opt/ale/verify/config.json")
TASK_INSTRUCTION_PATH = PurePosixPath("/opt/ale/verify/instruction.md")
TASK_PARAMETERS_PATH = PurePosixPath("/opt/ale/verify/parameters.json")
AGENT_JUDGE_LOG_PATH = PurePosixPath("/opt/ale/verify/agent-judge.jsonl")


class StandardEnvironment(Environment):
    """Linear provision → setup → agent → verify flow."""

    name = "core/standard"

    def __init__(
        self,
        harness: AutonomousHarness | PolicyHarness,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        stall_limit: int = DEFAULT_STALL_LIMIT,
        agent_enabled: bool = True,
    ) -> None:
        self.harness = harness
        self.max_steps = max_steps
        self.stall_limit = stall_limit
        self.agent_enabled = agent_enabled

    async def run(self, task: Task, ctx: EpisodeContext) -> Verdict:
        spec = task.spec
        if self.agent_enabled:
            self.harness.validate_resources(ctx.agent_resources)
        sandbox = await self._timed(ctx, Phase.PROVISION, self._provision(ctx))
        try:
            await self._with_deadline(
                ctx, Phase.SETUP, spec.timeouts.setup, self._setup(task, ctx, sandbox)
            )
            if self.agent_enabled:
                await self._with_deadline(
                    ctx, Phase.AGENT, spec.timeouts.agent, self._agent(task, ctx, sandbox)
                )
            try:
                await self._capture_solver_evidence(ctx, sandbox)
            except BaseException:
                ctx.extras["failure_phase"] = Phase.AGENT.value
                raise

            async def verify() -> dict[str, float]:
                if spec.verify.environment_mode is VerificationMode.SHARED:
                    return await self._verify(task, ctx, sandbox)
                await self._sanitize_for_retention(ctx, sandbox)
                await ctx.sandboxes.release(sandbox)
                verifier = await self._provision_verifier(ctx)
                try:
                    await ctx.artifacts.restore(verifier)
                    return await self._verify(task, ctx, verifier)
                finally:
                    await self._sanitize_for_retention(ctx, verifier)
                    await ctx.sandboxes.release(verifier)

            rewards = await self._with_deadline(ctx, Phase.VERIFY, spec.timeouts.verify, verify())
        finally:
            # Teardown runs on every path, including cancellation.
            await asyncio.shield(self._timed(ctx, Phase.TEARDOWN, self._teardown(ctx, sandbox)))

        ctx.extras["rewards"] = rewards
        metrics = ctx.extras.get("metrics")
        return Verdict.completed(
            await task.score(ctx),
            metrics=metrics if isinstance(metrics, dict) else None,
        )

    # --- phases ---

    async def _provision(self, ctx: EpisodeContext) -> Sandbox:
        spec = ctx.spec
        network = spec.network
        if ctx.proxy_url and ctx.allowed_hosts and spec.network.mode is not NetworkMode.OPEN:
            network = NetworkPolicy(
                mode=NetworkMode.ALLOWLIST,
                allowed_hosts=tuple(sorted(ctx.allowed_hosts)),
            )
        request = SandboxRequest(
            episode_id=ctx.episode_id,
            role=(
                SandboxRole.SHARED
                if spec.verify.environment_mode is VerificationMode.SHARED
                else SandboxRole.SOLVER
            ),
            prepared_image=ctx.prepared_image,
            resources=spec.resources,
            network=network,
            gateway_url=ctx.session.gateway_url or None,
            proxy_url=ctx.proxy_url,
            proxy_token=ctx.session.token,
            env={"ALE_EPISODE_ID": ctx.episode_id},
            sudo=spec.resources.sudo,
        )
        sandbox = await ctx.sandboxes.acquire(request)
        ctx.resolved_image = sandbox.resolved_image
        ctx.resource_allocation = sandbox.allocation
        # Derived from the account the image declared, not configured anywhere. A run
        # that could choose its own working directory was a second answer to a question
        # the image had already answered, and two answers can disagree.
        ctx.home = f"/home/{_agent_user(sandbox)}"
        ctx.sandbox_identity = SandboxProvenance(
            user=_agent_user(sandbox), sudo=spec.resources.sudo
        )
        return sandbox

    async def _provision_verifier(self, ctx: EpisodeContext) -> Sandbox:
        resources = ctx.spec.verify.resources
        if resources is None or ctx.prepared_verifier_image is None:
            raise TaskError("separate verification image/resources were not prepared")
        request = SandboxRequest(
            episode_id=ctx.episode_id,
            role=SandboxRole.VERIFIER,
            prepared_image=ctx.prepared_verifier_image,
            resources=resources.as_resources(),
            network=NetworkPolicy(mode=NetworkMode.OPEN),
            env={"ALE_EPISODE_ID": ctx.episode_id},
            sudo=False,
        )
        sandbox = await ctx.sandboxes.acquire(request)
        ctx.extras["verifier_resolved_image"] = sandbox.resolved_image
        ctx.extras["verifier_resource_allocation"] = sandbox.allocation
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

        folder = getattr(task, "folder", None)
        if folder is None:
            return

        if setup_dir := folder.stage_dir("setup"):
            await sandbox.upload_dir(str(setup_dir), str(SETUP_DIR))
            if folder.stage_entry("setup"):
                await self._run_stage(ctx, sandbox, SETUP_DIR, Phase.SETUP)

    async def _agent(self, task: Task, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        spec = ctx.spec
        ctx.extras["agent_started"] = True

        harness = self.harness
        if isinstance(harness, PolicyHarness):
            await self._rollout(harness, ctx, sandbox)
            return

        if harness.name == "oracle":
            await self._upload_oracle(ctx, task, sandbox)

        # Installed first, then sealed. Putting the agent's own CLI in place is our
        # preparation, not its work; an image that has not pre-baked one would otherwise
        # be unusable, since the only thing a sealed sandbox can reach is the gateway.
        # The session the agent gets carries the sandbox's own view of the gateway.
        session = ctx.session.model_copy(
            update={
                "gateway_url": sandbox.gateway_url or ctx.session.gateway_url,
                "home": ctx.home,
                "sandbox_id": sandbox.sandbox_id,
                "resources_digest": ctx.agent_resources.digest,
            }
        )
        ctx.agent_version = await harness.install(
            _RecordedSandbox(ctx, sandbox, component="harness-install")
        )
        subscription = ctx.extras.get("subscription_lease")
        if isinstance(subscription, ProfileLease):
            await subscription.stage(
                _RecordedSandbox(ctx, sandbox, component="subscription-stage"),
                ctx.home,
            )
        ctx.agent_resources = await self._stage_mcp_files(ctx, sandbox)
        await harness.install_resources(
            _RecordedSandbox(ctx, sandbox, component="harness-resources"),
            session,
            ctx.agent_resources,
        )
        await self._validate_stdio_mcp(ctx, sandbox)
        await self._seal(ctx, sandbox)
        run = await harness.launch(
            spec.instruction,
            _RecordedSandbox(
                ctx,
                sandbox,
                component="harness-launch",
                actor="task" if harness.name == "oracle" else "framework",
            ),
            session,
            timeout_sec=spec.timeouts.agent,
        )
        ctx.extras["agent_run"] = run
        if run.exit_code != 0:
            detail = run.final_message or "no diagnostic output"
            raise AgentError(f"{harness.name} exited {run.exit_code}: {detail}")

    async def _stage_mcp_files(
        self, ctx: EpisodeContext, sandbox: Sandbox
    ) -> EffectiveAgentResources:
        resolved = []
        for item in ctx.agent_resources.mcp_servers:
            server = item.server
            if item.staged_files is not None and isinstance(server, StdioMcpServer):
                target = f"{ctx.home}/.ale-mcp/{item.name}"
                await sandbox.upload_dir(
                    str(item.staged_files),
                    target,
                    identity=Identity.AGENT,
                )
                server = server.model_copy(
                    update={
                        "command": server.command.replace("{mcp}", target),
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
        inverted through queues to fit. Now it drives, and the environment is still the
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
            closing = await harness.rollout(env, ctx.session.model_copy(update={"home": ctx.home}))
        if closing:
            builder.add(source="agent", message=closing)
        self._persist_trajectory(ctx, builder.build())
        ctx.extras["trajectory_written"] = True

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

    async def _validate_stdio_mcp(self, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        for resolved in ctx.agent_resources.mcp_servers:
            server = resolved.server
            if not isinstance(server, StdioMcpServer):
                continue
            command = server.command.replace("{home}", ctx.home)
            if command.startswith("/"):
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
                result = await sandbox.exec(["test", "-d", cwd], identity=Identity.AGENT)
                if not result.ok:
                    raise TaskError(f"MCP server {resolved.name!r} cwd is not a directory: {cwd}")

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
        trajectory = ctx.run_dir / "trajectory.json"
        if trajectory.is_file():
            await sandbox.write_file(TRAJECTORY_STAGE_PATH, trajectory.read_bytes())
        await sandbox.write_file(TASK_INSTRUCTION_PATH, ctx.spec.instruction.encode("utf-8"))
        parameters = json.dumps(ctx.spec.params, ensure_ascii=False).encode("utf-8")
        await sandbox.write_file(TASK_PARAMETERS_PATH, parameters)
        await self._stage_verification_config(ctx, sandbox)
        # Scoring is the framework's own work, and a verifier may need to reach something
        # the agent could not. The agent has already finished; nothing it does can follow.
        await sandbox.open_egress()

        destination = await self._site_packages(sandbox)
        framework_source, framework_version, framework_hash = installed_ale_verify()
        await sandbox.upload_dir(
            str(framework_source),
            f"{destination}/ale_verify",
        )
        await self._probe_package(sandbox, "ale-verify", "import ale_verify")
        ctx.extras["ale_verify_provenance"] = AleVerifyProvenance(
            version=framework_version,
            content_hash=framework_hash,
        )
        exit_code = await self._run_stage(ctx, sandbox, VERIFY_DIR, Phase.VERIFY)
        record = await self._collect_verification_record(ctx, sandbox)
        await self._collect_agent_judge_log(ctx, sandbox, record)
        if exit_code != 0:
            if record is not None and (
                (record.failure or "").startswith("judge infrastructure:")
                or any(invocation.status == "failed" for invocation in record.judge_invocations)
            ):
                raise VerificationInfrastructureError(
                    record.failure or f"Judge verification failed with exit code {exit_code}"
                )
            raise TaskError(
                f"verify failed with exit code {exit_code}"
                + (f": {record.failure}" if record is not None and record.failure else "")
            )
        if record is None:
            raise VerifierOutputError(f"verify wrote no record to {VERIFICATION_PATH}")
        if record.status != "completed":
            raise VerifierOutputError(f"verify record is {record.status}, expected completed")
        rewards, metrics = await self._read_verdict(sandbox)
        if rewards != record.rewards or metrics != record.metrics:
            raise VerifierOutputError(
                "verify record and reward envelope contain different rewards or metrics"
            )
        ctx.extras["metrics"] = metrics

        return rewards

    async def _stage_verification_config(self, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        config = ctx.extras.get("verification_config")
        payload = (
            config.model_dump(mode="json", exclude_none=True)
            if hasattr(config, "model_dump")
            else {}
        )
        await sandbox.write_file(
            VERIFY_CONFIG_PATH,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        command_env: dict[str, str] = {}
        secrets: list[str] = []
        for section in payload.values():
            if not isinstance(section, dict):
                continue
            name = section.get("api_key_env")
            if isinstance(name, str) and (value := os.environ.get(name)):
                command_env[name] = value
                secrets.append(value)
        ctx.extras["verification_command_env"] = command_env
        ctx.extras["verification_secrets"] = tuple(secrets)

    async def _collect_verification_record(
        self, ctx: EpisodeContext, sandbox: Sandbox
    ) -> VerificationRecord | None:
        try:
            raw = await sandbox.read_file(VERIFICATION_PATH)
        except Exception:
            return None
        secrets = cast(tuple[str, ...], ctx.extras.get("verification_secrets", ()))
        if any(secret.encode() in raw for secret in secrets if secret):
            raise VerifierOutputError("verify record contains a configured credential")
        try:
            record = VerificationRecord.from_json(raw)
        except ValueError as exc:
            raise VerifierOutputError(f"verify wrote malformed verification record: {exc}") from exc
        atomic_write_json(ctx.run_dir / "verification.json", record.to_dict(), sort_keys=False)
        ctx.extras["verification_record"] = record
        return record

    async def _collect_agent_judge_log(
        self,
        ctx: EpisodeContext,
        sandbox: Sandbox,
        record: VerificationRecord | None,
    ) -> None:
        if record is None:
            return
        launched = any(
            invocation.kind == "agent"
            and any(attempt.request_id for attempt in invocation.attempts)
            for invocation in record.judge_invocations
        )
        try:
            raw = await sandbox.read_file(AGENT_JUDGE_LOG_PATH)
        except Exception as exc:
            if launched:
                raise VerifierOutputError(
                    "Agent Judge launched but wrote no agent-judge.jsonl transcript"
                ) from exc
            return
        redactor = Redactor(cast(tuple[str, ...], ctx.extras.get("verification_secrets", ())))
        target = ctx.run_dir / "logs" / "agent-judge.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            redactor(raw.decode("utf-8", errors="replace")),
            encoding="utf-8",
        )

    async def _capture_solver_evidence(self, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        if ctx.extras.get("solver_evidence_captured"):
            return
        logger = logging.getLogger("ale.execution")
        if self.agent_enabled:
            session = ctx.session.model_copy(
                update={
                    "gateway_url": sandbox.gateway_url or ctx.session.gateway_url,
                    "home": ctx.home,
                    "sandbox_id": sandbox.sandbox_id,
                    "resources_digest": ctx.agent_resources.digest,
                }
            )
            cleanup_error: Exception | None = None
            try:
                await self.harness.cleanup(
                    _RecordedSandbox(ctx, sandbox, component="harness-cleanup"),
                    session,
                )
            except Exception as exc:
                cleanup_error = exc
            subscription = ctx.extras.get("subscription_lease")
            if isinstance(subscription, ProfileLease) and not ctx.extras.get(
                "subscription_finalized"
            ):
                try:
                    ctx.extras["subscription_persistence"] = await subscription.persist(
                        _RecordedSandbox(ctx, sandbox, component="subscription-persist"),
                        ctx.home,
                    )
                finally:
                    try:
                        cleaned = await subscription.cleanup(
                            _RecordedSandbox(ctx, sandbox, component="subscription-cleanup"),
                            ctx.home,
                        )
                    except Exception:
                        cleaned = False
                    ctx.extras["subscription_cleanup"] = "succeeded" if cleaned else "failed"
                    ctx.extras["subscription_finalized"] = True
                    if not cleaned:
                        logger.warning("staged subscription credential cleanup failed")
            if cleanup_error is not None:
                raise cleanup_error
            logger.info("harness cleanup completed")
        # The harness's own logs, in their own place. What the agent produced and how the
        # harness went about producing it are different questions with different owners:
        # the task declares the first because only it knows what its output is, and the
        # harness declares the second because only it knows what it writes.
        for name in getattr(self.harness, "logs", ()):
            try:
                await ctx.artifacts.collect_file(
                    sandbox, f"{ctx.home}/{name}", f"{self.harness.name}/{name}"
                )
                logger.info(
                    "native log collected",
                    extra={"ale_data": {"harness": self.harness.name, "name": name}},
                )
            except Exception as exc:
                logger.warning(
                    "native log collection failed",
                    extra={
                        "ale_data": {
                            "harness": self.harness.name,
                            "name": name,
                            "error": str(exc),
                        }
                    },
                )

        if ctx.extras.get("agent_started") and not ctx.extras.get("trajectory_written"):
            self._parse_harness_trajectory(ctx)

        if ctx.artifacts.enabled:
            for index, path in enumerate(ctx.spec.artifacts):
                name = Path(path).name or f"artifact-{index}"
                await ctx.artifacts.collect(sandbox, path, name)
                logger.info(
                    "artifact collected",
                    extra={"ale_data": {"source": path, "name": name}},
                )

        ctx.extras["solver_evidence_captured"] = True

    async def _teardown(self, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        # Best-effort interrupted evidence does not replace the primary failure.
        if ctx.extras.get("agent_started") and not ctx.extras.get("solver_evidence_captured"):
            try:
                await self._capture_solver_evidence(ctx, sandbox)
            except Exception as exc:
                logging.getLogger("ale.execution").warning(
                    "interrupted solver evidence cleanup failed",
                    extra={"ale_data": {"error": str(exc)}},
                )
        await self._sanitize_for_retention(ctx, sandbox)
        await ctx.sandboxes.release(sandbox)
        logging.getLogger("ale.execution").info("sandbox released")

    async def _sanitize_for_retention(self, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        if sandbox.request.retention == "destroy":
            ctx.sandboxes.mark_sanitized(sandbox, succeeded=True)
            return
        credential_paths = [
            f"{ctx.home}/.codex-ale/auth.json",
            f"{ctx.home}/.grok-ale/auth.json",
        ]
        credential_cleanup = await sandbox.exec(["rm", "-f", "--", *credential_paths])
        if not credential_cleanup.ok:
            logging.getLogger("ale.execution").warning(
                "retained sandbox subscription credential cleanup failed"
            )
        paths = [
            str(VERIFY_CONFIG_PATH),
            str(VERIFY_DIR / "agent"),
            str(VERIFY_DIR / "agent-tools"),
            f"{ctx.home}/.codex/auth.json",
            f"{ctx.home}/.claude/.credentials.json",
        ]
        result = await sandbox.exec(["rm", "-rf", "--", *paths])
        ctx.sandboxes.mark_sanitized(
            sandbox,
            succeeded=result.ok,
            reason=None if result.ok else result.stderr.strip() or "credential sanitation failed",
        )

    def _parse_harness_trajectory(self, ctx: EpisodeContext) -> None:
        assert ctx.blobs is not None
        assert ctx.trajectory is not None
        run = ctx.extras.get("agent_run")
        trajectory = self.harness.parse_trajectory(
            TrajectoryParseContext(
                episode_id=ctx.episode_id,
                trajectory_id=ctx.trajectory_id,
                instruction=ctx.spec.instruction,
                logs_dir=ctx.run_dir / "logs" / self.harness.name,
                model=ctx.session.model,
                agent_version=ctx.agent_version or self.harness.version(),
                blobs=ctx.blobs,
                session_id=getattr(getattr(run, "continuation", None), "native_session_id", None),
                final_message=getattr(run, "final_message", None),
                incomplete=run is None,
                incomplete_reason="agent_interrupted" if run is None else None,
            )
        )
        self._persist_trajectory(ctx, trajectory)
        ctx.extras["trajectory_written"] = True
        policy = ctx.extras.get("logging_policy")
        if run is not None and getattr(policy, "native_logs", "minimal") == "minimal":
            shutil.rmtree(ctx.run_dir / "logs" / self.harness.name, ignore_errors=True)

    def _persist_trajectory(self, ctx: EpisodeContext, trajectory: AtifTrajectory) -> None:
        """Correlate only observed provider/native IDs, then persist before links."""
        assert ctx.trajectory is not None
        calls = {
            record["provider_response_id"]: record["call_id"]
            for record in read_jsonl(ctx.run_dir / "trace.transport.jsonl").records
            if record.get("kind") == "call"
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
        trajectory = trajectory.model_copy(update={"steps": steps})
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
        self, ctx: EpisodeContext, sandbox: Sandbox, directory: PurePosixPath, phase: Phase
    ) -> int:
        entry = directory / "run.sh"
        env = {
            "ALE_STAGE_DIR": str(directory),
            "ALE_HOME": ctx.home,
            "ALE_PARAMS_JSON": str(directory / "params.json"),
            "ALE_VERDICT_PATH": str(VERDICT_PATH),
        }
        if phase is Phase.VERIFY:
            env.update(
                {
                    "ALE_EPISODE_ID": ctx.episode_id,
                    "ALE_VERIFICATION_PATH": str(VERIFICATION_PATH),
                    "ALE_VERIFY_CONFIG_PATH": str(VERIFY_CONFIG_PATH),
                    "ALE_TASK_INSTRUCTION_PATH": str(TASK_INSTRUCTION_PATH),
                    "ALE_TASK_PARAMETERS_PATH": str(TASK_PARAMETERS_PATH),
                    "ALE_AGENT_JUDGE_LOG_PATH": str(AGENT_JUDGE_LOG_PATH),
                    **cast(
                        dict[str, str],
                        ctx.extras.get("verification_command_env", {}),
                    ),
                }
            )
            if (ctx.run_dir / "trajectory.json").is_file():
                env["ALE_TRAJECTORY_PATH"] = str(TRAJECTORY_STAGE_PATH)
        await sandbox.write_file(
            directory / "params.json", json.dumps(ctx.spec.params).encode("utf-8")
        )
        assert ctx.execution is not None
        assert ctx.blobs is not None
        execution_id = f"{phase.value}-{len(ctx.phases) + 1}"
        recorder = CommandRecorder(
            execution=ctx.execution,
            blobs=cast(BlobStore, ctx.blobs),
            episode_id=ctx.episode_id,
            phase=phase.value,
            component="task-stage",
            execution_id=execution_id,
            actor="task",
            argv=["bash", entry.name],
            cwd=str(directory),
            secrets=tuple(
                secret
                for secret in (
                    ctx.session.token,
                    *cast(tuple[str, ...], ctx.extras.get("verification_secrets", ())),
                )
                if secret
            ),
        )
        started = time.monotonic()
        timeout_sec = cast(float | None, ctx.extras.get("phase_timeout_sec"))
        try:
            result = await sandbox.exec(
                ["bash", entry.name],
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

    async def _probe_package(self, sandbox: Sandbox, name: str, statement: str) -> None:
        result = await sandbox.exec(["python3", "-c", statement])
        if not result.ok:
            raise TaskError(
                f"package {name!r} is incompatible with the selected image: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    async def _site_packages(self, sandbox: Sandbox) -> str:
        """Where this image's interpreter looks for installed packages."""
        result = await sandbox.exec(
            [
                "python3",
                "-c",
                (
                    "import sys,sysconfig;"
                    "assert sys.version_info >= (3,12), "
                    "f'ale_verify requires Python 3.12+, got {sys.version.split()[0]}';"
                    "print(sysconfig.get_paths()['purelib'])"
                ),
            ],
        )
        path = result.stdout.strip()
        if result.exit_code != 0 or not path:
            raise TaskError(
                "verify-stage python3 must be Python 3.12+ and expose site-packages: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return path

    async def _upload_oracle(self, ctx: EpisodeContext, task: Task, sandbox: Sandbox) -> None:
        folder = getattr(task, "folder", None)
        source = folder.stage_dir("oracle") if folder else None
        if source is None:
            raise TaskError("validation requires an oracle stage, and this task has none")
        # As the agent, since the agent is the account that will run it.
        root = oracle_dir(ctx.home)
        await sandbox.upload_dir(str(source), str(root), identity=Identity.AGENT)
        await sandbox.write_file(
            root / "params.json",
            json.dumps(task.spec.params).encode("utf-8"),
            identity=Identity.AGENT,
        )

    async def _read_verdict(self, sandbox: Sandbox) -> tuple[dict[str, float], dict[str, float]]:
        try:
            raw = await sandbox.read_file(VERDICT_PATH)
        except Exception as exc:
            raise VerifierOutputError(f"verify wrote no rewards to {VERDICT_PATH}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
            rewards = payload["rewards"]
            if not isinstance(rewards, dict) or not rewards:
                raise ValueError("rewards must be a non-empty object")
            parsed = {str(key): float(value) for key, value in rewards.items()}
            if any(not key.strip() for key in parsed):
                raise ValueError("reward names must be non-empty")
            if any(not math.isfinite(value) for value in parsed.values()):
                raise ValueError("reward values must be finite")
            metrics = payload.get("metrics", {})
            if not isinstance(metrics, dict):
                raise ValueError("metrics must be an object")
            parsed_metrics = {str(key): float(value) for key, value in metrics.items()}
            if any(not key.strip() for key in parsed_metrics):
                raise ValueError("metric names must be non-empty")
            if any(not math.isfinite(value) for value in parsed_metrics.values()):
                raise ValueError("metric values must be finite")
            return parsed, parsed_metrics
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise VerifierOutputError(f"verify wrote malformed rewards: {exc}") from exc

    async def _read_rewards(self, sandbox: Sandbox) -> dict[str, float]:
        rewards, _ = await self._read_verdict(sandbox)
        return rewards

    async def _with_deadline(self, ctx, phase: Phase, seconds: float, coro):  # type: ignore[no-untyped-def]
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
        callback = ctx.extras.get("phase_callback")
        if callable(callback):
            callback(phase)
        ctx.execution.append(
            PhaseStarted(
                episode_id=ctx.episode_id,
                phase=phase.value,
                component=self.name,
            ),
            durable=True,
        )
        outcome = "succeeded"
        previous_timeout = ctx.extras.get("phase_timeout_sec")
        ctx.extras["phase_timeout_sec"] = timeout_sec
        try:
            with execution_logging(
                ctx.execution,
                episode_id=ctx.episode_id,
                phase=phase.value,
                component=self.name,
                secrets=(ctx.session.token,),
            ):
                if timeout_sec is None:
                    return await coro
                async with asyncio.timeout(timeout_sec):
                    return await coro
        except TimeoutError as exc:
            outcome = "timed_out"
            ctx.extras["failure_phase"] = phase.value
            raise PhaseTimeoutError(phase.value, timeout_sec or 0) from exc
        except PhaseTimeoutError:
            outcome = "timed_out"
            ctx.extras["failure_phase"] = phase.value
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "failed"
            ctx.extras["failure_phase"] = phase.value
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
                    component=self.name,
                    level="info" if outcome == "succeeded" else "error",
                    outcome=outcome,  # type: ignore[arg-type]
                    duration_ms=elapsed,
                ),
                durable=True,
            )
            ctx.current_phase = None
            if previous_timeout is None:
                ctx.extras.pop("phase_timeout_sec", None)
            else:
                ctx.extras["phase_timeout_sec"] = previous_timeout


def _agent_user(sandbox: Sandbox) -> str:
    """The account this sandbox calls the agent, for a plain `chown`."""
    return getattr(sandbox, "agent_user", "user")


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
    ) -> None:
        self._ctx = ctx
        self._sandbox = sandbox
        self._component = component
        self._actor = actor

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
        counter = int(self._ctx.extras.get("harness_execution_counter", 0)) + 1
        self._ctx.extras["harness_execution_counter"] = counter
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
            secrets=(self._ctx.session.token,),
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
                env=env,
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
        return result
