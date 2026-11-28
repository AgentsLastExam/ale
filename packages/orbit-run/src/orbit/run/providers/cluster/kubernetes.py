"""A namespaced Job runs one worker Pod on each node in an allocation."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from orbit.core.config import ClusterPartition, KubernetesProviderConfig, KubernetesQueue
from orbit.core.errors import ProviderStartError
from orbit.run.images import acquire_lock
from orbit.run.process import run
from orbit.run.providers.cluster.delegated import HELPER, SPEC, DelegatedAllocation
from orbit.run.providers.cluster.engine import engine
from orbit.run.providers.cluster.job import (
    CLOSE_TIMEOUT_SEC,
    CurrentAllocation,
    Job,
    cache,
    exists,
    read,
    remove_job_directory,
    without_credentials,
    write,
)

_OWNER = "orbit.run/allocation"
_INDEX = "batch.kubernetes.io/job-completion-index"
# A separate nonce lets a second controller distinguish an accepted submission from
# an unrelated object of the same name, even when the first controller lost its reply.
_WORKER = (
    "if ! test -f stop; then "
    'message="Orbit worker cannot read $PWD/stop. '
    'Check shared_claim, shared_fs_dir and the worker UID."; '
    'echo "$message" >&2; echo "$message" > /dev/termination-log; exit 1; fi; '
    "test ! -s stop || exit 0; "
    "rank=${ORBIT_KUBERNETES_RANK-}; log=job.log; "
    'if test -n "$rank" && test "$rank" != 0; then log=job-"$rank".log; fi; '
    'mkdir "worker-started${rank:+-$rank}" || exit 1; '
    "while ! test -s placement.json; do test ! -s stop || exit 0; sleep 0.5; done; "
    'if test -n "$rank" && test "$rank" != 0; then '
    'exec "$ORBIT_KUBERNETES_PEER_PYTHON" -I -m orbit.run.providers.cluster.kubernetes_peer '
    '>>"$log" 2>&1; fi; '
    'exec "$@" >>"$log" 2>&1'
)


async def _kubectl(config: KubernetesProviderConfig, *args: str, body: dict | None = None) -> str:
    command = ["kubectl", "--namespace", config.namespace, "--request-timeout=20s"]
    if config.context:
        command += ["--context", config.context]
    if config.kubeconfig:
        command += ["--kubeconfig", str(Path(config.kubeconfig).expanduser())]
    for attempt in range(3):
        try:
            code, out, err = await run(
                *command,
                *args,
                timeout=30,
                stdin=json.dumps(body).encode() if body else None,
                env=without_credentials(),
            )
            if not code:
                return out.strip()
            detail = err.strip() or out.strip()
        except FileNotFoundError:
            raise ProviderStartError("Kubernetes needs kubectl on the controller") from None
        except ProviderStartError as error:
            detail = str(error)
        # Retry observations across short API outages. An ambiguous create or delete
        # is recovered by reading the server's object and checking its identity.
        transient = any(
            word in detail.lower()
            for word in (
                "timed out",
                "timeout",
                "connection reset",
                "connection refused",
                " eof",
                "serviceunavailable",
                "too many requests",
                "toomanyrequests",
                "server is currently unable",
            )
        )
        if args[0] != "get" or not transient or attempt == 2:
            raise ProviderStartError(
                f"kubectl {' '.join(args)} in namespace {config.namespace}: {detail}"
            )
        await asyncio.sleep(1 + attempt)


async def preflight(
    config: KubernetesProviderConfig, partitions: Sequence[KubernetesQueue], *, nodes: int = 1
) -> None:
    """Check the exact namespace, claim and API verbs a run needs, including diagnostics."""
    claim = await _kubectl(
        config, "get", "pvc", config.shared_claim, "-o", "json" if nodes > 1 else "name"
    )
    if nodes > 1 and "ReadWriteMany" not in _json(claim).get("spec", {}).get("accessModes", ()):
        raise ProviderStartError(
            f"Kubernetes PVC {config.namespace}/{config.shared_claim} needs ReadWriteMany "
            f"for {nodes} worker nodes"
        )
    for verb, resource in (
        ("create", "jobs.batch"),
        ("get", "jobs.batch"),
        ("delete", "jobs.batch"),
        ("list", "pods"),
    ):
        await _kubectl(config, "auth", "can-i", verb, resource)


def _json(text: str) -> dict:
    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError
    except ValueError as exc:
        raise ProviderStartError("kubectl returned an invalid Kubernetes API response") from exc
    return value


class KubernetesJob(Job):
    """One Pod per allocated node, with per-node limits and a private local image store."""

    spans_nodes = True
    helpers = (HELPER,)
    preflight = staticmethod(preflight)

    def __init__(self, config: KubernetesProviderConfig, partition: KubernetesQueue) -> None:
        super().__init__(config, partition)
        self.config = config
        self.partition = partition
        self._next_event_check = 0.0
        self._admission_message = ""
        self._ready_nodes: tuple[str, ...] = ()
        self._pod_uids: tuple[str, ...] = ()
        self._lost_worker = ""

    def _manifest(
        self,
        cpus: int,
        memory_mb: int,
        gpus: int,
        minutes: int,
        argv: Sequence[str],
        owner: str,
        nodes: int = 1,
        *,
        peer_python: Path | None = None,
    ) -> dict:
        uid, gid = os.getuid(), os.getgid()
        if uid == 0 or gid == 0:
            raise ProviderStartError("the Kubernetes worker requires a non-root controller UID/GID")
        limits = {"cpu": str(cpus), "memory": f"{memory_mb}Mi"}
        if gpus:
            limits["nvidia.com/gpu"] = str(gpus)
        labels = {_OWNER: owner}
        scratch = self.config.node_local_dir or "/tmp"
        pod = {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "activeDeadlineSeconds": minutes * 60,
            "nodeSelector": self.config.node_labels
            | self.config.node_selector
            | self.partition.node_selector,
            "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": gid},
            "volumes": [
                {"name": "cache", "persistentVolumeClaim": {"claimName": self.config.shared_claim}},
                {"name": "scratch", "emptyDir": {}},
            ],
            "containers": [
                {
                    "name": "worker",
                    "image": self.config.worker_image,
                    # The trusted worker image may provide an entrypoint for its runtime.
                    "args": ["sh", "-ec", _WORKER, "orbit-worker", *argv],
                    "workingDir": str(self.directory),
                    "terminationMessagePolicy": "FallbackToLogsOnError",
                    "env": [
                        {"name": "ORBIT_KUBERNETES_JOB", "value": self.name},
                        {"name": "ORBIT_KUBERNETES_QUEUE", "value": self.partition.name},
                        {"name": "ORBIT_KUBERNETES_JOB_DIRECTORY", "value": str(self.directory)},
                        {
                            "name": "ORBIT_KUBERNETES_NODE",
                            "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}},
                        },
                    ],
                    "resources": {"requests": limits, "limits": limits},
                    "volumeMounts": [
                        {"name": "cache", "mountPath": str(cache(self.config))},
                        {"name": "scratch", "mountPath": scratch},
                    ],
                }
            ],
        }
        if self.config.runtime_class:
            pod["runtimeClassName"] = self.config.runtime_class
        indexed = {}
        if nodes > 1:
            indexed = {"completionMode": "Indexed", "completions": nodes, "parallelism": nodes}
            pod["affinity"] = {
                "podAntiAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "labelSelector": {"matchLabels": labels},
                            "topologyKey": "kubernetes.io/hostname",
                        }
                    ]
                }
            }
            pod["containers"][0]["env"].extend(
                [
                    {"name": "ORBIT_KUBERNETES_PEER_PYTHON", "value": str(peer_python)},
                    {
                        "name": "ORBIT_KUBERNETES_RANK",
                        "valueFrom": {
                            "fieldRef": {"fieldPath": f"metadata.annotations['{_INDEX}']"}
                        },
                    },
                    {
                        "name": "ORBIT_KUBERNETES_POD_IP",
                        "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                    },
                ]
            )
        # A continuation may wait indefinitely for a new slot. Once it starts,
        # the per-Pod deadline still bounds the job's wall time.
        if not exists(self.directory / "continuation.json"):
            indexed["activeDeadlineSeconds"] = self.config.queue_timeout_sec + minutes * 60
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": self.name, "namespace": self.config.namespace, "labels": labels},
            "spec": {
                **indexed,
                "backoffLimit": 0,
                # Jobs are deleted explicitly. The TTL also catches an abandoned controller.
                "ttlSecondsAfterFinished": 86400,
                "template": {"metadata": {"labels": labels}, "spec": pod},
            },
        }

    def _identity(self, obj: dict, owner: str) -> str:
        meta = obj.get("metadata", {})
        uid = meta.get("uid")
        expected_uid = self.id or read(self.directory / "job-id")
        if (
            obj.get("kind") != "Job"
            or meta.get("name") != self.name
            or meta.get("namespace") != self.config.namespace
            or meta.get("labels", {}).get(_OWNER) != owner
            or not owner
            or not isinstance(uid, str)
            or not uid
            or (expected_uid and uid != expected_uid)
        ):
            raise ProviderStartError(f"Kubernetes Job {self.name} has a different owner or UID")
        return uid

    async def _get(self) -> dict | None:
        listing = await _kubectl(
            self.config, "get", "job", self.name, "--ignore-not-found", "-o", "json"
        )
        if not listing:
            return None
        obj = _json(listing)
        self._identity(obj, read(self.directory / "submitting"))
        return obj

    async def submit(
        self,
        *,
        cpus: int,
        memory_mb: int,
        gpus: int,
        minutes: int,
        nodes: int = 1,
        argv: Sequence[str],
    ) -> None:
        if not 1 <= nodes <= self.partition.max_nodes:
            raise ProviderStartError(
                f"Kubernetes pool {self.partition.name} supports at most "
                f"{self.partition.max_nodes} nodes, requested {nodes}"
            )
        gpus = max(gpus, self.partition.min_gpus)
        owner = uuid.uuid4().hex
        peer_python = await engine(self.config) if nodes > 1 else None
        manifest = self._manifest(
            cpus, memory_mb, gpus, minutes, argv, owner, nodes, peer_python=peer_python
        )
        spec = dict(
            workload_manager="kubernetes",
            job_variable="ORBIT_KUBERNETES_JOB",
            podman=self.config.podman,
            cpus=cpus,
            memory_mb=memory_mb,
            gpus=gpus,
        )
        if nodes > 1:
            spec["nodes"] = nodes
        with (self.directory / "submission").open("a") as lock:
            await acquire_lock(lock)
            if read(self.directory / "stop") or read(self.directory / "submitting"):
                raise ProviderStartError(f"job {self.name} was stopped or already submitted")
            # Image preparation can submit a build before the episode preflight.
            # Establish cleanup access before either path creates a resource.
            await preflight(self.config, (self.partition,), nodes=nodes)
            (self.directory / "allocations").mkdir(exist_ok=True, mode=0o700)
            write(self.directory / SPEC, json.dumps(spec).encode())
            write(self.directory / "submitting", owner.encode())
            pending = asyncio.create_task(self._create(manifest, owner))
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise

    async def _create(self, manifest: dict, owner: str) -> None:
        try:
            response = await _kubectl(self.config, "create", "-f", "-", "-o", "json", body=manifest)
        except ProviderStartError:
            # A timeout may lose the server's acknowledgement of a successful POST.
            obj = await self._get()
            if obj is None:
                raise
        else:
            obj = _json(response)
        self.id = self._identity(obj, owner)
        write(self.directory / "job-id", self.id.encode())

    async def _pods(self, uid: str, *, include_orphaned: bool = False) -> list[dict]:
        result = await _kubectl(
            self.config,
            "get",
            "pods",
            "-l",
            f"batch.kubernetes.io/controller-uid={uid}",
            "-o",
            "json",
        )
        items = _json(result).get("items")
        if not isinstance(items, list):
            raise ProviderStartError("kubectl returned an invalid Pod list")
        # An administrator's orphan deletion removes the owner reference but leaves
        # both the Job UID label selected above and Orbit's original submission label.
        owner = read(self.directory / "submitting")
        owned = []
        for pod in items:
            metadata = pod["metadata"]
            refs = metadata.get("ownerReferences", ())
            if any(ref.get("uid") == uid and ref.get("controller") is True for ref in refs) or (
                include_orphaned
                and not refs
                and owner
                and metadata.get("labels", {}).get(_OWNER) == owner
            ):
                owned.append(pod)
        return owned

    async def _workers(self, job: dict) -> dict[int, dict]:
        """Group owned Pods by completion index. A failed or duplicate rank cannot be replaced
        once it has created its local image store and Sandbox."""
        count = job.get("spec", {}).get("completions", 1)
        ranked = {}
        for pod in await self._pods(job["metadata"]["uid"]):
            rank = 0
            if count > 1:
                raw = pod["metadata"].get("annotations", {}).get(_INDEX, "")
                if (
                    not isinstance(raw, str)
                    or not raw.isascii()
                    or not raw.isdecimal()
                    or int(raw) >= count
                ):
                    raise ProviderStartError(
                        f"Kubernetes Job {self.name} has an invalid Pod index {raw!r}"
                    )
                rank = int(raw)
            if rank in ranked:
                raise ProviderStartError(
                    f"Kubernetes Job {self.name} has multiple workers for rank {rank}"
                )
            ranked[rank] = pod
        return ranked

    @staticmethod
    def _terminal(obj: dict) -> str:
        for condition in obj.get("status", {}).get("conditions", ()):
            if condition.get("status") != "True":
                continue
            if condition["type"] == "Complete":
                return "COMPLETED"
            if condition["type"] in {"Failed", "FailureTarget"}:
                return "TIMEOUT" if condition.get("reason") == "DeadlineExceeded" else "FAILED"
        return ""

    async def _admission(self) -> str:
        """A Job whose Pod was refused has no Pod status. Its controller records a Warning."""
        now = asyncio.get_running_loop().time()
        if now >= self._next_event_check:
            self._next_event_check = now + 10
            selector = f"involvedObject.uid={self.id},reason=FailedCreate"
            try:
                result = await _kubectl(
                    self.config, "get", "events", "--field-selector", selector, "-o", "json"
                )
            except ProviderStartError as error:
                # Reading Events is optional in tightly scoped namespaces.
                if "forbidden" not in str(error).lower():
                    raise
            else:
                events = _json(result).get("items", [])
                event = max(
                    events,
                    key=lambda e: e.get("lastTimestamp") or e["metadata"]["creationTimestamp"],
                    default={},
                )
                self._admission_message = event.get("message", "")
        return self._admission_message

    async def state(self) -> str:
        if self._lost_worker:
            return ""
        job = await self._get()
        if job is None:
            return ""
        terminal = self._terminal(job)
        if terminal in {"COMPLETED", "TIMEOUT"}:
            return ""
        if job["metadata"].get("deletionTimestamp"):
            return "COMPLETING"
        workers = await self._workers(job)
        count = job.get("spec", {}).get("completions", 1)
        for rank, pod in workers.items():
            status = pod.get("status", {})
            terminated = self._container_state(status).get("terminated", {})
            fence = "worker-started" if count == 1 else f"worker-started-{rank}"
            if (
                terminated.get("exitCode")
                and terminated.get("message")
                and not (self.directory / fence).exists()
            ):
                raise ProviderStartError(
                    f"Kubernetes Job {self.name} could not start its worker on rank {rank}: "
                    f"{terminated['message']}"
                )
            if self._ready_nodes and (
                pod["metadata"].get("deletionTimestamp")
                or pod["metadata"]["uid"] != self._pod_uids[rank]
            ):
                self._lost_worker = f"lost worker rank {rank} on {self._ready_nodes[rank]}"
                return ""
            if status.get("phase") in {"Failed", "Succeeded"}:
                return ""
            self._check_container(status, rank)
        if terminal:
            return ""
        if len(workers) < count:
            if self._ready_nodes:
                missing = sorted(set(range(count)) - workers.keys())
                self._lost_worker = f"lost worker ranks {missing}"
                return ""
            message = await self._admission()
            # Insufficient quota can become available as another Job finishes.
            if (
                message
                and "exceeded quota" not in message.lower()
                and any(w in message.lower() for w in ("forbidden", "denied", "is invalid"))
            ):
                raise ProviderStartError(
                    f"Kubernetes could not create the Pod for {self.name}: {message}"
                )
            return "PENDING"
        if all(pod.get("status", {}).get("phase") == "Running" for pod in workers.values()):
            if not self._ready_nodes:
                self._record_placement(job, workers)
            return "RUNNING"
        return "PENDING"

    def _record_placement(self, job: dict, workers: dict[int, dict]) -> None:
        """Give the episode the actual nodes and earliest admitted deadline, without
        exposing API credentials to a Pod. Once placed, each rank keeps its Pod UID."""
        ordered = [workers[rank] for rank in range(len(workers))]
        nodes = tuple(pod["spec"].get("nodeName", "") for pod in ordered)
        if not all(nodes) or len(set(nodes)) != len(nodes):
            raise ProviderStartError(
                f"Kubernetes Job {self.name} requires one distinct scheduled node per rank: {nodes}"
            )
        try:
            timed = [*ordered, job] if "activeDeadlineSeconds" in job["spec"] else ordered
            deadline = min(
                datetime.fromisoformat(obj["status"]["startTime"]).timestamp()
                + obj["spec"]["activeDeadlineSeconds"]
                for obj in timed
            )
        except (KeyError, ValueError, TypeError) as error:
            raise ProviderStartError(
                f"Kubernetes Job {self.name} has no active deadline"
            ) from error
        self._ready_nodes = nodes
        self._pod_uids = tuple(pod["metadata"]["uid"] for pod in ordered)
        write(
            self.directory / "placement.json",
            json.dumps({"nodes": nodes, "end": deadline}).encode(),
        )

    def _check_container(self, pod: dict, rank: int) -> None:
        waiting = self._container_state(pod).get("waiting", {})
        if waiting.get("reason") in {
            "ImagePullBackOff",
            "InvalidImageName",
            "CreateContainerConfigError",
            "CreateContainerError",
            "ErrImageNeverPull",
        }:
            raise ProviderStartError(
                f"Kubernetes Job {self.name} cannot start its worker on rank {rank}: "
                f"{waiting['reason']}: {waiting.get('message', '')}"
            )

    @staticmethod
    def _container_state(pod: dict) -> dict:
        return next(
            (c.get("state", {}) for c in pod.get("containerStatuses", ()) if c["name"] == "worker"),
            {},
        )

    async def pending_reason(self) -> str:
        job = await self._get()
        if not job:
            return "Kubernetes Job disappeared"
        workers = await self._workers(job)
        for rank, worker in sorted(workers.items()):
            status = worker.get("status", {})
            if status.get("phase") != "Running":
                reason = self._pod_reason(status)
                return (
                    f"rank {rank}: {reason}"
                    if job.get("spec", {}).get("completions", 1) > 1
                    else reason
                )
        return await self._admission() or "waiting for the Kubernetes worker Pod"

    def _pod_reason(self, pod: dict) -> str:
        if pod.get("message"):
            return pod["message"]
        ended = self._container_state(pod).get("terminated", {})
        if ended:
            return ended.get("message") or ended.get("reason", "")
        waiting = self._container_state(pod).get("waiting", {})
        if waiting:
            return waiting.get("message") or waiting.get("reason", "")
        return next(
            (
                c.get("message") or c.get("reason", "")
                for c in pod.get("conditions", ())
                if c.get("type") == "PodScheduled" and c.get("status") == "False"
            ),
            "waiting for the Kubernetes worker Pod",
        )

    async def ended(self) -> str:
        job = await self._get()
        if job is None:
            return ""
        outcome = self._terminal(job)
        if outcome in {"COMPLETED", "TIMEOUT"}:
            return outcome
        workers = await self._workers(job)
        for worker in workers.values():
            pod = worker.get("status", {})
            if self._container_state(pod).get("terminated", {}).get("reason") == "OOMKilled":
                return "OUT_OF_MEMORY"
            if pod.get("reason") == "DeadlineExceeded":
                return "TIMEOUT"
            if pod.get("reason") == "Evicted":
                return "PREEMPTED"
        if any(worker.get("status", {}).get("phase") == "Failed" for worker in workers.values()):
            return "FAILED"
        return outcome or ("PREEMPTED" if self._lost_worker else "")

    async def _delete(self, obj: dict) -> None:
        # kubectl's ordinary delete-by-name would also delete a concurrent replacement.
        path = f"/apis/batch/v1/namespaces/{self.config.namespace}/jobs/{self.name}"
        options = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "propagationPolicy": "Foreground",
            "preconditions": {"uid": self._identity(obj, read(self.directory / "submitting"))},
        }
        try:
            await _kubectl(self.config, "delete", "--raw", path, "-f", "-", body=options)
        except ProviderStartError:
            if await self._get() is not None:
                raise

    async def stop(self, *, cancel: bool = False) -> None:
        try:
            lock = (self.directory / "submission").open("a")
        except FileNotFoundError:
            return
        with lock:
            await acquire_lock(lock)
            write(self.directory / "stop", b"stop")
            self.id = self.id or read(self.directory / "job-id")
            job = await self._get() if read(self.directory / "submitting") else None
            if job and not self.id:
                self.id = job["metadata"]["uid"]
                write(self.directory / "job-id", self.id.encode())
        loop = asyncio.get_running_loop()
        start = loop.time()
        while job is not None:
            age = loop.time() - start
            if age >= 900:
                raise ProviderStartError(f"Kubernetes did not release Job {self.name}")
            if not job["metadata"].get("deletionTimestamp"):
                if cancel or self._terminal(job) or age >= 600:
                    await self._delete(job)
                else:
                    workers = await self._pods(self.id)
                    if len(workers) != job.get("spec", {}).get("completions", 1) or any(
                        p.get("status", {}).get("phase") not in {"Running", "Succeeded"}
                        for p in workers
                    ):
                        await self._delete(job)
            await asyncio.sleep(1)
            job = await self._get()
        if self.id:
            # Also protect the control directory when someone deleted the Job outside
            # Orbit with background or orphan propagation. The worker sees the stop marker.
            until = loop.time() + 120
            while any(
                p.get("status", {}).get("phase") not in {"Failed", "Succeeded"}
                for p in await self._pods(self.id, include_orphaned=True)
            ):
                if loop.time() >= until:
                    raise ProviderStartError(
                        f"Kubernetes still has a worker for Job {self.name}. "
                        f"Control files remain in {self.directory}"
                    )
                await asyncio.sleep(1)
        if failure := read(self.directory / "cleanup-error"):
            raise ProviderStartError(
                f"Kubernetes worker cleanup failed in {self.directory}: {failure}"
            )
        await remove_job_directory(self.directory)


class KubernetesAllocation(DelegatedAllocation):
    """The primary Pod runs Orbit and the local worker. The other Pods hold their own
    stores and run workers for the same allocation on the shared volume."""

    JOB_VARIABLE = "ORBIT_KUBERNETES_JOB"
    QUEUE_VARIABLE = "ORBIT_KUBERNETES_QUEUE"

    def __init__(self, config: KubernetesProviderConfig, current: CurrentAllocation) -> None:
        super().__init__(config, current)
        # Foreground Job deletion can now reclaim these files even if the engine's
        # Pod dies before it can close its allocation. Retained snapshots stay in the cache.
        self.directory = self.job_directory() / "allocations" / self.name

    @staticmethod
    def job_directory() -> Path:
        return Path(os.environ["ORBIT_KUBERNETES_JOB_DIRECTORY"])

    @classmethod
    def held(
        cls, partition: ClusterPartition, job: str, *, cpus: int, memory_mb: int, gpus: int
    ) -> tuple[tuple[str, ...], int, int, int]:
        assert isinstance(partition, KubernetesQueue)
        nodes = tuple(json.loads(read(cls.job_directory() / "placement.json"))["nodes"])
        if not nodes or nodes[0] != os.environ.get("ORBIT_KUBERNETES_NODE"):
            raise ProviderStartError(f"Kubernetes Job {job} must run its episode on rank zero")
        return nodes, cpus, memory_mb, max(gpus, partition.min_gpus)

    @classmethod
    def ends_at(cls, started_at: datetime, walltime_minutes: int) -> datetime:
        placement = json.loads(read(cls.job_directory() / "placement.json"))
        return datetime.fromtimestamp(placement["end"], UTC)

    def stage(self, spec: Mapping[str, object]) -> None:
        super().stage(spec)
        if len(self.nodes) > 1:
            # Each peer Pod starts the worker only after every input is in place.
            shutil.copyfile(Path(__file__).with_name("bridge.py"), self.directory / "bridge.py")
            settings = self.directory / SPEC
            write(
                settings,
                json.dumps({**json.loads(read(settings)), "nodes": len(self.nodes)}).encode(),
            )
            write(self.directory / "start-peers", b"1")

    def remote_step(self, *args: str, node: str) -> list[str]:
        if node not in self.nodes:
            raise ProviderStartError(f"Kubernetes Job {self.id} does not hold node {node!r}")
        return [
            sys.executable, "-I", str(self.directory / "bridge.py"), "connect",
            str(self.directory / "workers" / node), *args,
        ]  # fmt: skip

    def ensure_running(self) -> None:
        super().ensure_running()
        for rank in range(1, len(self.nodes)):
            if error := read(self.job_directory() / f"peer-error-{rank}"):
                raise ProviderStartError(f"{self.job_label} lost rank {rank}: {error}")

    async def close(self) -> None:
        if len(self.nodes) > 1 and exists(self.directory / "start-peers"):
            # A peer may still be cleaning its local store when the primary quits.
            # Keep the allocation's files until all ranks have closed them.
            write(self.directory / "stop", b"stop")
            limit = asyncio.get_running_loop().time() + CLOSE_TIMEOUT_SEC
            while not all(
                exists(self.directory / f"peer-done-{rank}")
                or exists(self.job_directory() / f"peer-error-{rank}")
                for rank in range(1, len(self.nodes))
            ):
                if asyncio.get_running_loop().time() >= limit:
                    raise ProviderStartError(f"{self.job_label} did not release its peer workers")
                await asyncio.sleep(0.5)
        await super().close()
