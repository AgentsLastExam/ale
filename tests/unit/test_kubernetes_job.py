"""The Kubernetes API objects and ownership rules for a single Orbit allocation."""

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orbit.core.config import KubernetesProviderConfig, KubernetesQueue
from orbit.core.errors import ProviderStartError
from orbit.run.providers.cluster import kubernetes as kube
from orbit.run.providers.cluster import worker
from orbit.run.providers.cluster.job import CurrentAllocation

pytestmark = pytest.mark.unit


def job(tmp_path, *, nodes=1):
    queue = KubernetesQueue(
        name="gpu",
        max_nodes=nodes,
        max_cpus=4,
        max_memory_mb=8192,
        max_gpus=2,
        gpu_model={"name": "NVIDIA H100"},
        node_selector={"pool": "gpu"},
    )
    config = KubernetesProviderConfig(
        namespace="orbit-test",
        context="research",
        shared_claim="worker-data",
        kubeconfig="/config/client",
        worker_image="registry/worker:1",
        shared_fs_dir=str(tmp_path / "shared"),
        node_local_dir="/scratch",
        runtime_class="nested",
        node_selector={"site": "west"},
        partitions=(queue,),
    )
    return kube.KubernetesJob(config, queue)


@pytest.fixture
def allocation(tmp_path, monkeypatch):
    # A controller may be tested by root in CI, even when workers use a dedicated UID.
    monkeypatch.setattr(kube.os, "getuid", lambda: 1001)
    monkeypatch.setattr(kube.os, "getgid", lambda: 1002)
    instance = job(tmp_path)
    instance.stage({"nodes": 1})
    return instance


def accepted(manifest):
    return {**manifest, "metadata": {**manifest["metadata"], "uid": "job-uid"}}


@pytest.mark.parametrize("cards", [0, 2])
async def test_worker_job_matches_the_request_and_carries_no_controller_secrets(
    allocation, monkeypatch, cards
):
    monkeypatch.setenv("ORBIT_MODEL_API_KEY", "private-controller-secret")

    async def run(*argv, timeout, stdin, env):
        assert "ORBIT_MODEL_API_KEY" not in env
        if "auth" in argv or "pvc" in argv:
            return 0, "yes", ""
        assert argv == (
            "kubectl",
            "--namespace",
            "orbit-test",
            "--request-timeout=20s",
            "--context",
            "research",
            "--kubeconfig",
            "/config/client",
            "create",
            "-f",
            "-",
            "-o",
            "json",
        )
        obj = json.loads(stdin)
        assert obj["metadata"]["name"] == allocation.name
        assert (obj["kind"], obj["apiVersion"]) == ("Job", "batch/v1")
        assert obj["spec"]["activeDeadlineSeconds"] == 1800 + allocation.config.queue_timeout_sec
        assert obj["spec"]["backoffLimit"] == 0
        pod = obj["spec"]["template"]["spec"]
        assert pod["restartPolicy"] == "Never"
        assert pod["activeDeadlineSeconds"] == 1800
        assert not pod["automountServiceAccountToken"] and not pod["enableServiceLinks"]
        assert pod["securityContext"] == {
            "runAsNonRoot": True,
            "runAsUser": 1001,
            "runAsGroup": 1002,
        }
        assert pod["runtimeClassName"] == "nested"
        assert pod["nodeSelector"] == {
            "kubernetes.io/os": "linux",
            "kubernetes.io/arch": "amd64",
            "site": "west",
            "pool": "gpu",
        }
        assert pod["volumes"] == [
            {"name": "cache", "persistentVolumeClaim": {"claimName": "worker-data"}},
            {"name": "scratch", "emptyDir": {}},
        ]
        (worker,) = pod["containers"]
        assert worker["workingDir"] == str(allocation.directory)
        assert worker["volumeMounts"] == [
            {"name": "cache", "mountPath": str(allocation.directory.parent)},
            {"name": "scratch", "mountPath": "/scratch"},
        ]
        assert worker["args"][-4:] == ["python3", "-I", "worker.py", "prepare"]
        assert all(
            v in worker["env"]
            for v in [
                {"name": "ORBIT_KUBERNETES_JOB", "value": allocation.name},
                {
                    "name": "ORBIT_KUBERNETES_NODE",
                    "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}},
                },
            ]
        )
        resources = {"cpu": "4", "memory": "2048Mi"}
        if cards:
            resources["nvidia.com/gpu"] = str(cards)
        assert worker["resources"] == {"requests": resources, "limits": resources}
        assert "private-controller-secret" not in stdin.decode()
        return 0, json.dumps(accepted(obj)), ""

    monkeypatch.setattr(kube, "run", run)
    await allocation.submit(
        cpus=4,
        memory_mb=2048,
        gpus=cards,
        minutes=30,
        argv=["python3", "-I", "worker.py", "prepare"],
    )
    assert (allocation.directory / "job-id").read_text() == allocation.id == "job-uid"
    with pytest.raises(ProviderStartError, match="already submitted"):
        await allocation.submit(cpus=1, memory_mb=256, gpus=0, minutes=2, argv=["true"])


async def test_preflight_requires_the_volume_and_the_controller_verbs(allocation, monkeypatch):
    calls = []

    async def run(*argv, **kwargs):
        args = argv[argv.index("/config/client") + 1 :]
        calls.append(args)
        if args == ("auth", "can-i", "list", "pods"):
            return 1, "no", ""
        return 0, "yes", ""

    monkeypatch.setattr(kube, "run", run)
    with pytest.raises(ProviderStartError, match=r"can-i list pods.*orbit-test: no"):
        await kube.KubernetesJob.preflight(allocation.config, [allocation.partition])
    assert calls[0] == ("get", "pvc", "worker-data", "-o", "name")
    assert ("auth", "can-i", "delete", "jobs.batch") in calls
    calls.clear()
    with pytest.raises(ProviderStartError, match="can-i list pods"):
        await allocation.submit(cpus=1, memory_mb=1024, gpus=0, minutes=5, argv=["true"])
    assert ("create", "-f", "-", "-o", "json") not in calls
    assert not (allocation.directory / "submitting").exists()


async def test_lost_create_reply_and_completed_job_are_recovered_then_deleted(
    allocation, monkeypatch
):
    resource = None
    deletes = []

    async def kubectl(config, *args, body=None):
        nonlocal resource
        if args[0] == "auth" or args[:2] == ("get", "pvc"):
            return "yes"
        if args[0] == "create":
            resource = accepted(body)
            resource["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
            raise ProviderStartError("client lost its reply")
        if args[0] == "delete":
            assert args[2].endswith("/namespaces/orbit-test/jobs/" + allocation.name)
            deletes.append(body)
            resource = None
            return ""
        if args[:2] == ("get", "pods"):
            return '{"items":[]}'
        assert args[:3] == ("get", "job", allocation.name)
        return json.dumps(resource) if resource else ""

    async def sleep(delay):
        return None

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    monkeypatch.setattr(kube.asyncio, "sleep", sleep)
    await allocation.submit(cpus=1, memory_mb=1024, gpus=0, minutes=15, argv=["true"])
    assert await allocation.ended() == "COMPLETED"
    monkeypatch.setenv("ORBIT_KUBERNETES_JOB_DIRECTORY", str(allocation.directory))
    current = CurrentAllocation(allocation.id, ("node-0",), 1, 1024, 0, None)
    child = kube.KubernetesAllocation(allocation.config, current)
    assert child.directory.parent == allocation.directory / "allocations"
    child.directory.mkdir()
    monkeypatch.setattr(worker, "delegated", child)
    with monkeypatch.context() as local:
        local.chdir(child.directory)
        assert worker.cache_dir() == allocation.directory.parent
    allocation.id = ""  # A new controller uses the persisted UID.
    await allocation.stop()
    assert deletes == [
        {
            "kind": "DeleteOptions",
            "apiVersion": "v1",
            "preconditions": {"uid": "job-uid"},
            "propagationPolicy": "Foreground",
        }
    ]
    assert not allocation.directory.exists()
    assert not child.directory.exists()
    await allocation.stop()


@pytest.mark.parametrize("foreign", ["uid", "owner"])
async def test_stop_refuses_to_delete_a_same_named_replacement(allocation, monkeypatch, foreign):
    (allocation.directory / "submitting").write_text("ours")
    (allocation.directory / "job-id").write_text("initial-uid")
    replacement = {
        "kind": "Job",
        "metadata": {
            "namespace": "orbit-test",
            "name": allocation.name,
            "uid": "new-uid" if foreign == "uid" else "initial-uid",
            "labels": {"orbit.run/allocation": "foreign" if foreign == "owner" else "ours"},
        },
    }

    async def kubectl(config, *args, **kwargs):
        assert args[0] == "get"
        return json.dumps(replacement)

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    with pytest.raises(ProviderStartError, match="different owner or UID"):
        await allocation.stop(cancel=True)
    assert allocation.directory.exists()


async def test_cancellation_waits_for_the_submission_receipt(allocation, monkeypatch):
    entered, accepted_event = asyncio.Event(), asyncio.Event()

    async def kubectl(config, *args, body=None):
        if args[0] == "auth" or args[:2] == ("get", "pvc"):
            return "yes"
        entered.set()
        await accepted_event.wait()
        return json.dumps(accepted(body))

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    submit = asyncio.create_task(
        allocation.submit(cpus=1, memory_mb=256, gpus=0, minutes=1, argv=["true"])
    )
    await entered.wait()
    submit.cancel()
    accepted_event.set()
    with pytest.raises(asyncio.CancelledError):
        await submit
    assert (allocation.directory / "job-id").read_text() == "job-uid"


async def test_pod_and_deadline_report_running_pending_failure_and_oom(allocation, monkeypatch):
    (allocation.directory / "submitting").write_text("intent")
    allocation.id = "stored-uid"
    resource = {
        "kind": "Job",
        "metadata": {
            "name": allocation.name,
            "namespace": "orbit-test",
            "labels": {"orbit.run/allocation": "intent"},
            "uid": allocation.id,
        },
        "spec": {"activeDeadlineSeconds": 1000},
        "status": {
            "active": 1,
            "startTime": (datetime.now(UTC) - timedelta(seconds=11)).isoformat(),
        },
    }
    conditions = [{"type": "PodScheduled", "status": "False", "message": "node selector mismatch"}]
    pod = {
        "metadata": {"ownerReferences": [{"controller": True, "uid": allocation.id}]},
        "status": {"phase": "Pending", "conditions": conditions},
    }
    foreign = {
        "metadata": {"ownerReferences": [{"controller": True, "uid": "another"}]},
        "status": {"phase": "Running"},
    }

    async def kubectl(config, *args, **kwargs):
        if args[:2] == ("get", "job"):
            return json.dumps(resource)
        assert args[:4] == ("get", "pods", "-l", "batch.kubernetes.io/controller-uid=stored-uid")
        return json.dumps({"items": [foreign, pod]})

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    assert await allocation.state() == "PENDING"
    assert await allocation.pending_reason() == "node selector mismatch"
    pod["status"].update(phase="Running", startTime=datetime.now(UTC).isoformat())
    pod["spec"] = {"activeDeadlineSeconds": 1100, "nodeName": "machine-0"}
    pod["metadata"]["uid"] = "pod-0"
    assert await allocation.state() == "RUNNING"
    placement = json.loads((allocation.directory / "placement.json").read_text())
    assert placement["nodes"] == ["machine-0"]
    assert 980 < placement["end"] - datetime.now(UTC).timestamp() <= 990
    pod["status"]["containerStatuses"] = [
        {
            "name": "worker",
            "state": {"waiting": {"reason": "ImagePullBackOff", "message": "bad tag"}},
        }
    ]
    with pytest.raises(ProviderStartError, match="ImagePullBackOff: bad tag"):
        await allocation.state()
    pod["status"]["phase"] = "Failed"
    pod["status"]["containerStatuses"][0]["state"] = {"terminated": {"reason": "OOMKilled"}}
    assert await allocation.ended() == "OUT_OF_MEMORY"
    pod["status"]["containerStatuses"].clear()
    pod["status"]["reason"] = "DeadlineExceeded"
    assert await allocation.ended() == "TIMEOUT"
    resource["status"]["conditions"] = [
        {"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}
    ]
    assert await allocation.state() == ""
    assert await allocation.ended() == "TIMEOUT"


async def test_stop_preserves_recovery_state_if_api_is_unreachable(allocation, monkeypatch):
    (allocation.directory / "submitting").write_text("ours")

    async def kubectl(*args):
        raise ProviderStartError("cannot reach API")

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    with pytest.raises(ProviderStartError, match="cannot reach API"):
        await allocation.stop(cancel=True)
    assert (allocation.directory / "stop").read_text() == "stop"


async def test_pool_capacity_and_root_worker_are_checked_before_submission(allocation, monkeypatch):
    with pytest.raises(ProviderStartError, match="supports at most 1 nodes"):
        await allocation.submit(cpus=1, memory_mb=1024, minutes=10, gpus=0, nodes=2, argv=["true"])
    monkeypatch.setattr(kube.os, "getuid", lambda: 0)
    with pytest.raises(ProviderStartError, match="non-root"):
        await allocation.submit(cpus=1, memory_mb=1024, minutes=10, gpus=0, argv=["true"])
    assert not (allocation.directory / "submitting").exists()


async def test_transient_api_read_is_retried_and_permanent_denial_is_reported(
    allocation, monkeypatch
):
    attempts = []

    async def run(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            return 1, "", "error: connection reset by peer"
        if len(attempts) == 2:
            return 0, "job", ""
        return 1, "", "Error from server (Forbidden): permission denied"

    async def sleep(delay):
        pass

    monkeypatch.setattr(kube, "run", run)
    monkeypatch.setattr(kube.asyncio, "sleep", sleep)
    assert await kube._kubectl(allocation.config, "get", "job", allocation.name) == "job"
    with pytest.raises(ProviderStartError, match="permission denied"):
        await kube._kubectl(allocation.config, "get", "job", allocation.name)
    assert len(attempts) == 3


@pytest.mark.parametrize(
    "admission,immediate",
    [
        ("pods is forbidden: violates PodSecurity", True),
        ("pods is forbidden: exceeded quota: namespace-quota", False),
    ],
)
async def test_failed_admission_without_a_pod_reports_the_kubernetes_event(
    allocation, monkeypatch, admission, immediate
):
    (allocation.directory / "submitting").write_text("intent")
    allocation.id = "job-uid"
    resource = {
        "kind": "Job",
        "metadata": {
            "uid": "job-uid",
            "name": allocation.name,
            "namespace": "orbit-test",
            "labels": {"orbit.run/allocation": "intent"},
        },
    }
    events = []

    async def kubectl(config, *args):
        if args[:2] == ("get", "job"):
            return json.dumps(resource)
        if args[:2] == ("get", "pods"):
            return '{"items":[]}'
        events.append(args)
        assert any("involvedObject.uid=job-uid" in arg for arg in args)
        return json.dumps(
            {"items": [{"message": admission, "lastTimestamp": "2026-01-02T00:00:00Z"}]}
        )

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    if immediate:
        with pytest.raises(ProviderStartError, match="violates PodSecurity"):
            await allocation.state()
    else:
        assert await allocation.state() == "PENDING"
    assert await allocation.pending_reason() == admission
    assert len(events) == 1


def test_duplicate_pod_cannot_enter_the_same_shared_allocation(allocation):
    (allocation.directory / "worker.py").write_text("with open('runs','a') as f: f.write('x')")
    manifest = allocation._manifest(1, 256, 0, 2, ["python3", "worker.py"], "nonce")
    argv = manifest["spec"]["template"]["spec"]["containers"][0]["args"]
    (allocation.directory / "placement.json").write_text('{"nodes": ["host"]}')
    subprocess.run(argv, cwd=allocation.directory, check=True, timeout=5)
    duplicate = subprocess.run(argv, cwd=allocation.directory, capture_output=True)
    assert duplicate.returncode and (allocation.directory / "runs").read_text() == "x"


def test_missing_control_volume_exits_nonzero_and_names_the_paths(allocation):
    manifest = allocation._manifest(1, 256, 0, 2, ["python3", "worker.py"], "nonce")
    worker = manifest["spec"]["template"]["spec"]["containers"][0]
    assert worker["terminationMessagePolicy"] == "FallbackToLogsOnError"
    argv = worker["args"].copy()
    message = allocation.directory / "termination-log"
    argv[2] = argv[2].replace("/dev/termination-log", str(message))
    (allocation.directory / "stop").unlink()
    process = subprocess.run(argv, cwd=allocation.directory, capture_output=True)
    assert process.returncode and "shared_claim" in message.read_text()
    assert str(allocation.directory / "stop") in message.read_text()
    assert not (allocation.directory / "worker-started").exists()


@pytest.mark.parametrize("job_failed", [True, False])
async def test_pod_startup_error_reaches_controller_before_or_after_job_failure(
    allocation, monkeypatch, job_failed
):
    (allocation.directory / "submitting").write_text("intent")
    allocation.id = "job-uid"
    resource = {
        "kind": "Job",
        "metadata": {
            "name": allocation.name,
            "namespace": "orbit-test",
            "uid": "job-uid",
            "labels": {"orbit.run/allocation": "intent"},
        },
        "status": {"conditions": [{"type": "Failed", "status": "True"}] if job_failed else []},
    }
    pod = {
        "metadata": {"ownerReferences": [{"controller": True, "uid": "job-uid"}]},
        "status": {
            "phase": "Failed",
            "containerStatuses": [
                {
                    "name": "worker",
                    "state": {
                        "terminated": {
                            "exitCode": 1,
                            "message": "cannot read /data/test/stop. Check shared_claim",
                        }
                    },
                }
            ],
        },
    }

    async def kubectl(config, *args):
        return json.dumps(resource if args[:2] == ("get", "job") else {"items": [pod]})

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    with pytest.raises(ProviderStartError, match="cannot read /data/test/stop"):
        await allocation.state()


async def test_an_externally_orphaned_live_worker_keeps_its_control_files(allocation, monkeypatch):
    (allocation.directory / "submitting").write_text("intent")
    (allocation.directory / "job-id").write_text("original-uid")
    phases = iter(("Running", "Succeeded"))

    async def kubectl(config, *args):
        if args[:2] == ("get", "job"):
            return ""
        assert "batch.kubernetes.io/controller-uid=original-uid" in args
        assert (allocation.directory / "stop").read_text() == "stop"
        return json.dumps(
            {
                "items": [
                    {
                        "metadata": {"labels": {"orbit.run/allocation": "intent"}},
                        "status": {"phase": next(phases)},
                    }
                ]
            }
        )

    async def sleep(delay):
        assert allocation.directory.exists()

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    monkeypatch.setattr(kube.asyncio, "sleep", sleep)
    await allocation.stop()
    assert not allocation.directory.exists()


async def test_never_submitted_job_cleans_locally_without_api_access(allocation, monkeypatch):
    async def kubectl(*args, **kwargs):
        pytest.fail("a local preparation directory has no Kubernetes objects")

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    await allocation.stop()
    assert not allocation.directory.exists()


@pytest.fixture
def multi(tmp_path, monkeypatch):
    monkeypatch.setattr(kube.os, "getuid", lambda: 1001)
    monkeypatch.setattr(kube.os, "getgid", lambda: 1002)

    async def engine(config):
        return Path("/engine/bin/python3")

    monkeypatch.setattr(kube, "engine", engine)
    instance = job(tmp_path, nodes=2)
    instance.stage({"nodes": 2})
    return instance


async def test_distributed_job_allocates_whole_gpus_per_node_and_fences_each_rank(
    multi, monkeypatch
):
    manifests = []

    async def kubectl(config, *args, body=None):
        if args[:2] == ("get", "pvc"):
            assert args[-1] == "json"
            return json.dumps({"spec": {"accessModes": ["ReadWriteMany"]}})
        if args[0] == "auth":
            return "yes"
        assert args[0] == "create"
        manifests.append(body)
        return json.dumps(accepted(body))

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    await multi.submit(cpus=3, memory_mb=4096, gpus=2, minutes=20, nodes=2, argv=["true"])
    spec = manifests[0]["spec"]
    assert (spec["completionMode"], spec["completions"], spec["parallelism"]) == ("Indexed", 2, 2)
    pod = spec["template"]["spec"]
    (worker,) = pod["containers"]
    (anti,) = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert anti["topologyKey"] == "kubernetes.io/hostname"
    assert anti["labelSelector"]["matchLabels"] == spec["template"]["metadata"]["labels"]
    assert (
        anti["labelSelector"]["matchLabels"]["orbit.run/allocation"]
        == (multi.directory / "submitting").read_text()
    )
    assert (
        worker["resources"]["requests"]
        == worker["resources"]["limits"]
        == {"cpu": "3", "memory": "4096Mi", "nvidia.com/gpu": "2"}
    )
    fields = {
        entry["name"]: entry["valueFrom"]["fieldRef"]["fieldPath"]
        for entry in worker["env"]
        if "valueFrom" in entry
    }
    assert (
        fields["ORBIT_KUBERNETES_RANK"]
        == "metadata.annotations['batch.kubernetes.io/job-completion-index']"
    )
    assert fields["ORBIT_KUBERNETES_NODE"] == "spec.nodeName"
    assert fields["ORBIT_KUBERNETES_POD_IP"] == "status.podIP"


async def test_multi_node_job_checks_read_write_many_before_create(multi, monkeypatch):
    async def kubectl(config, *args, body=None):
        assert args == ("get", "pvc", "worker-data", "-o", "json")
        return json.dumps({"spec": {"accessModes": ["ReadWriteOnce"]}})

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    with pytest.raises(ProviderStartError, match=r"orbit-test/worker-data needs ReadWriteMany"):
        await multi.submit(cpus=2, memory_mb=512, gpus=0, minutes=10, nodes=2, argv=["true"])
    assert not (multi.directory / "submitting").exists()


@pytest.mark.parametrize("replacement", [False, True])
async def test_rank_order_queue_deadline_and_lost_worker(multi, monkeypatch, replacement):
    pods = []
    job = None
    deleted = []

    async def kubectl(config, *args, body=None):
        nonlocal job
        if args[:2] == ("get", "pvc"):
            return json.dumps({"spec": {"accessModes": ["ReadWriteMany"]}})
        if args[0] == "auth":
            return "yes"
        if args[0] == "create":
            job = accepted(body)
            job["status"] = {"startTime": datetime.now(UTC).isoformat()}
            return json.dumps(job)
        if args[:2] == ("get", "pods"):
            return json.dumps({"items": pods})
        if args[:2] == ("get", "events"):
            return '{"items":[]}'
        if args[:2] == ("get", "job"):
            return json.dumps(job) if job else ""
        assert args[0] == "delete"
        deleted.append(body)
        job = None
        pods.clear()
        return ""

    monkeypatch.setattr(kube, "_kubectl", kubectl)
    await multi.submit(cpus=1, memory_mb=512, gpus=1, minutes=10, nodes=2, argv=["true"])

    def pod(rank):
        return {
            "metadata": {
                "uid": f"worker-{rank}",
                "annotations": {"batch.kubernetes.io/job-completion-index": str(rank)},
                "ownerReferences": [{"uid": multi.id, "controller": True}],
            },
            "spec": {"nodeName": f"machine-{rank}", "activeDeadlineSeconds": 100 if rank else 600},
            "status": {"phase": "Running", "startTime": datetime.now(UTC).isoformat()},
        }

    pods.extend([pod(1), pod(0)])
    pods[0]["status"].update(phase="Pending", message="Insufficient nvidia.com/gpu")
    assert await multi.state() == "PENDING"
    assert await multi.pending_reason() == "rank 1: Insufficient nvidia.com/gpu"
    pods[0]["status"]["phase"] = "Running"
    assert await multi.state() == "RUNNING"
    placement = json.loads((multi.directory / "placement.json").read_text())
    assert placement["nodes"] == ["machine-0", "machine-1"]
    if replacement:
        pods[0]["metadata"]["uid"] = "replacement"
    else:
        pods.pop(0)
    assert await multi.state() == ""
    assert await multi.ended() == "PREEMPTED"

    # A partly lost allocation can still be cancelled with its original Job UID.
    async def sleep(delay):
        pass

    monkeypatch.setattr(kube.asyncio, "sleep", sleep)
    await multi.stop(cancel=True)
    assert deleted[0]["preconditions"] == {"uid": "job-uid"}
    assert not multi.directory.exists()


def test_continuation_deadline_begins_after_scheduling(allocation):
    (allocation.directory / "continuation.json").write_text("{}")
    spec = allocation._manifest(1, 256, 0, 2, ["true"], "nonce")["spec"]
    assert "activeDeadlineSeconds" not in spec
    assert spec["template"]["spec"]["activeDeadlineSeconds"] == 120
