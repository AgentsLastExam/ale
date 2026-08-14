"""Harbor's single-step evaluation protocol on ALE episode machinery."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import math
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import cast

from ale.core.environment import EpisodeContext
from ale.core.errors import AgentError, TaskDefinitionError, TaskError, VerifierOutputError
from ale.core.harness import AgentRun, AutonomousHarness, PolicyHarness
from ale.core.lock import SandboxProvenance
from ale.core.sandbox import Identity, Sandbox, SandboxRequest, SandboxRole
from ale.core.taskspec import NetworkMode, NetworkPolicy, VerificationMode
from ale.run.environments.standard import StandardEnvironment
from ale.run.envs import DEFAULT_MAX_STEPS, DEFAULT_STALL_LIMIT
from ale.run.harbor.config import HarborArtifactConfig, HarborHealthcheckConfig
from ale.run.harbor.task import HarborTask, HarborTaskSpec

__all__ = ["HarborEnvironment"]

_TESTS_DIR = PurePosixPath("/tests")
_VERIFIER_DIR = PurePosixPath("/logs/verifier")
_REWARD_JSON = _VERIFIER_DIR / "reward.json"
_REWARD_TEXT = _VERIFIER_DIR / "reward.txt"
_CONVENTION_ARTIFACT = HarborArtifactConfig(source="/logs/artifacts")


class HarborEnvironment(StandardEnvironment):
    """Linear Harbor provision -> agent/oracle -> tests/test.sh flow."""

    name = "harbor"

    def __init__(
        self,
        harness: AutonomousHarness | PolicyHarness,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        stall_limit: int = DEFAULT_STALL_LIMIT,
        agent_enabled: bool = True,
    ) -> None:
        super().__init__(
            harness,
            max_steps=max_steps,
            stall_limit=stall_limit,
            agent_enabled=agent_enabled,
        )

    def _validate_task(self, task) -> None:  # type: ignore[no-untyped-def]
        if not isinstance(task, HarborTask) or not isinstance(task.spec, HarborTaskSpec):
            raise TaskError("HarborEnvironment requires a HarborTaskSpec")

    async def _provision(self, ctx: EpisodeContext) -> Sandbox:
        spec = _spec(ctx)
        if ctx.prepared_image is None:
            raise TaskError("Harbor solver image was not prepared")
        network = spec.network
        if ctx.proxy_url and ctx.allowed_hosts and network.mode is not NetworkMode.OPEN:
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
            env={"ALE_EPISODE_ID": ctx.episode_id, **_resolve_env(spec.environment_env)},
            sudo=False,
        )
        sandbox = await ctx.sandboxes.acquire(request)
        ctx.resolved_image = sandbox.resolved_image
        ctx.resource_allocation = sandbox.allocation
        ctx.home = spec.workdir or getattr(sandbox, "workdir", "/")
        if hasattr(sandbox, "workdir"):
            sandbox.workdir = ctx.home  # type: ignore[attr-defined]
        await _prepare_workdir(sandbox, ctx.home)
        user = str(getattr(sandbox, "agent_user", "root"))
        ctx.sandbox_identity = SandboxProvenance(user=user, sudo=False)
        return sandbox

    async def _provision_verifier(self, ctx: EpisodeContext) -> Sandbox:
        spec = _spec(ctx)
        resources = spec.verify.resources
        if resources is None or ctx.prepared_verifier_image is None:
            raise TaskError("separate Harbor verifier image/resources were not prepared")
        request = SandboxRequest(
            episode_id=ctx.episode_id,
            role=SandboxRole.VERIFIER,
            prepared_image=ctx.prepared_verifier_image,
            resources=resources.as_resources(),
            network=spec.verifier_network,
            env={
                "ALE_EPISODE_ID": ctx.episode_id,
                **_resolve_env(spec.verifier_environment_env),
            },
            sudo=False,
        )
        sandbox = await ctx.sandboxes.acquire(request)
        if spec.verifier_workdir and hasattr(sandbox, "workdir"):
            sandbox.workdir = spec.verifier_workdir  # type: ignore[attr-defined]
            await _prepare_workdir(sandbox, spec.verifier_workdir)
        ctx.verifier_resolved_image = sandbox.resolved_image
        ctx.verifier_resource_allocation = sandbox.allocation
        return sandbox

    async def _setup(self, task, ctx: EpisodeContext, sandbox: Sandbox) -> None:  # type: ignore[no-untyped-def]
        await sandbox.open_egress()
        await _healthcheck(sandbox, _spec(ctx).healthcheck)

    async def _agent(
        self,
        task,
        ctx: EpisodeContext,
        sandbox: Sandbox,  # type: ignore[no-untyped-def]
    ) -> None:
        if self.harness.name != "oracle":
            await super()._agent(task, ctx, sandbox)
            return
        source = cast(HarborTask, task).folder.stage_dir("oracle")
        entry = cast(HarborTask, task).folder.stage_entry("oracle")
        if source is None or entry is None:
            raise TaskError("Harbor validation requires solution/solve.sh")
        ctx.agent_started = True
        await sandbox.upload_dir(str(source), "/solution", identity=Identity.AGENT)
        ctx.agent_version = await self.harness.install(sandbox)
        await self._seal(ctx, sandbox)
        result = await sandbox.exec(
            ["bash", "/solution/solve.sh"],
            cwd=ctx.home,
            env=_resolve_env(_spec(ctx).solution_env),
            timeout_sec=_spec(ctx).timeouts.agent,
            identity=Identity.AGENT,
        )
        ctx.agent_run = AgentRun(
            exit_code=result.exit_code if result.exit_code is not None else -1,
            final_message=result.stderr.strip() or result.stdout.strip() or None,
        )
        if not result.ok:
            raise AgentError(
                f"Harbor oracle exited {result.exit_code}: "
                f"{result.stderr[-500:] or result.stdout[-500:]}"
            )

    async def _verify(
        self,
        task,
        ctx: EpisodeContext,
        sandbox: Sandbox,  # type: ignore[no-untyped-def]
    ) -> dict[str, float]:
        spec = _spec(ctx)
        if sandbox.request.role is SandboxRole.VERIFIER:
            await self._restore_harbor_artifacts(ctx, sandbox)
            await _healthcheck(sandbox, spec.verifier_healthcheck)

        tests = cast(HarborTask, task).folder.stage_dir("verify")
        test_entry = cast(HarborTask, task).folder.stage_entry("verify")
        await _checked(sandbox, ["rm", "-rf", "--", str(_VERIFIER_DIR)])
        await _checked(sandbox, ["mkdir", "-p", "--", str(_VERIFIER_DIR)])
        await _checked(sandbox, ["chmod", "0777", str(_VERIFIER_DIR)])
        if tests is not None and test_entry is not None:
            await _checked(sandbox, ["rm", "-rf", "--", str(_TESTS_DIR)])
            await sandbox.upload_dir(str(tests), _TESTS_DIR)
        staged_test = _TESTS_DIR / "test.sh"
        await _checked(sandbox, ["chmod", "+x", str(staged_test)])
        result = await sandbox.exec(
            [str(staged_test)],
            cwd=str(getattr(sandbox, "workdir", ctx.home)),
            env=_resolve_env(spec.verifier_env),
            timeout_sec=ctx.phase_timeout_sec,
            identity=Identity.AGENT,
        )
        log_dir = ctx.run_dir / "logs" / "verifier"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "test-stdout.txt").write_text(result.stdout, encoding="utf-8")
        (log_dir / "test-stderr.txt").write_text(result.stderr, encoding="utf-8")
        try:
            rewards = await _read_rewards(sandbox)
        except VerifierOutputError as exc:
            if not result.ok:
                raise TaskError(
                    f"Harbor verifier exited {result.exit_code}: "
                    f"{result.stderr[-500:] or result.stdout[-500:]}"
                ) from exc
            raise
        ctx.metrics = {}
        return rewards

    def _artifact_paths(self, ctx: EpisodeContext) -> tuple[str, ...]:
        return ()

    async def _capture_solver_evidence(self, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        if ctx.solver_evidence_captured:
            return
        await super()._capture_solver_evidence(ctx, sandbox)
        if not ctx.artifacts.enabled:
            return
        entries = list(_spec(ctx).artifacts)
        if not any(item.source.rstrip("/") == "/logs/artifacts" for item in entries):
            entries.insert(0, _CONVENTION_ARTIFACT)
        records: list[dict[str, str]] = []
        claimed: list[Path] = []
        for entry in entries:
            source = _artifact_source(entry.source, ctx.home)
            exists = await sandbox.exec(["test", "-e", source])
            if not exists.ok:
                continue
            target = ctx.run_dir / "artifacts" / _artifact_destination(entry, source)
            if any(
                target == item or target in item.parents or item in target.parents
                for item in claimed
            ):
                continue
            directory = (await sandbox.exec(["test", "-d", source])).ok
            target.parent.mkdir(parents=True, exist_ok=True)
            if directory:
                target.mkdir(parents=True, exist_ok=True)
                await sandbox.download_dir(source, str(target))
                _apply_excludes(target, entry.exclude)
                _reject_symlinks(target)
                kind = "directory"
            else:
                target.write_bytes(await sandbox.read_file(source))
                kind = "file"
            claimed.append(target)
            records.append(
                {
                    "source": source,
                    "stored_path": target.relative_to(ctx.run_dir / "artifacts").as_posix(),
                    "kind": kind,
                }
            )
        ctx.extras["harbor_artifacts"] = records
        if records:
            manifest = ctx.run_dir / "artifacts" / "manifest.json"
            manifest.write_text(json.dumps({"entries": records}, indent=2) + "\n")

    async def _restore_harbor_artifacts(self, ctx: EpisodeContext, sandbox: Sandbox) -> None:
        records = cast(list[dict[str, str]], ctx.extras.get("harbor_artifacts", []))
        for record in records:
            source = record["source"]
            stored = ctx.run_dir / "artifacts" / record["stored_path"]
            await _checked(sandbox, ["rm", "-rf", "--", source])
            await _checked(sandbox, ["mkdir", "-p", "--", str(PurePosixPath(source).parent)])
            if record["kind"] == "directory":
                await sandbox.upload_dir(str(stored), source)
            else:
                await sandbox.write_file(source, stored.read_bytes())


def _spec(ctx: EpisodeContext) -> HarborTaskSpec:
    if not isinstance(ctx.spec, HarborTaskSpec):
        raise TaskError("HarborEnvironment requires a HarborTaskSpec")
    return ctx.spec


async def _prepare_workdir(sandbox: Sandbox, workdir: str) -> None:
    exists = await sandbox.exec(["test", "-d", workdir], cwd="/")
    if exists.ok:
        return
    result = await sandbox.exec(["mkdir", "-p", "--", workdir], cwd="/")
    if not result.ok:
        raise TaskError(f"could not prepare Harbor workdir {workdir}: {result.stderr.strip()}")
    user = str(getattr(sandbox, "agent_user", "root"))
    if user not in {"", "0", "root"}:
        result = await sandbox.exec(["chown", user, workdir], cwd="/")
        if not result.ok:
            raise TaskError(f"could not give Harbor workdir {workdir} to {user}")


async def _healthcheck(sandbox: Sandbox, healthcheck: HarborHealthcheckConfig | None) -> None:
    if healthcheck is None:
        return
    loop = asyncio.get_running_loop()
    start_period_end = loop.time() + healthcheck.start_period_sec
    failures = 0
    while True:
        in_start_period = loop.time() < start_period_end
        result = await sandbox.exec(
            ["sh", "-c", healthcheck.command],
            timeout_sec=healthcheck.timeout_sec,
            identity=Identity.AGENT,
        )
        if result.ok:
            return
        if in_start_period:
            await asyncio.sleep(healthcheck.start_interval_sec)
            continue
        failures += 1
        if failures >= healthcheck.retries:
            raise TaskError(
                f"Harbor healthcheck failed after {healthcheck.retries} attempts: "
                f"{healthcheck.command}"
            )
        await asyncio.sleep(healthcheck.interval_sec)


async def _checked(sandbox: Sandbox, argv: list[str]) -> None:
    result = await sandbox.exec(argv)
    if not result.ok:
        raise TaskError(f"Harbor sandbox command failed: {' '.join(argv)}: {result.stderr.strip()}")


async def _read_rewards(sandbox: Sandbox) -> dict[str, float]:
    payload: object
    try:
        raw = await sandbox.read_file(_REWARD_JSON)
    except Exception:
        try:
            raw = await sandbox.read_file(_REWARD_TEXT)
        except Exception as exc:
            raise VerifierOutputError(
                f"Harbor verifier wrote neither {_REWARD_JSON} nor {_REWARD_TEXT}"
            ) from exc
        try:
            payload = {"reward": float(raw.decode().strip())}
        except ValueError as exc:
            raise VerifierOutputError("Harbor reward.txt is not numeric") from exc
    else:
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerifierOutputError("Harbor reward.json is malformed") from exc
    if not isinstance(payload, dict) or not payload:
        raise VerifierOutputError("Harbor rewards must be a non-empty object")
    try:
        rewards = {str(key): float(value) for key, value in payload.items()}
    except (TypeError, ValueError) as exc:
        raise VerifierOutputError("Harbor reward values must be numeric") from exc
    if any(not key.strip() for key in rewards) or any(
        not math.isfinite(value) for value in rewards.values()
    ):
        raise VerifierOutputError("Harbor reward names and values must be finite")
    return rewards


def _resolve_env(values: dict[str, str]) -> dict[str, str]:
    return {key: _resolve_env_value(value) for key, value in values.items()}


def _resolve_env_value(value: str) -> str:
    import re

    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        resolved = os.environ.get(name)
        if resolved is not None and resolved != "":
            return resolved
        if default is not None:
            return default
        raise TaskDefinitionError(f"required Harbor environment variable {name} is unset")

    return pattern.sub(replace, value)


def _artifact_source(source: str, workdir: str) -> str:
    path = PurePosixPath(source)
    return str(path if path.is_absolute() else PurePosixPath(workdir) / path)


def _artifact_destination(entry: HarborArtifactConfig, source: str) -> Path:
    if entry.destination:
        return Path(entry.destination)
    parts = [part for part in PurePosixPath(source).parts if part not in {"", "/", ".."}]
    return Path(*parts) if parts else Path("root")


def _apply_excludes(root: Path, patterns: tuple[str, ...]) -> None:
    if not patterns:
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        relative = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)


def _reject_symlinks(root: Path) -> None:
    symlink = next((path for path in root.rglob("*") if path.is_symlink()), None)
    if symlink is not None:
        raise TaskError(f"Harbor artifact contains a symlink: {symlink.relative_to(root)}")
