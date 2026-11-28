"""The worker's PBS and LSF adapter: each Sandbox held to its share of the job."""

import os
import subprocess
from pathlib import Path

import pytest

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
