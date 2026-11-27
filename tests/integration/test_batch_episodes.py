"""Run a solver and its snapshot verifier in a PBS Pro or LSF job, as Orbit runs each episode.

Run inside a job of the site's (``tests.support.provider_in_job``). ORBIT_TEST_PBS_CONFIG or
ORBIT_TEST_LSF_CONFIG selects a Run TOML for the site. ORBIT_TEST_BATCH_GPUS optionally
requests whole GPUs in both Sandboxes, which the job must hold.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orbit.core.config import load_run_config
from orbit.core.verdict import Status
from orbit.run.episode import run_episode
from orbit.run.harnesses.builtin import OracleHarness
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.providers import ProviderRegistry
from orbit.run.tasksets.manifest import load_tasks
from tests.support import provider_in_job, write_task

pytestmark = pytest.mark.integration

# Exercise the driver on each visible device using the image's own Python; no CUDA SDK
# or ML framework is required. Even the zero-GPU case checks for a leaked device.
_DEVICES = """\
python3 - "$1" <<'PYTHON'
import ctypes as c
import sys
from pathlib import Path

count = int(sys.argv[1])
if count == 0:
    assert not list(Path("/dev").glob("nvidia[0-9]*")), "a CPU Sandbox sees a GPU"
    sys.exit()
cuda = c.CDLL("libcuda.so.1")
def call(name, *args):
    code = getattr(cuda, name)(*args)
    assert code == 0, (name, code)
call("cuInit", 0)
visible = c.c_int()
call("cuDeviceGetCount", c.byref(visible))
assert visible.value == count, (visible.value, count)
for ordinal in range(count):
    device, ctx, ptr = c.c_int(), c.c_void_p(), c.c_uint64()
    call("cuDeviceGet", c.byref(device), ordinal)
    call("cuDevicePrimaryCtxRetain", c.byref(ctx), device)
    call("cuCtxSetCurrent", ctx)
    call("cuMemAlloc_v2", c.byref(ptr), c.c_size_t(4))
    call("cuMemsetD32_v2", ptr, c.c_uint(42), c.c_size_t(1))
    result = c.c_uint()
    call("cuMemcpyDtoH_v2", c.byref(result), ptr, c.c_size_t(4))
    assert result.value == 42, (ordinal, result.value)
    call("cuMemFree_v2", ptr)
    call("cuDevicePrimaryCtxRelease_v2", device)
PYTHON
"""


@pytest.mark.parametrize(
    "scheduler",
    [
        pytest.param("pbs", marks=pytest.mark.needs_pbs),
        pytest.param("lsf", marks=pytest.mark.needs_lsf),
    ],
)
async def test_solver_and_snapshot_verifier_on_batch_cluster(
    scheduler: str, tmp_path: Path
) -> None:
    variable = f"ORBIT_TEST_{scheduler.upper()}_CONFIG"
    if not (path := os.environ.get(variable)):
        pytest.skip(f"set {variable} to a Run TOML for this login node")
    config = load_run_config(run_path=Path(path))
    assert config.sandbox.providers == (scheduler,), f"{variable} must select only {scheduler}"
    gpus = int(os.environ.get("ORBIT_TEST_BATCH_GPUS", "0"))
    assert gpus >= 0
    stage = f"#!/bin/sh\nset -eu\nset -- {gpus}\n" + _DEVICES
    task_dir = write_task(
        tmp_path / "batch-task",
        name="orbit/batch-smoke",
        dockerfile=(
            "FROM python:3.12-slim-bookworm\n"
            "RUN useradd --create-home researcher\n"
            'CMD ["sleep", "infinity"]\n'
        ),
        config={
            "environment": {"cpus": 2, "memory_mb": 2048, "gpus": gpus},
            "agent": {"user": "researcher"},
            "verifier": {"environment_mode": "snapshot", "environment": {"gpus": gpus}},
        },
        solve=stage + 'test "$(id -un)" = researcher\nprintf solved > "$ORBIT_HOME/answer"\n',
        test=stage
        + """\
exec "$ORBIT_VERIFY_PYTHON" - <<'PYTHON'
import os
from pathlib import Path
from orbit_verify import write
assert (Path(os.environ["ORBIT_HOME"]) / "answer").read_text() == "solved"
write({"reward": 1.0})
PYTHON
""",
    )
    (task,) = load_tasks(task_dir)
    registry = ProviderRegistry(config)
    provider = provider_in_job(config.sandbox.pbs if scheduler == "pbs" else config.sandbox.lsf)
    registry._instances[scheduler] = provider
    try:
        result = await run_episode(
            task,
            EpisodeProtocol(OracleHarness()),
            registry,
            run_dir=tmp_path / "runs",
            destroy_failed_sandboxes=True,
        )
    finally:
        await provider.close()
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    assert {role for item in result.record.sandboxes for role in item.roles} == {
        "solver",
        "verifier",
    }
    assert all(item.outcome == "destroyed" for item in result.record.sandboxes)
