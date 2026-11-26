"""Allocation-local isolation and stdin/stdout between the login and compute workers."""

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orbit.run.providers.batch import bridge, worker


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


def test_worker_keeps_only_assigned_gpu_ids(monkeypatch):
    monkeypatch.setenv("PBS_JOBID", "100")
    monkeypatch.setenv("ORBIT_BATCH_STEP_GPUS", "GPU-b")
    context = worker.Batch({"scheduler": "pbs", "gpus": 2})
    assert context.devices([{"id": "GPU-a"}, {"id": "GPU-b"}]) == [{"id": "GPU-b"}]


def test_allocation_control_stream_is_bidirectional(tmp_path):
    (tmp_path / "stop").touch()
    source = tmp_path / "bridge.py"
    shutil.copyfile(bridge.__file__, source)
    (tmp_path / "worker.py").write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'ready\\x00')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(b'node error stream')\n"
        "while data := sys.stdin.buffer.read(65536):\n"
        "    sys.stdout.buffer.write(data)\n"
        "sys.exit(19)\n"
    )
    server = subprocess.Popen([sys.executable, "-I", str(source), "listen"], cwd=tmp_path)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "sessions").is_dir():
            assert time.monotonic() < deadline and server.poll() is None
            time.sleep(0.01)
        with subprocess.Popen(
            [sys.executable, "-I", str(source), "connect", str(tmp_path), "exec", "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as client:
            assert client.stdout.read(6) == b"ready\x00"
            payload = os.urandom(180_000)
            stdout, stderr = client.communicate(payload, timeout=15)
            assert (stdout, stderr, client.returncode) == (payload, b"node error stream", 19)
    finally:
        (tmp_path / "stop").write_text("stop")
        server.wait(timeout=8)
    assert list((tmp_path / "sessions").iterdir()) == []


def test_killed_jobs_store_is_reclaimed_after_its_lock_drops(tmp_path, monkeypatch):
    monkeypatch.setenv("PBS_JOBID", "43.flow")
    context = worker.Batch({"scheduler": "pbs", "gpus": 0})
    store = tmp_path / "orbit-pbs-random"
    store.mkdir()
    context.claim(store / "job")
    os.utime(store, (10, 10))
    assert context.abandoned(tmp_path) == []
    context.store_owner.close()
    assert context.abandoned(tmp_path) == [store]


def test_cancelled_control_stream_reaps_children_after_command_leader_exits(tmp_path):
    (tmp_path / "stop").touch()
    source = tmp_path / "bridge.py"
    shutil.copyfile(bridge.__file__, source)
    (tmp_path / "worker.py").write_text(
        "import subprocess\nprint(subprocess.Popen(['sleep', '60']).pid, flush=True)\n"
    )
    server = subprocess.Popen([sys.executable, "-I", str(source), "listen"], cwd=tmp_path)
    child = 0
    try:
        end = time.monotonic() + 5
        while not (tmp_path / "sessions").is_dir():
            assert time.monotonic() < end and server.poll() is None
            time.sleep(0.01)
        with subprocess.Popen(
            [sys.executable, "-I", str(source), "connect", str(tmp_path), "exec", "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        ) as client:
            child = int(client.stdout.readline())
            client.terminate()
            client.wait(timeout=4)
        end = time.monotonic() + 6
        status = Path(f"/proc/{child}/stat")
        while status.is_file():
            try:
                if status.read_text().rpartition(")")[2].split()[0] == "Z":
                    break
            except FileNotFoundError:
                break
            assert time.monotonic() < end, "orphaned allocation command"
            time.sleep(0.05)
        child = 0
    finally:
        if child:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)
        (tmp_path / "stop").write_text("stop")
        server.wait(timeout=8)


def test_site_proxy_credentials_stay_with_pulls(monkeypatch, capsys):
    for key in tuple(os.environ):
        if key.lower().endswith("_proxy"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HTTP_PROXY", "http://flow-proxy:8080")
    monkeypatch.setenv("NO_PROXY", "localhost,.example.org,10.0.0.0/8")
    assert worker.build_proxy_flag() == "--http-proxy=true"
    monkeypatch.setenv("HTTPS_PROXY", "http://private:fake-test-only@flow-proxy:8080")
    assert worker.build_proxy_flag() == "--http-proxy=false"
    diagnostic = capsys.readouterr().err
    assert "HTTPS_PROXY" in diagnostic and "fake-test-only" not in diagnostic


def test_compute_node_defaults_cannot_inject_scheduler_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PBS_JOBID", "4.pbs")
    monkeypatch.setenv("CONTAINERS_CONF_OVERRIDE", "/site/containers.conf")
    monkeypatch.delenv("ORBIT_BATCH_SITE_CONTAINERS_CONF", raising=False)
    context = worker.Batch({"scheduler": "pbs", "gpus": 0})
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
