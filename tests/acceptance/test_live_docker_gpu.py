from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from orbit.core.capability import GpuHold
from orbit.core.config import RunConfig, SandboxRetentionConfig
from orbit.core.errors import ProviderCapacityError
from orbit.core.harness import EffectiveAgentResources
from orbit.core.lock import TaskSource
from orbit.core.sandbox import PreparedTaskImage, SandboxRequest
from orbit.core.taskspec import NetworkPolicy, Resources
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.harnesses.builtin import OracleHarness
from orbit.run.process import run
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from orbit.run.providers.docker import DockerProvider, DockerSandbox
from orbit.run.scaffold import scaffold_task
from orbit.run.tasksets.manifest import load_tasks
from tests.support import (
    CONTAINER_BASE,
    edit_task_config,
    prepare_reference,
    provider_registry,
    skip_without_gvisor,
)

pytestmark = [
    pytest.mark.needs_docker,
    pytest.mark.needs_gpu,
    pytest.mark.skipif(
        os.environ.get("ORBIT_TEST_DOCKER_GPU") != "1",
        reason="set ORBIT_TEST_DOCKER_GPU=1 on a qualified NVIDIA Docker host",
    ),
]


def _gpu_image_reference() -> str:
    image = os.environ.get("ORBIT_TEST_DOCKER_GPU_IMAGE")
    tag = os.environ.get("ORBIT_TEST_DOCKER_GPU_TAG")
    if image is None and tag is None:
        return CONTAINER_BASE
    return f"{image or CONTAINER_BASE.rsplit(':', 1)[0]}:{tag or 'latest'}"


def _linker_cache_names(listing: str) -> set[str]:
    return {line.split()[0] for line in listing.splitlines() if " => " in line}


@pytest.mark.asyncio
async def test_live_docker_gpu_operation_and_exclusive_lease() -> None:
    index = int(os.environ.get("ORBIT_TEST_DOCKER_GPU_INDEX", "0"))
    provider = DockerProvider(gpus=(index,))
    prepared = await prepare_reference(provider, _gpu_image_reference())
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
        with pytest.raises(ProviderCapacityError, match="holds it whole"):
            await contender.create(request.model_copy(update={"episode_id": "gpu-contender"}))
    finally:
        await sandbox.destroy()


@pytest.mark.asyncio
async def test_live_docker_gpu_sandbox_has_rendering_drivers() -> None:
    host = subprocess.run(["/sbin/ldconfig", "-p"], check=True, capture_output=True, text=True)
    if "libEGL_nvidia.so.0" not in _linker_cache_names(host.stdout):
        pytest.skip("the Host driver has no graphics libraries")
    index = int(os.environ.get("ORBIT_TEST_DOCKER_GPU_INDEX", "0"))
    provider = DockerProvider(gpus=(index,))
    prepared = await prepare_reference(provider, _gpu_image_reference())
    sandbox = await provider.create(
        SandboxRequest(
            episode_id="live-docker-gpu-graphics",
            prepared_image=prepared,
            resources=Resources(gpus=1),
            network=NetworkPolicy(),
        )
    )
    try:
        listing = await sandbox.exec(["/sbin/ldconfig", "-p"])
        assert listing.ok, listing.stderr
        names = _linker_cache_names(listing.stdout)
        # On a runtime-hook Host these appear only with the graphics capability; a CDI Host
        # has them whatever is requested.
        for soname in ("libcuda.so.1", "libEGL_nvidia.so.0", "libGLX_nvidia.so.0"):
            assert soname in names, f"{soname} is not in the GPU sandbox's linker cache"
        assert any(name.startswith("libnvidia-rtcore.so") for name in names)
    finally:
        await sandbox.destroy()


@pytest.mark.asyncio
async def test_live_docker_gpu_two_devices_in_one_sandbox() -> None:
    indices = _gpu_indices()
    if len(indices) < 2:
        pytest.skip("two physical NVIDIA GPUs are required")
    provider = DockerProvider(gpus=indices[:2])
    prepared = await prepare_reference(provider, _gpu_image_reference())
    sandbox = await provider.create(
        SandboxRequest(
            episode_id="live-docker-two-gpus",
            prepared_image=prepared,
            resources=Resources(gpus=2),
            network=NetworkPolicy(),
        )
    )
    try:
        allocation = sandbox.allocation.gpu
        assert allocation is not None
        assert len(allocation.observed_devices) == 2
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
    edit_task_config(
        task,
        {
            "artifacts": ["/home/user/output/solver-gpu.txt"],
            "task": {"name": "agentslastexam/gpu-separate"},
            "environment": {"memory_mb": 1024, "gpus": 1},
            "verifier": {"environment": {"cpus": 1, "memory_mb": 1024, "gpus": 1}},
        },
    )
    (task / "solution/solve.sh").write_text(
        "#!/bin/sh\nset -eu\n"
        "nvidia-smi --query-gpu=uuid --format=csv,noheader > "
        "/home/user/output/solver-gpu.txt\n"
    )
    (task / "tests/verify.py").write_text(
        "import subprocess\n"
        "from orbit_verify import write\n"
        "solver = open('/home/user/output/solver-gpu.txt').read().strip()\n"
        "verifier = subprocess.check_output(['nvidia-smi', '--query-gpu=uu"
        "id', '--format=csv,noheader'], text=True).strip()\n"
        "v = {}\n"
        "v['disjoint'] = float(solver != verifier)\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    return task


async def _run_separate_gpu(task: Path, run_dir: Path, indices: tuple[int, ...]):  # type: ignore[no-untyped-def]
    harness = OracleHarness()
    settings = RunConfig()
    return await run_episode(
        load_tasks(task)[0],
        EpisodeProtocol(harness),
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
            await DockerProvider().destroy_retained(outcome.handle)


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
        assert "holds it whole" in result.verdict.failure.message
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
        assert result.verdict.rewards == {"reward": 1.0}
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


#: Run in a GPU Sandbox: creates a CUDA context on its first GPU, allocates and sets each of
#: ``argv[2:]`` MiB in turn, printing how each went, and holds them ``argv[1]`` seconds.
_CUDA = r"""
import ctypes, sys, time
cu = ctypes.CDLL("libcuda.so.1")
def name(code):
    text = ctypes.c_char_p()
    cu.cuGetErrorName(code, ctypes.byref(text))
    return text.value.decode()
device, context = ctypes.c_int(), ctypes.c_void_p()
assert cu.cuInit(0) == 0 and cu.cuDeviceGet(ctypes.byref(device), 0) == 0
assert cu.cuCtxCreate_v2(ctypes.byref(context), 0, device) == 0
held = []
for mib in map(int, sys.argv[2:]):
    pointer = ctypes.c_uint64()
    code = cu.cuMemAlloc_v2(ctypes.byref(pointer), ctypes.c_size_t(mib << 20))
    if code == 0:
        assert cu.cuMemsetD8_v2(pointer, ctypes.c_ubyte(0x5A), ctypes.c_size_t(mib << 20)) == 0
        held.append(pointer)
    print(f"cuMemAlloc {mib} MiB: {name(code)}", flush=True)
open("/tmp/orbit-cuda-held", "w").close()
time.sleep(float(sys.argv[1]))
"""


def _shared(index: int, memory_mb: int | None, episode: str) -> SandboxRequest:
    return SandboxRequest(
        episode_id=episode,
        prepared_image=_PREPARED[index],
        resources=Resources(cpus=2, memory_mb=4096, gpus=1, gpu_memory_mb=memory_mb),
        network=NetworkPolicy(),
    )


#: The image each test prepared for the GPU it uses.
_PREPARED: dict[int, PreparedTaskImage] = {}


async def _provider(runtime: str) -> tuple[DockerProvider, int]:
    if runtime == "gvisor":
        skip_without_gvisor()
    index = int(os.environ.get("ORBIT_TEST_DOCKER_GPU_INDEX", "0"))
    provider = DockerProvider(gpus=(index,), runtime=runtime)
    _PREPARED[index] = await prepare_reference(provider, _gpu_image_reference())
    return provider, index


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["runc", "gvisor"])
async def test_live_docker_gpu_is_shared_by_memory_and_refused_past_its_headroom(
    runtime: str,
) -> None:
    """Two Sandboxes share the GPU, each reserving RESERVED MiB and using USED of it, and a
    third is sized to be admitted only when what they use is subtracted from what they
    reserved: with F free at the start and the default margin M, headroom is F - 2 RESERVED
    when their use is subtracted, and F - 2 RESERVED - 2 (USED + a context) when it is not, so
    the third, needing F - 2 RESERVED - M - USED / 2, fits the first way with 4 GiB to spare
    and misses the second by about 13 GiB. Under gVisor that needs each Sentry mapped to its
    container. A fourth like it is then refused for headroom, and one that wants the GPU whole
    because the GPU is shared."""
    provider, index = await _provider(runtime)
    margin = provider.gpu_memory_margin_mb
    free = (await provider._host_gpus())[index].memory_free_mb
    assert free is not None
    third = free - 2 * _RESERVED - margin - _USED // 2
    print(f"GPU {index}: {free} MiB free; A and B reserve {_RESERVED} MiB and use {_USED}; "
          f"the third needs {third}", flush=True)  # fmt: skip
    sandboxes: list[DockerSandbox] = []
    holding: list[asyncio.Future[object]] = []
    try:
        for episode in ("live-shared-a", "live-shared-b"):
            sandbox = await provider.create(_shared(index, _RESERVED, episode))
            assert isinstance(sandbox, DockerSandbox)
            sandboxes.append(sandbox)
            gpu = sandbox.allocation.gpu
            assert gpu is not None and gpu.memory_mb == _RESERVED
            assert gpu.hold is (GpuHold.LIMIT if runtime == "gvisor" else GpuHold.WATCHED)
            print(f"{sandbox.container}: {gpu.model_dump_json()}", flush=True)
            code, shown, _ = await run(
                "docker", "inspect", "--format",
                "{{json .Config.Labels}} {{json .HostConfig.Annotations}}", sandbox.container,
                timeout=60,
            )  # fmt: skip
            assert code == 0
            print(f"{sandbox.container} labels and annotations: {shown.strip()}", flush=True)
            labels, annotations = (json.loads(part) for part in shown.split(" ", 1))
            assert labels["orbit.gpu_memory_mb"] == str(_RESERVED)
            limit = (annotations or {}).get("dev.gvisor.flag.nvproxy-gpu-memory-limit")
            assert limit == (str(_RESERVED << 20) if runtime == "gvisor" else None)
            holding.append(
                asyncio.ensure_future(
                    sandbox.exec(["python3", "-c", _CUDA, "600", str(_USED)], timeout_sec=900)
                )
            )
            # The command holds the session it runs on; the file session is free.
            for _ in range(240):
                if await sandbox.list_files("/tmp/orbit-cuda-held"):
                    break
                await asyncio.sleep(0.5)
            else:
                raise AssertionError(f"{sandbox.container} never held its {_USED} MiB")
        rows = await run(
            "nvidia-smi", "--query-compute-apps=pid,used_memory,gpu_uuid",
            "--format=csv,noheader,nounits", timeout=60,
        )  # fmt: skip
        gpu_id = sandboxes[0].allocation.gpu.provider_addresses[0]  # type: ignore[union-attr]
        print("rows on the GPU:", [row for row in rows[1].splitlines() if gpu_id in row])
        admitted = await provider.create(_shared(index, third, "live-shared-c"))
        sandboxes.append(admitted)  # type: ignore[arg-type]
        print(f"third, needing {third} MiB: admitted", flush=True)
        with pytest.raises(ProviderCapacityError, match="of headroom") as refused:
            await provider.create(_shared(index, third, "live-shared-d"))
        print(f"fourth, shared: {refused.value}", flush=True)
        with pytest.raises(ProviderCapacityError, match="share it") as refused:
            await provider.create(_shared(index, None, "live-shared-whole"))
        print(f"fifth, whole: {refused.value}", flush=True)
    finally:
        for future in holding:
            future.cancel()
        for sandbox in sandboxes:
            await sandbox.destroy()


#: What each of the first two shared Sandboxes reserves and allocates, in MiB.
_RESERVED = 20000
_USED = 8192


@pytest.mark.asyncio
async def test_live_gvisor_sandbox_allocating_past_its_share_gets_out_of_memory_and_goes_on() -> (
    None
):
    provider, index = await _provider("gvisor")
    sandbox = await provider.create(_shared(index, 2048, "live-gvisor-limit"))
    try:
        # Its context takes 516 MiB of the 2048.
        result = await sandbox.exec(
            ["python3", "-c", _CUDA, "0", "1024", "1024", "256"], timeout_sec=300
        )
        print(result.stdout + result.stderr, flush=True)
        assert result.ok, result.stderr
        assert result.stdout.splitlines() == [
            "cuMemAlloc 1024 MiB: CUDA_SUCCESS",
            "cuMemAlloc 1024 MiB: CUDA_ERROR_OUT_OF_MEMORY",
            "cuMemAlloc 256 MiB: CUDA_SUCCESS",
        ]
        await asyncio.sleep(12)  # the watchdog looks, and finds nothing to act on
        assert (await sandbox.exec(["true"])).ok
    finally:
        await sandbox.destroy()


@pytest.mark.asyncio
async def test_live_runc_sandbox_past_its_share_loses_that_process_and_goes_on() -> None:
    """As under the OOM killer: the command whose process went past the share returns killed,
    the Sandbox and its other processes go on, and it is watched still, so a second overrun
    loses its process too."""
    provider, index = await _provider("runc")
    sandbox = await provider.create(_shared(index, 2048, "live-runc-watched"))
    assert isinstance(sandbox, DockerSandbox)
    try:
        assert (await sandbox.exec(["sh", "-c", "setsid sleep 600 >/dev/null 2>&1 &"])).ok
        for attempt in (1, 2):
            started = asyncio.get_running_loop().time()
            result = await sandbox.exec(["python3", "-c", _CUDA, "120", "4096"], timeout_sec=300)
            took = asyncio.get_running_loop().time() - started
            print(f"overrun {attempt}: exit {result.exit_code} after {took:.1f} s", flush=True)
            assert result.exit_code == -9 and took < 60, result
            code, alive, _ = await run(
                "docker", "exec", sandbox.container, "sh", "-c",
                # Its own sleep by its command line: the desktop's loops start sleeps too.
                "pgrep -fx 'sleep 600'; pgrep -f 'cuMem[A]lloc' || echo no-cuda-process",
                timeout=60,
            )  # fmt: skip
            print(f"in {sandbox.container} afterwards: {alive.split()}", flush=True)
            assert code == 0 and alive.split()[-1] == "no-cuda-process" and len(alive.split()) == 2
        assert (await sandbox.exec(["true"])).ok
    finally:
        await sandbox.destroy()
