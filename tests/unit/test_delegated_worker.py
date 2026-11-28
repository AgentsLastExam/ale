"""The worker's PBS and LSF adapter: each Sandbox held to its share of the job."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orbit.run.providers.cluster import bridge
from orbit.run.providers.cluster import delegated_worker as worker

pytestmark = pytest.mark.unit


def test_job_device_visibility(monkeypatch):
    cards = [{"id": "GPU-one"}, {"id": "GPU-two"}]
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    with pytest.raises(RuntimeError, match="NVML lists 2"):
        worker.allocated(cards, 1)
    assert worker.allocated(cards, 2) == cards
    assert worker.allocated(cards, 0) == []
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-two")
    assert worker.allocated(cards, 1) == [cards[1]]
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    monkeypatch.setattr(worker, "cuda_uuids", lambda: ["GPU-two"])
    assert worker.allocated(cards, 1) == [cards[1]]
    for mask in ("", "-1", "MIG-partition", "GPU-missing"):
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", mask)
        with pytest.raises(RuntimeError, match="whole GPU UUIDs"):
            worker.allocated([cards[0]], 1)


def test_image_architecture_and_layer_ownership(monkeypatch):
    monkeypatch.setattr(worker.platform, "machine", lambda: "aarch64")
    assert worker.native("arm64") == "linux/arm64"
    with pytest.raises(RuntimeError, match="native linux/amd64"):
        worker.native("x86_64")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "0 1000 1\n")
    with pytest.raises(RuntimeError, match="subordinate UID"):
        worker.subids(["podman"])


def test_job_without_delegation_does_not_start_steps(tmp_path):
    (tmp_path / "cgroup.subtree_control").touch()
    (tmp_path / "cgroup.controllers").write_text("cpu pids")
    with pytest.raises(RuntimeError, match="unavailable controllers: memory"):
        worker.Allocation(tmp_path, []).open()
    assert not list(tmp_path.glob("orbit-*"))


def test_the_worker_holds_sandboxes_in_the_subtree_orbit_gave_it(tmp_path, monkeypatch):
    """Never in the cgroup it finds itself in, which is Orbit's own."""
    monkeypatch.setenv("PBS_JOBID", "5.server")
    opened = []
    monkeypatch.setattr(worker.Allocation, "open", lambda self: opened.append(self.group))
    with pytest.raises(RuntimeError, match="no cgroup"):
        worker.Delegated({"workload_manager": "pbs", "job_variable": "PBS_JOBID", "gpus": 0}).open(
            []
        )
    worker.Delegated(
        {"workload_manager": "pbs", "job_variable": "PBS_JOBID", "gpus": 0, "cgroup": str(tmp_path)}
    ).open([])
    assert opened == [tmp_path]


def test_worker_keeps_only_assigned_gpu_ids(monkeypatch):
    monkeypatch.setenv("LSB_JOBID", "100")
    monkeypatch.setenv("ORBIT_BATCH_STEP_GPUS", "GPU-b")
    context = worker.Delegated({"workload_manager": "lsf", "job_variable": "LSB_JOBID", "gpus": 2})
    assert context.devices([{"id": "GPU-a"}, {"id": "GPU-b"}]) == [{"id": "GPU-b"}]


def test_killed_jobs_store_is_reclaimed_after_its_lock_drops(tmp_path, monkeypatch):
    monkeypatch.setenv("PBS_JOBID", "43.flow")
    context = worker.Delegated({"workload_manager": "pbs", "job_variable": "PBS_JOBID", "gpus": 0})
    store = tmp_path / "orbit-pbs-random"
    store.mkdir()
    context.claim(store / "job")
    os.utime(store, (10, 10))
    assert context.abandoned(tmp_path) == []
    context.store_owner.close()
    assert context.abandoned(tmp_path) == [store]


def test_compute_node_defaults_cannot_inject_scheduler_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PBS_JOBID", "4.pbs")
    monkeypatch.setenv("CONTAINERS_CONF_OVERRIDE", "/site/containers.conf")
    monkeypatch.delenv("ORBIT_BATCH_SITE_CONTAINERS_CONF", raising=False)
    context = worker.Delegated({"workload_manager": "pbs", "job_variable": "PBS_JOBID", "gpus": 0})
    context.claim(tmp_path / "job")
    try:
        assert context.podman_argv(str(tmp_path)) == ["podman", "--module", "/site/containers.conf"]
        path = Path(os.environ["CONTAINERS_CONF_OVERRIDE"])
        assert path.read_text().splitlines() == [
            "[containers]",
            "env=[{append=false}]",
            "env_host=false",
            "http_proxy=false",
        ]
    finally:
        context.store_owner.close()


def test_indexed_worker_readiness_and_completion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORBIT_KUBERNETES_RANK", "1")
    monkeypatch.setenv("ORBIT_KUBERNETES_POD_IP", "10.42.2.17")
    peer = worker.KubernetesGroup(2, "worker-b")
    peer.publish()
    assert peer.nodes() == []
    monkeypatch.setenv("ORBIT_KUBERNETES_RANK", "0")
    monkeypatch.setenv("ORBIT_KUBERNETES_POD_IP", "10.42.1.17")
    leader = worker.KubernetesGroup(2, "worker-a")
    leader.publish()
    assert leader.nodes() == peer.nodes() == ["worker-a", "worker-b"]
    assert leader.address == "10.42.1.17" and peer.address == "10.42.2.17"
    box = tmp_path / "sandboxes" / "0"
    for rank in (0, 1):
        (box / "nodes" / str(rank)).mkdir(parents=True)
    leader.done(box, 0)
    assert leader.completion(box, 2) is None
    assert leader.completion(box, 1) == 0  # A single-node verifier uses rank zero only.
    (box / "step-1.log").write_text("could not attach assigned GPU")
    peer.done(box, 9)
    assert leader.completion(box, 2) == 9
    leader.fail(box / "error", "another rank of this Sandbox failed")
    assert (
        "rank 1 on worker-b: Sandbox step exited 9: could not attach assigned GPU"
        in (box / "error").read_text()
    )


def test_indexed_worker_refuses_incorrect_placement(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORBIT_KUBERNETES_POD_IP", "10.42.0.1")
    for index in range(2):
        monkeypatch.setenv("ORBIT_KUBERNETES_RANK", str(index))
        group = worker.KubernetesGroup(2, "same-machine")
        group.publish()
    with pytest.raises(RuntimeError, match="multiple allocation ranks on one node"):
        group.nodes()
    monkeypatch.setenv("ORBIT_KUBERNETES_RANK", "2")
    with pytest.raises(RuntimeError, match="outside allocation"):
        worker.KubernetesGroup(2, "other-machine")


def test_multinode_bridge_routes_each_session_to_its_target(tmp_path):
    (tmp_path / "stop").touch()
    script = tmp_path / "bridge.py"
    shutil.copyfile(bridge.__file__, script)
    (tmp_path / "worker.py").write_text("import os\nprint(os.environ['TEST_NODE'])\n")
    peers = []
    try:
        for name in ("worker-a", "worker-b"):
            endpoint = tmp_path / "workers" / name
            endpoint.mkdir(parents=True)
            proc = subprocess.Popen(
                [sys.executable, "-I", str(script), "listen", str(endpoint)],
                env={**os.environ, "TEST_NODE": name},
                cwd=tmp_path,
            )
            peers.append(proc)
            deadline = time.monotonic() + 5
            while not (endpoint / "sessions").is_dir():
                assert time.monotonic() < deadline and proc.poll() is None
                time.sleep(0.01)
        for name in ("worker-b", "worker-a"):
            target = tmp_path / "workers" / name
            result = subprocess.run(
                [sys.executable, "-I", str(script), "connect", str(target), "exec", "0"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            assert result.stdout.strip() == name
    finally:
        (tmp_path / "stop").write_text("stop")
        for proc in peers:
            proc.wait(timeout=8)


def test_kubernetes_driver_override_requires_exact_upstream_abi_pair(monkeypatch):
    from orbit.run.providers.cluster import worker as runtime

    monkeypatch.setattr(runtime, "this_node", lambda: "worker-b")
    assert runtime.nvproxy_abi("570.211.01", ["570.195.03"], kubernetes=True) == "570.195.03"
    assert runtime.nvproxy_abi("570.211.01", ["570.211.01", "570.195.03"], kubernetes=True) is None
    for driver, supported, kubernetes in (
        ("570.211.01", ["570.195.03"], False),
        ("570.211.02", ["570.195.03"], True),
        ("570.211.01", ["570.195.04"], True),
    ):
        with pytest.raises(RuntimeError, match=f"{driver} of worker-b is unsupported"):
            runtime.nvproxy_abi(driver, supported, kubernetes=kubernetes)


def test_kubernetes_peer_mtu_and_router_policy(monkeypatch):
    import json
    from types import SimpleNamespace

    from orbit.run.providers.cluster import worker as runtime

    monkeypatch.setattr(runtime, "delegated", SimpleNamespace(group=object()))
    monkeypatch.setattr(
        runtime,
        "output",
        lambda *args: json.dumps(
            [
                {"mtu": 65536, "addr_info": [{"local": "127.0.0.1"}]},
                {"mtu": 1450, "addr_info": [{"local": "10.42.1.19"}]},
            ]
        ),
    )
    assert runtime.peer_mtu("10.42.1.19") == 1370
    with pytest.raises(RuntimeError, match="no IPv4 interface"):
        runtime.peer_mtu("10.42.1.20")
    calls = []

    def run(argv, **kwargs):
        calls.append(" ".join(argv))

    monkeypatch.setattr(runtime.subprocess, "run", run)
    runtime.group(dict(router=10, sandbox=20, rank=0, mtu=1320, peers=[]))
    router = "nsenter -t 10 -n "
    assert router + "ip rule add iif peers-router lookup 100 pref 100" in calls
    assert router + "ip rule add lookup local pref 200" in calls
    assert router + "ip rule del lookup local pref 0" in calls
    assert router + "ip link set wg0 mtu 1320 up" in calls
    assert (
        router + "tc filter replace dev peers-router ingress protocol ipv6 matchall action drop"
        in calls
    )
