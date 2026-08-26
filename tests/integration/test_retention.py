from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.config import LoggingPolicy, RunConfig, SandboxRetentionConfig
from ale.core.errors import TrajectoryConversionError
from ale.core.harness import AgentRun
from ale.core.lock import TaskSource
from ale.core.sandbox import (
    ExecResult,
    ImageKind,
    PreparedTaskImage,
    RetainedSandbox,
    SandboxRequest,
)
from ale.core.taskspec import NetworkPolicy, Resources
from ale.core.verdict import Status
from ale.run.cli.tasks import _reverify
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import _Lease, run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers.docker import (
    DockerProvider,
    DockerSandbox,
    destroy_retained,
    list_retained,
)
from ale.run.recording import EpisodeRecording
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from tests.support import provider_registry

pytestmark = pytest.mark.integration


def _request(kind: ImageKind, role: str) -> SandboxRequest:
    digest = "sha256:" + ("1" if kind is ImageKind.CONTAINER else "2") * 64
    return SandboxRequest(
        episode_id="mixed",
        role=role,
        prepared_image=PreparedTaskImage(
            kind=kind,
            source="external-ref",
            input_identity=digest,
            runtime_ref="image@" + digest if kind is ImageKind.CONTAINER else "/tmp/disk.qcow2",
            prepared_identity=digest,
            resolved_reference="registry/image@" + digest,
        ),
        resources=Resources(),
        network=NetworkPolicy(),
    )


@pytest.mark.asyncio
async def test_mixed_provider_retention_keeps_actual_provider_attribution() -> None:
    class Sandbox:
        def __init__(self, provider: str, request: SandboxRequest) -> None:
            self.provider = provider
            self.request = request
            self.sandbox_id = f"{provider}-{request.role}"
            self.destroyed = False

        async def destroy(self) -> None:
            self.destroyed = True

        def release_resources(self) -> None:
            pass

        async def retain(self, *, roles, reason):  # type: ignore[no-untyped-def]
            return RetainedSandbox(
                provider=self.provider,
                handle=self.sandbox_id,
                episode_id="mixed",
                roles=roles,
                reason=reason,
                cleanup_command=f"cleanup {self.sandbox_id}",
            )

    class Provider:
        def __init__(self, name: str) -> None:
            self.name = name

        async def create(self, request: SandboxRequest) -> Sandbox:
            return Sandbox(self.name, request)

    docker, qemu = Provider("docker"), Provider("qemu")
    registry = SimpleNamespace(get=lambda kind: docker if kind is ImageKind.CONTAINER else qemu)
    lease = _Lease(
        registry,  # type: ignore[arg-type]
        SandboxRetentionConfig(solver="keep", verifier="destroy"),
    )
    solver = await lease.acquire(_request(ImageKind.CONTAINER, "solver"))
    verifier = await lease.acquire(_request(ImageKind.VM, "verifier"))
    lease.mark_sanitized(solver, succeeded=True)  # type: ignore[arg-type]
    await lease.release(verifier)  # type: ignore[arg-type]
    await lease.finalize()

    assert [(item.provider, item.outcome) for item in lease.outcomes] == [
        ("qemu", "destroyed"),
        ("docker", "retained"),
    ]


def context(root: Path, policy: LoggingPolicy, run: AgentRun | None):
    recording = EpisodeRecording(root)
    logs = root / "logs/claude-code"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text(
        json.dumps({"type": "result", "session_id": "session", "result": "done"}) + "\n"
    )
    return SimpleNamespace(
        blobs=recording.blobs,
        trajectory=recording,
        transport=recording.transport,
        episode_id=root.name,
        trajectory_id="trajectory",
        run_dir=root,
        session=SimpleNamespace(model="model"),
        agent_version="1",
        spec=SimpleNamespace(instruction="instruction"),
        agent_run=run,
        logging_policy=policy,
        trajectory_written=False,
    )


def test_minimal_deletes_successful_native_logs(tmp_path: Path) -> None:
    root = tmp_path / "minimal"
    ctx = context(root, LoggingPolicy(), AgentRun(exit_code=0, final_message="done"))
    StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(ctx)  # type: ignore[arg-type]
    assert (root / "trajectory.json").is_file()
    assert not (root / "logs/claude-code").exists()


def test_debug_and_interruption_keep_native_logs(tmp_path: Path) -> None:
    debug = tmp_path / "debug"
    debug_ctx = context(
        debug,
        LoggingPolicy(native_logs="debug"),
        AgentRun(exit_code=0, final_message="done"),
    )
    StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(debug_ctx)  # type: ignore[arg-type]
    assert (debug / "logs/claude-code/transcript.jsonl").is_file()

    interrupted = tmp_path / "interrupted"
    interrupted_ctx = context(interrupted, LoggingPolicy(), None)
    StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(  # type: ignore[arg-type]
        interrupted_ctx
    )
    assert (interrupted / "logs/claude-code/transcript.jsonl").is_file()
    trajectory = json.loads((interrupted / "trajectory.json").read_text())
    assert trajectory["extra"]["ale"]["incomplete"] is True


@pytest.mark.needs_docker
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "separate"])
async def test_retained_episode_is_reverified_without_rerunning_agent(
    tmp_path: Path, mode: str
) -> None:
    task_path = scaffold_task(tmp_path / "reverify")
    if mode == "separate":
        manifest = task_path / "task.yaml"
        manifest.write_text(
            manifest.read_text()
            + "verify:\n"
            + "  environment_mode: separate\n"
            + "  resources: {cpus: 1, memory_mb: 512, storage_mb: null, gpus: 0}\n"
        )
    stale = task_path / "verify/stale.txt"
    stale.write_text("must not survive a verifier revision\n")
    dockerfile = task_path / "image/Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + "\nLABEL ale.gui=false\n")
    registry = provider_registry(DockerProvider())
    task = load_tasks(task_path)[0]
    settings = RunConfig()
    first = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        registry,
        run_dir=tmp_path / "first",
        provenance=ProvenanceInputs(
            source=TaskSource(kind="local", path=str(task_path)),
            agent=agent_provenance(OracleHarness(), ""),
            gateway=gateway_provenance(settings),
            config_hash=settings.config_hash,
        ),
        sandbox_retention=SandboxRetentionConfig(solver="keep", verifier="destroy"),
    )
    retained = next(item for item in first.record.sandboxes if item.outcome == "retained")
    assert retained.handle is not None
    assert retained.roles == (("solver", "verifier") if mode == "shared" else ("solver",))
    assert task.prepared_image is not None

    stale.unlink()
    (task_path / "verify/verify.py").write_text(
        "from ale_verify import Verification, checks\n"
        "v = Verification()\n"
        "v.check('reverified', checks.text_equals("
        "'/home/user/output/result.txt', 'hello\\n'))\n"
        "v.check('stale_removed', checks.file_missing('/opt/ale/verify/stale.txt'))\n"
        "v.write()\n"
    )
    try:
        assert await _reverify(str(task_path), first.run_dir, settings, tmp_path / "second") == 0
        result_path = next((tmp_path / "second").rglob("result.json"))
        result = json.loads(result_path.read_text())
        assert result["rewards"] == {"reverified": 1.0, "stale_removed": 1.0}
        if mode == "separate":
            assert result["sandboxes"][0]["roles"] == ["verifier"]
        assert retained.handle in {item["handle"] for item in await list_retained()}
    finally:
        await destroy_retained(retained.handle)


def test_conversion_failure_keeps_native_logs(tmp_path: Path) -> None:
    root = tmp_path / "failed"
    ctx = context(root, LoggingPolicy(), AgentRun(exit_code=0, final_message="done"))
    transcript = root / "logs/claude-code/transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "orphan",
                            "content": "unmatched",
                        }
                    ]
                },
            }
        )
        + "\n"
    )

    with pytest.raises(TrajectoryConversionError):
        StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(ctx)  # type: ignore[arg-type]

    assert transcript.is_file()


def test_minimal_retention_is_smaller_than_debug(tmp_path: Path) -> None:
    minimal = tmp_path / "minimal-size"
    debug = tmp_path / "debug-size"
    StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(  # type: ignore[arg-type]
        context(minimal, LoggingPolicy(), AgentRun(exit_code=0, final_message="done"))
    )
    StandardEnvironment(ClaudeCodeHarness())._parse_harness_trajectory(  # type: ignore[arg-type]
        context(
            debug,
            LoggingPolicy(native_logs="debug"),
            AgentRun(exit_code=0, final_message="done"),
        )
    )

    minimal_size = sum(path.stat().st_size for path in minimal.rglob("*") if path.is_file())
    debug_size = sum(path.stat().st_size for path in debug.rglob("*") if path.is_file())
    assert debug_size > minimal_size


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_shared_keep_returns_one_sanitized_actionable_handle(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "shared-keep")
    oracle = task / "oracle/run.sh"
    oracle.write_text(
        oracle.read_text()
        + "mkdir -p /home/user/.codex /home/user/.claude\n"
        + "printf secret > /home/user/.codex/auth.json\n"
        + "printf secret > /home/user/.claude/.credentials.json\n"
    )
    result = await run_episode(
        load_tasks(task)[0],
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
        sandbox_retention=SandboxRetentionConfig(solver="keep", verifier="destroy"),
    )
    outcome = result.record.sandboxes[0]
    assert result.verdict.status is Status.COMPLETED
    assert outcome.roles == ("solver", "verifier")
    assert outcome.outcome == "retained"
    assert outcome.handle and outcome.cleanup_command
    container = outcome.handle.removeprefix("docker:")
    try:
        retained = await list_retained()
        assert any(item["handle"] == outcome.handle for item in retained)
        probe = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            container,
            "test",
            "!",
            "-e",
            "/opt/ale/verify/config.json",
        )
        assert await probe.wait() == 0
        credentials = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            "test ! -e /home/user/.codex/auth.json && "
            "test ! -e /home/user/.claude/.credentials.json",
        )
        assert await credentials.wait() == 0
    finally:
        await destroy_retained(outcome.handle)


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_separate_retention_applies_to_solver_and_verifier_independently(
    tmp_path: Path,
) -> None:
    task = scaffold_task(tmp_path / "separate-keep")
    manifest = task / "task.yaml"
    manifest.write_text(
        manifest.read_text()
        + "verify:\n"
        + "  environment_mode: separate\n"
        + "  resources: {cpus: 1, memory_mb: 512, storage_mb: null, gpus: 0}\n"
    )
    result = await run_episode(
        load_tasks(task)[0],
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
        sandbox_retention=SandboxRetentionConfig(solver="keep", verifier="destroy"),
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    by_role = {outcome.roles: outcome for outcome in result.record.sandboxes}
    assert by_role[("solver",)].outcome == "retained"
    assert by_role[("verifier",)].outcome == "destroyed"
    handle = by_role[("solver",)].handle
    assert handle is not None
    try:
        assert any(item["handle"] == handle for item in await list_retained())
    finally:
        await destroy_retained(handle)


@pytest.mark.needs_docker
@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["setup", "agent"])
async def test_shared_failure_still_applies_keep_and_records_the_phase(
    tmp_path: Path,
    failure_phase: str,
) -> None:
    task = scaffold_task(tmp_path / f"shared-{failure_phase}-failure")
    if failure_phase == "setup":
        setup = task / "setup/run.sh"
        setup.parent.mkdir()
        setup.write_text("#!/bin/sh\nexit 7\n")
        setup.chmod(0o755)
    else:
        oracle = task / "oracle/run.sh"
        oracle.write_text("#!/bin/sh\nexit 8\n")

    result = await run_episode(
        load_tasks(task)[0],
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
        sandbox_retention=SandboxRetentionConfig(solver="keep", verifier="destroy"),
    )
    outcome = result.record.sandboxes[0]
    assert result.verdict.status is not Status.COMPLETED
    assert result.verdict.failure is not None
    assert result.verdict.failure.phase == failure_phase
    assert outcome.outcome == "retained"
    assert outcome.handle is not None
    try:
        assert any(item["handle"] == outcome.handle for item in await list_retained())
    finally:
        await destroy_retained(outcome.handle)


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_separate_verifier_failure_retains_each_requested_sandbox(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "separate-verifier-failure")
    manifest = task / "task.yaml"
    manifest.write_text(
        manifest.read_text()
        + "verify:\n"
        + "  environment_mode: separate\n"
        + "  resources: {cpus: 1, memory_mb: 512, storage_mb: null, gpus: 0}\n"
    )
    (task / "verify/verify.py").write_text("raise RuntimeError('verifier failed')\n")
    result = await run_episode(
        load_tasks(task)[0],
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
        sandbox_retention=SandboxRetentionConfig(solver="keep", verifier="keep"),
    )
    handles = [outcome.handle for outcome in result.record.sandboxes if outcome.handle]
    try:
        assert result.verdict.status is Status.TASK_ERROR
        assert result.verdict.failure is not None
        assert result.verdict.failure.phase == "verify"
        assert [outcome.outcome for outcome in result.record.sandboxes] == [
            "retained",
            "retained",
        ]
        retained = await list_retained()
        assert set(handles) <= {item["handle"] for item in retained}
    finally:
        for handle in handles:
            await destroy_retained(handle)


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_sanitation_failure_destroys_sandbox_without_changing_reward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_exec = DockerSandbox.exec

    async def fail_sanitation(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        if list(argv[:3]) == ["rm", "-rf", "--"] and "/opt/ale/verify/config.json" in argv:
            return ExecResult(exit_code=1, stderr="simulated sanitation failure")
        return await original_exec(self, argv, **kwargs)

    monkeypatch.setattr(DockerSandbox, "exec", fail_sanitation)
    task = scaffold_task(tmp_path / "sanitation-failure")
    result = await run_episode(
        load_tasks(task)[0],
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
        sandbox_retention=SandboxRetentionConfig(solver="keep", verifier="destroy"),
    )

    assert result.verdict.status is Status.COMPLETED
    assert result.verdict.rewards == {"content": 1.0, "overall": 1.0}
    assert result.record.sandboxes[0].outcome == "retention-failed"
    assert "sanitation" in (result.record.sandboxes[0].reason or "")
    assert not any(item["episode"] == result.episode_id for item in await list_retained())
