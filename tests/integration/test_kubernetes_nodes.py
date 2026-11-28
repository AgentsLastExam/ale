"""Two Kubernetes nodes per solver and CPU snapshot verifiers of one or two nodes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from orbit.core.config import KubernetesProviderConfig, load_run_config
from orbit.core.result import ResultRecord
from orbit.core.verdict import Status
from tests.support import write_task

pytestmark = [pytest.mark.integration, pytest.mark.needs_kubernetes]

_GPU_PROBE = """\
import ctypes as c
import json
import os
import subprocess
import uuid
from pathlib import Path

driver = c.CDLL("libcuda.so.1")
assert Path("/proc/1/comm").read_text().strip() == "sleep"
def call(name, *args):
    code = getattr(driver, name)(*args)
    assert code == 0, (name, code)
call("cuInit", 0)
count = c.c_int()
call("cuDeviceGetCount", c.byref(count))
assert count.value == 1, count.value
device = c.c_int()
uid = (c.c_ubyte * 16)()
call("cuDeviceGet", c.byref(device), 0)
call("cuDeviceGetUuid_v2", c.byref(uid), device)
observed = "GPU-" + str(uuid.UUID(bytes=bytes(uid)))
nvml = subprocess.check_output(
    ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"], text=True
).split()
assert nvml == [observed], (nvml, observed)
rank = os.environ.get("ORBIT_NODE_RANK")
output = Path(f"/shared/rank-{rank}.json") if rank is not None else Path.home() / "uuid"
output.write_text(json.dumps(observed))
"""

_GLOO_PROBE = """\
import datetime
import json
import os
from pathlib import Path
import torch
import torch.distributed as dist

rank = int(os.environ["ORBIT_NODE_RANK"])
assert not list(Path("/dev").glob("nvidia[0-9]*"))
assert Path("/proc/1/comm").read_text().strip() == "sleep"
dist.init_process_group("gloo", init_method="tcp://node0:29400", rank=rank,
                        world_size=2, timeout=datetime.timedelta(seconds=60))
value = torch.full((32768,), rank + 1, dtype=torch.int32)
dist.all_reduce(value)
assert torch.all(value == 3).item()
(Path.home() / "solver-rank").write_text(str(rank))
Path(f"/shared/rank-{rank}.json").write_text(json.dumps(value[0].item()))
dist.destroy_process_group()
"""

_VERIFY = """\
exec "$ORBIT_VERIFY_PYTHON" - <<'PYTHON'
import json
from pathlib import Path
from orbit_verify import write

assert not list(Path("/dev").glob("nvidia[0-9]*")), "CPU verifier sees a GPU"
shared = Path("/shared")
ranks = [json.loads((shared / f"rank-{r}.json").read_text()) for r in range(2)]
try:
    (shared / "verifier-write").touch()
except OSError:
    pass
else:
    raise AssertionError("snapshot verifier can write to the solver's shared directory")
write({"reward": 1.0, "ranks": ranks})
PYTHON
"""


@pytest.fixture
def cluster_lab() -> Iterator[tuple[Path, KubernetesProviderConfig, Path]]:
    path = os.getenv("ORBIT_TEST_KUBERNETES_CONFIG")
    if not path:
        pytest.skip("set ORBIT_TEST_KUBERNETES_CONFIG to a Kubernetes Run TOML")
    config_path = Path(path).resolve()
    config = load_run_config(run_path=config_path)
    site = config.sandbox.kubernetes
    assert config.sandbox.providers == ("kubernetes",) and site
    # The controller and episode Pod must see the same Task and result directory.
    with TemporaryDirectory(dir=site.shared_fs_dir, prefix="kubernetes-test-") as shared:
        yield config_path, site, Path(shared)


def run_job(config_path: Path, shared: Path, task: Path) -> tuple[ResultRecord, Path]:
    runs = shared / "runs"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "orbit.run.cli",
            "run",
            str(task),
            "--config",
            str(config_path),
            "--agent",
            "oracle",
            "--runs-dir",
            str(runs),
            "--run-id",
            "qualification",
            "--episode-retries",
            "0",
        ],
        cwd=shared,
        capture_output=True,
        text=True,
        check=False,
    )
    result_paths = list((runs / "qualification").glob("*/result.json"))
    assert result_paths, completed.stdout + completed.stderr
    (result_path,) = result_paths
    result = ResultRecord.model_validate_json(result_path.read_text())
    assert result.status is Status.COMPLETED, result.failure
    assert completed.returncode == 0, completed.stderr
    assert result.rewards == {"reward": 1.0}
    assert all(item.outcome == "destroyed" for item in result.sandboxes)
    return result, result_path.parent


@pytest.mark.parametrize(
    "gpu", [False, pytest.param(True, marks=pytest.mark.needs_gpu)], ids=["gloo", "gpu-visibility"]
)
def test_two_node_solver_and_snapshot_verifier(cluster_lab, gpu: bool) -> None:
    config_path, site, shared_dir = cluster_lab
    if not any(p.max_nodes >= 2 and p.max_gpus >= int(gpu) for p in site.partitions):
        pytest.skip("the configured pool must include two eligible nodes")
    # Debian's CPU-only PyTorch installs its Gloo module for the system Python.
    install = (
        ""
        if gpu
        else (
            "RUN apt-get update && apt-get install -y --no-install-recommends python3-torch"
            " && rm -rf /var/lib/apt/lists/*\n"
        )
    )
    python = "python3" if gpu else "/usr/bin/python3"
    task_dir = write_task(
        shared_dir / "two-nodes",
        name="orbit/kubernetes-nodes",
        dockerfile="FROM python:3.12-slim-bookworm\n"
        + install
        + 'RUN useradd -m researcher\nCMD ["sleep", "infinity"]\n',
        config={
            "environment": {"nodes": 2, "cpus": 2, "memory_mb": 2048, "gpus": int(gpu)},
            "agent": {"user": "researcher", "timeout_sec": 240},
            "verifier": {
                "environment_mode": "snapshot",
                "environment": {"nodes": 1 if gpu else 2, "gpus": 0},
            },
        },
        solve=(
            '#!/bin/sh\nset -eu\ncp "$(dirname "$0")/probe.py" /shared/probe.py\n'
            f"ssh node1 {python} /shared/probe.py &\npeer=$!\n"
            f"{python} /shared/probe.py\nwait $peer\n"
        ),
        test="#!/bin/sh\nset -eu\n"
        + (
            'test -z "${ORBIT_NODES:-}"\n'
            if gpu
            else (
                'test "$ORBIT_NODES" = node0,node1\n'
                'test "$(cat /home/researcher/solver-rank)" = 0\n'
                'test "$(ssh node1 cat /home/researcher/solver-rank)" = 1\n'
            )
        )
        + _VERIFY,
    )
    (task_dir / "solution" / "probe.py").write_text(_GPU_PROBE if gpu else _GLOO_PROBE)
    result, episode = run_job(config_path, shared_dir, task_dir)
    allocation = result.segments[0].allocation
    assert allocation.provider == "kubernetes" and allocation.runtime == "runsc"
    assert len(set(allocation.nodes)) == 2
    ranks = json.loads((episode / "verification.json").read_text())["ranks"]
    if gpu:
        assert allocation.gpu and len(set(ranks)) == 2
        assert tuple(ranks) == allocation.gpu.provider_addresses
    else:
        assert allocation.gpu is None and ranks == [3, 3]


def test_one_node_solver_and_snapshot_verifier(cluster_lab) -> None:
    config_path, _, shared = cluster_lab
    gpus = int(os.environ.get("ORBIT_TEST_BATCH_GPUS", "0"))
    assert gpus in (0, 1), "this visibility test uses zero or one GPU"
    task = write_task(
        shared / "one-node",
        name="orbit/kubernetes-single",
        dockerfile=(
            'FROM python:3.12-slim-bookworm\nRUN useradd -m researcher\nCMD ["sleep", "infinity"]\n'
        ),
        config={
            "environment": {"cpus": 2, "memory_mb": 1024, "gpus": gpus},
            "agent": {"user": "researcher"},
            "verifier": {"environment_mode": "snapshot", "environment": {"gpus": 0}},
        },
        solve="#!/bin/sh\nset -eu\n"
        + ('python3 "$(dirname "$0")/probe.py"\n' if gpus else "")
        + 'echo solved > "$HOME/answer"\n',
        test="""#!/bin/sh
set -eu
exec "$ORBIT_VERIFY_PYTHON" - <<'PYTHON'
import json
from pathlib import Path
from orbit_verify import write
assert not list(Path("/dev").glob("nvidia[0-9]*"))
home = Path("/home/researcher")
assert (home / "answer").read_text().strip() == "solved"
uid = home / "uuid"
write({"reward": 1.0, "uuid": json.loads(uid.read_text()) if uid.exists() else None})
PYTHON
""",
    )
    if gpus:
        (task / "solution" / "probe.py").write_text(_GPU_PROBE)
    result, episode = run_job(config_path, shared, task)
    allocation = result.segments[0].allocation
    assert allocation.provider == "kubernetes" and allocation.runtime == "runsc"
    observed = json.loads((episode / "verification.json").read_text())["uuid"]
    if gpus:
        assert allocation.gpu and allocation.gpu.provider_addresses == (observed,)
    else:
        assert observed is None and allocation.gpu is None


async def test_queued_job_is_cancelled_after_a_lost_receipt(cluster_lab) -> None:
    from orbit.run.providers.cluster.kubernetes import KubernetesJob

    _, site, _ = cluster_lab
    pool = site.partitions[0]
    # This test can cancel a queued Job without reserving a compute node.
    pool = pool.model_copy(
        update={"node_selector": {**pool.node_selector, "orbit.run/test-node": uuid4().hex}}
    )
    job = KubernetesJob(site, pool)
    job.stage({})
    try:
        await job.submit(cpus=1, memory_mb=256, gpus=0, minutes=2, argv=["true"])
        assert job.id and await job.state() == "PENDING"
        (job.directory / "job-id").unlink()
        job.id = ""
    finally:
        await job.stop(cancel=True)
    assert not job.directory.exists()
    assert await job.state() == ""


async def test_rank_failure_releases_the_job_and_its_pods(cluster_lab, monkeypatch) -> None:
    from orbit.run.providers.cluster import kubernetes

    _, site, _ = cluster_lab
    pool = next(
        (pool for pool in site.partitions if pool.max_nodes >= 2 and not pool.min_gpus), None
    )
    if pool is None:
        pytest.skip("this test needs two eligible nodes for a CPU-only Job")
    job = kubernetes.KubernetesJob(site, pool)
    job.stage({})
    # The test peer is an executable on the PVC. It exits only on our marker, so the
    # namespaced controller can exercise rank loss without permission to kill arbitrary Pods.
    peer = job.directory / "test-peer"
    peer.write_text(
        '#!/bin/sh\nset -eu\necho "$ORBIT_KUBERNETES_NODE" > peer-node\n'
        "while ! test -f fail-peer; do sleep .2; done\nexit 42\n"
    )
    peer.chmod(0o700)

    async def peer_python(config):
        return peer

    monkeypatch.setattr(kubernetes, "engine", peer_python)
    try:
        await job.submit(
            cpus=1,
            memory_mb=128,
            gpus=0,
            minutes=5,
            nodes=2,
            argv=["sh", "-ec", 'echo "$ORBIT_KUBERNETES_NODE" > leader-node; sleep 240'],
        )
        files = [job.directory / path for path in ("leader-node", "peer-node")]
        async with asyncio.timeout(site.queue_timeout_sec + 60):
            while not all(path.exists() for path in files):
                assert await job.state() in {"PENDING", "RUNNING"}, await job.ended()
                await asyncio.sleep(0.5)
        assert len({path.read_text().strip() for path in files}) == 2
        # A failed episode may leave an open allocation tree on the PVC.
        orphan = job.directory / "allocations" / "orbit-incomplete"
        orphan.mkdir()
        (orphan / "stop").touch()
        (job.directory / "fail-peer").touch()
        async with asyncio.timeout(120):
            while await job.state():
                await asyncio.sleep(0.5)
        assert await job.ended() == "FAILED"
    finally:
        await job.stop(cancel=True)
    assert not job.directory.exists()
    assert not orphan.exists()
    assert await job.state() == ""
    assert not await job._pods(job.id, include_orphaned=True)
