from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ale.core.config import RunConfig, SandboxRetentionConfig
from ale.core.errors import ProviderCapabilityError
from ale.core.harness import EffectiveAgentResources
from ale.core.lock import TaskSource
from ale.core.sandbox import SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers.docker import DockerProvider, destroy_retained
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from tests.support import prepare_reference, provider_registry

pytestmark = [
    pytest.mark.needs_docker,
    pytest.mark.needs_gpu,
    pytest.mark.skipif(
        os.environ.get("ALE_TEST_DOCKER_GPU") != "1",
        reason="set ALE_TEST_DOCKER_GPU=1 on a qualified NVIDIA Docker host",
    ),
]


@pytest.mark.asyncio
async def test_live_docker_gpu_operation_and_exclusive_lease() -> None:
    index = int(os.environ.get("ALE_TEST_DOCKER_GPU_INDEX", "0"))
    provider = DockerProvider(gpus=(index,))
    reference = (
        f"{os.environ.get('ALE_TEST_DOCKER_GPU_IMAGE', 'ghcr.io/agentslastexam/sandbox-base-cli')}:"
        f"{os.environ.get('ALE_TEST_DOCKER_GPU_TAG', 'latest')}"
    )
    prepared = await prepare_reference(provider, reference)
    request = SandboxRequest(
        episode_id="live-docker-gpu",
        prepared_image=prepared,
        resources=Resources(gpus=1),
        network=NetworkPolicy(),
    )
    sandbox = await provider.create(request)
    try:
        allocation = sandbox.allocation.gpu
        assert allocation is not None
        assert len(allocation.observed_devices) == 1
        operation = await sandbox.exec(["nvidia-smi", "-L"])
        assert operation.ok, operation.stderr

        contender = DockerProvider(gpus=(index,))
        with pytest.raises(ProviderCapabilityError, match=r"available|candidates"):
            await contender.create(request.model_copy(update={"episode_id": "gpu-contender"}))
    finally:
        await sandbox.destroy()


def _gpu_indices() -> tuple[int, ...]:
    output = subprocess.run(
        ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader,nounits"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return tuple(int(line) for line in output.splitlines() if line.strip())


def _separate_gpu_task(root: Path) -> Path:
    task = scaffold_task(root)
    (task / "instruction.md").write_text("Record the assigned GPU.\n")
    (task / "task.yaml").write_text(
        "spec_type: core/v1\n"
        "name: gpu-separate\n"
        "image: {kind: container}\n"
        "resources: {cpus: 1, memory_mb: 1024, gpus: 1}\n"
        "artifacts: [/home/user/output/solver-gpu.txt]\n"
        "verify:\n"
        "  environment_mode: separate\n"
        "  resources: {cpus: 1, memory_mb: 1024, storage_mb: null, gpus: 1}\n"
    )
    (task / "oracle/run.sh").write_text(
        "#!/bin/sh\nset -eu\n"
        "nvidia-smi --query-gpu=uuid --format=csv,noheader > "
        "/home/user/output/solver-gpu.txt\n"
    )
    (task / "verify/verify.py").write_text(
        "import subprocess\n"
        "from ale_verify import CheckResult, Verification\n"
        "solver = open('/home/user/output/solver-gpu.txt').read().strip()\n"
        "verifier = subprocess.check_output(["
        "'nvidia-smi','--query-gpu=uuid','--format=csv,noheader'], text=True).strip()\n"
        "v = Verification()\n"
        "v.check('disjoint', CheckResult(float(solver != verifier)))\n"
        "v.write()\n"
    )
    return task


async def _run_separate_gpu(task: Path, run_dir: Path, indices: tuple[int, ...]):  # type: ignore[no-untyped-def]
    harness = OracleHarness()
    settings = RunConfig()
    return await run_episode(
        load_tasks(task)[0],
        StandardEnvironment(harness),
        provider_registry(DockerProvider(gpus=indices)),
        run_dir=run_dir,
        provenance=ProvenanceInputs(
            source=TaskSource(kind="local", path=str(task)),
            agent=agent_provenance(harness, "", settings, EffectiveAgentResources()),
            gateway=gateway_provenance(settings),
            config_hash=settings.config_hash,
        ),
        sandbox_retention=SandboxRetentionConfig(solver="keep", verifier="destroy"),
    )


async def _cleanup_retained(result) -> None:  # type: ignore[no-untyped-def]
    for outcome in result.record.sandboxes:
        if outcome.handle:
            await destroy_retained(outcome.handle)


@pytest.mark.asyncio
async def test_one_gpu_host_rejects_separate_verifier_while_solver_is_kept(
    tmp_path: Path,
) -> None:
    indices = _gpu_indices()
    result = await _run_separate_gpu(
        _separate_gpu_task(tmp_path / "gpu-separate"),
        tmp_path / "runs",
        indices[:1],
    )
    try:
        assert result.verdict.status is Status.ENV_ERROR
        assert result.verdict.failure is not None
        assert any(word in result.verdict.failure.message for word in ("available", "candidates"))
    finally:
        await _cleanup_retained(result)


@pytest.mark.asyncio
async def test_kept_solver_and_separate_verifier_receive_disjoint_gpus(tmp_path: Path) -> None:
    indices = _gpu_indices()
    if len(indices) < 2:
        pytest.skip("two physical NVIDIA GPUs are required")
    result = await _run_separate_gpu(
        _separate_gpu_task(tmp_path / "gpu-separate"),
        tmp_path / "runs",
        indices[:2],
    )
    try:
        assert result.verdict.status is Status.COMPLETED, result.verdict.failure
        assert result.verdict.rewards == {"disjoint": 1.0}
        assert result.lock is not None
        solver = result.lock.resources
        verifier = result.lock.verification.resources
        assert solver is not None and solver.effective.gpu is not None
        assert verifier is not None and verifier.effective.gpu is not None
        assert set(solver.effective.gpu.provider_addresses).isdisjoint(
            verifier.effective.gpu.provider_addresses
        )
    finally:
        await _cleanup_retained(result)
