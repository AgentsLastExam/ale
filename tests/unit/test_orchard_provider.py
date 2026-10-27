"""Ownership, provenance and failure semantics of the remote macOS provider."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest

from ale.core.config import OrchardProviderConfig
from ale.core.errors import GuestUnreachableError, ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import Identity, ImageKind, ImageRef, SandboxRequest, SandboxState
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.run.providers import orchard
from ale.run.providers.orchard_client import OrchardExecResult

pytestmark = pytest.mark.unit

REF = "registry.example/macos@sha256:" + "a" * 64


class FakeOrchard:
    def __init__(self):
        self.record = None
        self.deleted = []
        self.closed = False
        self.created = []
        self.uncertain = False
        self.mismatch = False
        self.workers = [
            {
                "name": "mac-1",
                "arch": "arm64",
                "runtime": "tart",
                "last_seen": datetime.now(UTC).isoformat(),
            }
        ]
        self.worker_reads = 0

    async def start(self):
        pass

    async def close(self):
        self.closed = True

    async def list_workers(self):
        self.worker_reads += 1
        return self.workers

    async def get_vm(self, name):
        return self.record

    async def create_vm(self, spec):
        self.created.append(spec)
        self.record = dict(spec) | {
            "uid": "uid-1",
            "status": "running",
            "image_fqn": spec["image"],
            "assignedCPU": spec["cpu"],
            "assignedMemory": spec["memory"],
        }
        if self.mismatch:
            self.record["image"] = REF.replace("a" * 64, "b" * 64)
        if self.uncertain:
            raise TimeoutError("unknown create outcome")
        return self.record

    async def update_vm(self, name, spec):
        self.record.update(spec)
        self.record.update(
            generation=2, observedGeneration=2, conditions=[{"type": "running", "state": "false"}]
        )
        return self.record

    async def delete_vm(self, name):
        self.deleted.append(name)
        self.record = None

    async def exec(self, name, argv, **kwargs):
        assert argv[:2] == ["sudo", "-n"]
        return OrchardExecResult(0, b'{"agent_home":"/Users/user"}', b"")

    async def connect_guest(self, name, argv, *, deadline=None):
        return object()


class FakeGuest:
    def __init__(self, transport):
        self.commands = []
        self.route = (0, "gateway: 192.168.64.1\ninterface: en0\n", "", False)
        self.files = {}
        self.sudo = False
        self.failure = None
        self.health_override = {}
        self.uploads = []
        self.calls = []

    async def health(self):
        return {
            "os": "macos",
            "framework_uid": 0,
            "agent_uid": 501,
            "agent_user": "user",
            "agent_home": "/Users/user",
            "gui": False,
        } | self.health_override

    async def exec(self, argv, **kwargs):
        self.commands.append((argv, kwargs))
        if self.failure:
            raise self.failure
        if argv[:2] == ["/sbin/route", "-n"]:
            return self.route
        if argv[:2] == ["sh", "-c"]:
            user = kwargs.get("run_as")
            return 0, ("501 yes" if self.sudo else "501 no") if user else "0 yes", "", False
        if argv[:2] == ["/usr/bin/sudo", "-l"]:
            return (0 if self.sudo else 1), "", "", False
        return 0, "done", "", False

    async def disk_usage(self, path):
        return 64 * 1024**3, 0, 64 * 1024**3

    async def close(self):
        pass

    async def write_file(self, path, data, **kwargs):
        self.files[path] = data

    async def read_file(self, path, *, offset=0, length=None):
        return self.files[path][offset : None if length is None else offset + length]

    async def upload_dir(self, source, target, *, run_as=None):
        self.uploads.append((source, target, run_as))

    async def call(self, op, params, **kwargs):
        self.calls.append(op)
        return {}, []


@pytest.fixture
def backend(monkeypatch):
    api = FakeOrchard()
    guest = FakeGuest(None)
    provider = orchard.OrchardProvider(OrchardProviderConfig(endpoint="https://orchard.example"))
    monkeypatch.setattr(provider, "_new_client", lambda: api)
    monkeypatch.setattr(orchard, "GuestClient", lambda transport, **kwargs: guest)
    return provider, api, guest


async def request(provider, **kwargs):
    image = await provider.prepare_image(ImageRef(kind=ImageKind.VM, reference=REF))
    return SandboxRequest(
        episode_id="episode-1",
        os=OperatingSystem.MACOS,
        prepared_image=image,
        resources=Resources(),
        network=NetworkPolicy(mode=NetworkMode.OPEN),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_fresh_owned_vm_and_idempotent_teardown(backend):
    provider, api, guest = backend
    sandbox = await provider.create(await request(provider))
    assert sandbox.state == SandboxState.READY
    assert "labels" not in api.created[0]
    assert sandbox.resolved_image.observed_identity == "sha256:" + "a" * 64
    assert sandbox.resolved_image.prepared_identity != sandbox.resolved_image.observed_identity
    assert sandbox.allocation.storage_mb == 65536
    result = await sandbox.exec(["echo", "hi"], identity=Identity.AGENT)
    assert result.ok and guest.commands[-1][1]["run_as"] == "user"
    await sandbox.destroy()
    await sandbox.destroy()
    assert api.deleted == [sandbox.sandbox_id]
    assert api.closed and sandbox.state == SandboxState.DESTROYED


@pytest.mark.asyncio
async def test_local_image_is_rejected_before_api(backend):
    provider, api, _ = backend
    with pytest.raises(ProviderCapabilityError, match="OCI image reference"):
        await provider.prepare_image(ImageRef(kind=ImageKind.VM, reference="image:latest"))
    assert not api.created


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"network": NetworkPolicy(mode=NetworkMode.BLOCK)},
        {"retention": "keep"},
        {"resources": Resources(gpus=1)},
        {"os": OperatingSystem.LINUX},
        {"user": "root"},
    ],
)
async def test_unsupported_requests_fail_before_create(backend, change):
    provider, api, _ = backend
    req = (await request(provider)).model_copy(update=change)
    with pytest.raises(ProviderCapabilityError):
        await provider.create(req)
    assert not api.created


@pytest.mark.asyncio
async def test_provenance_mismatch_cleans_only_created_vm(backend):
    provider, api, _ = backend
    api.mismatch = True
    with pytest.raises(ProviderStartError, match="attest requested image"):
        await provider.create(await request(provider))
    assert len(api.deleted) == 1 and api.closed


@pytest.mark.asyncio
async def test_uid_replacement_is_not_deleted(backend):
    provider, api, _ = backend
    sandbox = await provider.create(await request(provider))
    api.record["uid"] = "different-owner"
    with pytest.raises(ProviderStartError, match="UID changed"):
        await sandbox.destroy()
    assert not api.deleted and sandbox.state != SandboxState.DESTROYED


@pytest.mark.asyncio
async def test_uncertain_create_is_not_retried_or_adopted(backend):
    provider, api, _ = backend
    api.uncertain = True
    with pytest.raises(ProviderStartError, match="inspect and clean it up manually"):
        await provider.create(await request(provider))
    assert len(api.created) == 1 and not api.deleted and api.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError(), GuestUnreachableError("closed")])
async def test_uncertain_exec_destroys_vm_before_returning(backend, failure):
    provider, api, guest = backend
    sandbox = await provider.create(await request(provider))
    guest.failure = failure
    with pytest.raises(type(failure)):
        await sandbox.exec(["long-running"])
    assert api.deleted == [sandbox.sandbox_id]
    assert sandbox.state == SandboxState.DESTROYED
    assert "exec_control" not in guest.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [asyncio.CancelledError(), TimeoutError(), GuestUnreachableError("closed")]
)
async def test_cleanup_failure_does_not_replace_execution_failure(backend, monkeypatch, failure):
    provider, api, guest = backend
    sandbox = await provider.create(await request(provider))
    guest.failure = failure

    async def failed_stop(name, spec):
        raise ProviderStartError("worker offline")

    monkeypatch.setattr(api, "update_vm", failed_stop)
    with pytest.raises(type(failure)) as raised:
        await sandbox.exec(["long-running"])
    assert raised.value is failure
    assert any("cleanup" in note and sandbox.sandbox_id in note for note in failure.__notes__)
    assert not api.deleted


@pytest.mark.asyncio
async def test_sudo_false_rejects_privileged_image(backend):
    provider, api, guest = backend
    guest.sudo = True
    with pytest.raises(ProviderCapabilityError, match="did not request elevated privileges"):
        await provider.create(await request(provider))
    assert len(api.deleted) == 1


@pytest.mark.asyncio
async def test_queue_and_pull_do_not_consume_guest_ready_budget(backend, monkeypatch):
    provider, api, _ = backend
    provider.config = provider.config.model_copy(
        update={"provision_timeout_sec": 1, "boot_timeout_sec": 0.02}
    )
    get_vm = api.get_vm
    delayed = False

    async def queued_vm(name):
        nonlocal delayed
        if api.record is not None and not delayed:
            delayed = True
            await asyncio.sleep(0.04)
        return await get_vm(name)

    monkeypatch.setattr(api, "get_vm", queued_vm)
    sandbox = await provider.create(await request(provider))
    assert sandbox.state is SandboxState.READY
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_provision_timeout_reports_last_control_plane_state(backend, monkeypatch):
    provider, api, _ = backend
    provider.config = provider.config.model_copy(update={"provision_timeout_sec": 0.02})
    create_vm = api.create_vm

    async def pending_vm(spec):
        record = await create_vm(spec)
        record["status"] = "pending"
        return record

    monkeypatch.setattr(api, "create_vm", pending_vm)
    with pytest.raises(ProviderStartError, match=r"provision.*pending"):
        await provider.create(await request(provider))
    assert len(api.deleted) == 1


@pytest.mark.asyncio
async def test_guest_ready_timeout_has_distinct_diagnostic(backend, monkeypatch):
    provider, api, guest = backend
    provider.config = provider.config.model_copy(update={"boot_timeout_sec": 0.01})

    async def silent_guest():
        await asyncio.Event().wait()

    monkeypatch.setattr(guest, "health", silent_guest)
    with pytest.raises(ProviderStartError, match="guest readiness"):
        await provider.create(await request(provider))
    assert len(api.deleted) == 1


@pytest.mark.asyncio
async def test_bootstrap_and_guest_connections_share_startup_deadline(backend, monkeypatch):
    provider, api, _ = backend
    deadlines = []
    bootstrap_exec = api.exec
    connect_guest = api.connect_guest

    async def bootstrap(name, argv, **kwargs):
        deadline = kwargs["deadline"]
        deadlines.append(deadline)
        assert 179 < deadline - asyncio.get_running_loop().time() <= 180
        assert 179 < kwargs["timeout_sec"] <= 180
        return await bootstrap_exec(name, argv, **kwargs)

    async def connect(name, argv, *, deadline):
        deadlines.append(deadline)
        return await connect_guest(name, argv, deadline=deadline)

    monkeypatch.setattr(api, "exec", bootstrap)
    monkeypatch.setattr(api, "connect_guest", connect)
    sandbox = await provider.create(await request(provider))
    assert len(deadlines) == 3
    assert len(set(deadlines)) == 1
    await sandbox.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "health",
    [{"os": "linux"}, {"framework_uid": 501}, {"agent_uid": 0}, {"agent_user": "someone-else"}],
)
async def test_guest_contract_checked_and_failure_cleaned(backend, health):
    provider, api, guest = backend
    guest.health_override = health
    with pytest.raises(ProviderCapabilityError, match="identity check failed"):
        await provider.create(await request(provider))
    assert len(api.deleted) == 1


@pytest.mark.asyncio
async def test_upload_uses_shared_guest_tree_protocol(backend, tmp_path):
    provider, _, guest = backend
    sandbox = await provider.create(await request(provider))
    source = tmp_path / "source"
    source.mkdir()
    await sandbox.upload_dir(str(source), "/Users/user/task", identity=Identity.AGENT)
    assert guest.uploads == [(str(source), "/Users/user/task", "user")]
    assert not guest.files
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_remote_gateway_preserves_port_and_path(backend):
    provider, _, _ = backend
    provider.config.gateway_host = "10.0.0.9"
    sandbox = await provider.create(await request(provider, gateway_url="http://127.0.0.1:42/v1"))
    assert sandbox.gateway_url == "http://10.0.0.9:42/v1"
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_failed_reconciliation_keeps_orphan_name(backend, monkeypatch):
    provider, api, _ = backend
    api.uncertain = True
    original = api.get_vm

    async def get(name):
        if api.created:
            raise TimeoutError("controller unavailable")
        return await original(name)

    monkeypatch.setattr(api, "get_vm", get)
    with pytest.raises(ProviderStartError, match="reconciliation failed") as raised:
        await provider.create(await request(provider))
    assert api.created[0]["name"] in str(raised.value)
    assert not api.deleted and len(api.created) == 1


@pytest.mark.asyncio
async def test_failed_stop_is_not_successful_destruction(backend, monkeypatch):
    provider, api, _ = backend
    sandbox = await provider.create(await request(provider))

    async def failed_update(name, spec):
        raise ProviderStartError("worker unreachable")

    monkeypatch.setattr(api, "update_vm", failed_update)
    with pytest.raises(ProviderStartError, match="stop unconfirmed"):
        await sandbox.destroy()
    assert not api.deleted and api.record is not None
    assert sandbox.state != SandboxState.DESTROYED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"assignedCPU": 99},
        {"assignedMemory": 128},
        {"image_fqn": "registry.example/macos@sha256:" + "b" * 64},
    ],
)
async def test_actual_observation_differs_from_request(backend, monkeypatch, change):
    provider, api, _ = backend
    original = api.create_vm

    async def create(spec):
        record = await original(spec)
        record.update(change)
        return record

    monkeypatch.setattr(api, "create_vm", create)
    with pytest.raises(ProviderStartError):
        await provider.create(await request(provider))
    assert len(api.deleted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [
        {"PYTHONPATH": "/Users/user"},
        {"BAD=NAME": "x"},
        {"DYLD_INSERT_LIBRARIES": "/Users/user/evil.dylib"},
    ],
)
async def test_framework_environment_cannot_be_overridden(backend, env):
    provider, api, _ = backend
    with pytest.raises(ProviderCapabilityError):
        await provider.create(await request(provider, env=env))
    assert not api.created


@pytest.mark.asyncio
async def test_stop_already_requested_is_polled_without_repeated_put(backend, monkeypatch):
    provider, api, _ = backend
    sandbox = await provider.create(await request(provider))
    api.record.update(
        powerState="stopped",
        generation=3,
        observedGeneration=3,
        conditions=[{"type": "running", "state": "false"}],
    )

    async def unexpected_update(name, spec):
        pytest.fail("must not rewrite terminal powerState")

    monkeypatch.setattr(api, "update_vm", unexpected_update)
    await sandbox.destroy()
    assert sandbox.state == SandboxState.DESTROYED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    [
        "mac-cua-common-v1",
        "../other",
        "/absolute",
        ".",
        "name@tag",
        "registry.example/macos@sha256:bad",
        "https://registry.example/macos:v1",
        "registry.example/team/../macos:v1",
    ],
)
async def test_invalid_images_rejected_before_api(backend, reference):
    provider, api, _ = backend
    with pytest.raises(ProviderCapabilityError, match="OCI image reference"):
        await provider.prepare_image(ImageRef(kind=ImageKind.VM, reference=reference))
    assert not api.created


@pytest.mark.asyncio
async def test_terminal_failed_condition_is_not_physical_stop_proof(backend, monkeypatch):
    provider, api, _ = backend
    original = api.create_vm

    async def failed_create(spec):
        record = await original(spec)
        record.update(
            status="failed",
            powerState="running",
            generation=0,
            observedGeneration=0,
            conditions=[{"type": "running", "state": "false"}],
        )
        return record

    async def no_put(name, spec):
        pytest.fail("terminal failed records reject PUT; do not send one")

    monkeypatch.setattr(api, "create_vm", failed_create)
    monkeypatch.setattr(api, "update_vm", no_put)
    with pytest.raises(
        ProviderStartError, match=r"terminal failed.*physical stop unconfirmed"
    ) as raised:
        await provider.create(await request(provider))
    assert "Provisioning failed (ProviderStartError)" in str(raised.value)
    assert not api.deleted and api.record is not None
    assert api.closed


@pytest.mark.asyncio
async def test_file_contract_and_streamed_upload_with_unprivileged_guestd(backend, tmp_path):
    """Exercise real guest protocol locally; neither Orchard nor a VM is started."""
    import hashlib
    import os
    import pwd
    import sys
    from pathlib import Path

    from ale.core.sandbox import ResolvedImage, ResourceAllocation
    from ale.run import guestd
    from ale.run.transport import GuestClient, StdioTransport

    provider, api, _ = backend
    req = await request(provider, user=pwd.getpwuid(os.getuid()).pw_name)
    main = Path(guestd.__file__).with_name("main.py")
    command_transport = StdioTransport([sys.executable, str(main), "--stdio"])
    file_transport = StdioTransport([sys.executable, str(main), "--stdio"])
    await command_transport.start()
    await file_transport.start()
    api.record = {
        "uid": "uid-1",
        "status": "running",
        "os": "darwin",
        "arch": "arm64",
        "runtime": "tart",
    }
    sandbox = orchard.OrchardSandbox(
        request=req,
        gateway_host=None,
        orchard=api,
        client=GuestClient(command_transport),
        file_client=GuestClient(file_transport),
        name="unit-guest-files",
        uid="uid-1",
        agent_home=str(tmp_path),
        allocation=ResourceAllocation(
            cpus=1, memory_mb=1024, sudo=False, network_mode=NetworkMode.OPEN, provider="orchard"
        ),
        resolved_image=ResolvedImage(
            kind=ImageKind.VM,
            prepared_identity=req.prepared_image.prepared_identity,
            observed_identity=req.prepared_image.input_identity,
            observed_ref=REF,
        ),
    )
    task = None
    try:
        source = tmp_path / "source"
        source.mkdir()
        (source / "empty").mkdir()
        (source / "run.sh").write_bytes(b"first\nsecond\n")
        (source / "run.sh").chmod(0o777)
        (source / "link").symlink_to("run.sh")
        target = tmp_path / "target"
        await sandbox.upload_dir(str(source), str(target))
        assert (target / "run.sh").stat().st_mode & 0o777 == 0o644
        assert (target / "empty").stat().st_mode & 0o777 == 0o755
        assert (target / "link").is_symlink()
        ready = asyncio.Event()

        async def sink(stream, data):
            if stream == "stdout" and b"ready" in data:
                ready.set()

        task = asyncio.create_task(
            sandbox.exec(
                [sys.executable, "-c", 'import time; print("ready",flush=True);time.sleep(0.5)'],
                output_sink=sink,
                timeout_sec=5,
            )
        )
        await asyncio.wait_for(ready.wait(), 5)
        files = await asyncio.wait_for(sandbox.list_files(str(target)), 0.25)
        assert [item.path for item in files] == [str(target / "run.sh")]
        assert files[0].size == 13
        assert await sandbox.read_file(files[0].path, offset=6, length=7) == b"second\n"
        assert await sandbox.file_digest(files[0].path, length=6) == (
            "sha256:" + hashlib.sha256(b"first\n").hexdigest()
        )
        assert not task.done()
        for op in [
            sandbox.read_file(str(target / "link")),
            sandbox.file_digest(str(target / "link"), length=2),
        ]:
            with pytest.raises(GuestUnreachableError):
                await op
        assert await sandbox.list_files(str(target / "missing")) == ()
        await sandbox.open_egress()
        await sandbox.close_egress()
        assert (await task).ok
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await sandbox.destroy()


@pytest.mark.parametrize(
    "workers, message",
    [
        ([], "No Orchard workers"),
        ([{"name": "mac-1", "arch": "amd64", "runtime": "tart"}], "ARM64 Tart"),
        ([{"name": "mac-1", "arch": "arm64", "runtime": "docker"}], "ARM64 Tart"),
    ],
)
async def test_prepare_rejects_unavailable_cluster_before_creating_vm(backend, workers, message):
    provider, api, _ = backend
    api.workers = workers
    with pytest.raises(ProviderStartError, match=message):
        await request(provider)
    assert not api.created and api.closed


@pytest.mark.parametrize(
    "update, message",
    [
        ({"scheduling_paused": True}, "paused"),
        ({"last_seen": "2000-01-01T00:00:00Z"}, "heartbeat"),
        ({"last_seen": "invalid"}, "heartbeat"),
        ({"last_seen": "2026-01-01T00:00:00"}, "heartbeat"),
    ],
)
async def test_prepare_rejects_paused_or_stale_worker(backend, update, message):
    provider, api, _ = backend
    api.workers[0].update(update)
    with pytest.raises(ProviderStartError, match=message):
        await request(provider)
    assert not api.created


async def test_busy_workers_pass_and_provision_rechecks_after_preparation(backend):
    provider, api, _ = backend
    api.workers[0]["resources"] = {"org.cirruslabs.tart-vms": 0}
    prepared_request = await request(provider)
    assert api.worker_reads == 1 and not api.created
    api.workers = []
    with pytest.raises(ProviderStartError, match="No Orchard workers"):
        await provider.create(prepared_request)
    assert api.worker_reads == 2 and not api.created


async def test_preflight_accepts_fresh_worker_among_other_unavailable_workers(backend):
    provider, api, _ = backend
    api.workers.insert(
        0,
        {
            "name": "old",
            "arch": "arm64",
            "runtime": "tart",
            "last_seen": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        },
    )
    await provider.preflight()
    assert api.worker_reads == 1 and api.closed and not api.created


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["registry.example/macos:v1", "registry.example/macos"])
async def test_tag_is_pinned_before_vm_creation_and_not_reresolved(backend, monkeypatch, reference):
    provider, api, _ = backend
    resolved = []

    async def resolve(ref):
        resolved.append(ref)
        return REF

    monkeypatch.setattr(orchard, "resolve_registry_reference", resolve)
    prepared = await provider.prepare_image(ImageRef(kind=ImageKind.VM, reference=reference))
    assert prepared.runtime_ref == prepared.resolved_reference == REF
    assert prepared.input_identity == "sha256:" + "a" * 64
    req = await request(provider)
    sandbox = await provider.create(req.model_copy(update={"prepared_image": prepared}))
    assert api.created[0]["image"] == REF
    assert resolved == [reference]
    assert sandbox.resolved_image.observed_ref == REF
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_new_preparation_refreshes_tag_but_prepared_image_stays_fixed(backend, monkeypatch):
    provider, _, _ = backend
    refs = iter([REF, REF.replace("a" * 64, "b" * 64)])

    async def resolve(ref):
        return next(refs)

    monkeypatch.setattr(orchard, "resolve_registry_reference", resolve)
    image = ImageRef(kind=ImageKind.VM, reference="registry.example/macos:latest")
    first = await provider.prepare_image(image)
    second = await provider.prepare_image(image)
    assert first.prepared_identity != second.prepared_identity
    assert await provider.prepare_image(first) == first


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [REF, REF.replace("@", ":v1@")])
async def test_pinned_digest_needs_no_host_registry_query(backend, monkeypatch, reference):
    provider, _, _ = backend

    async def unexpected(ref):
        pytest.fail("digest reference must not query the registry")

    monkeypatch.setattr(orchard, "resolve_registry_reference", unexpected)
    prepared = await provider.prepare_image(ImageRef(kind=ImageKind.VM, reference=reference))
    assert prepared.runtime_ref == REF


@pytest.mark.asyncio
async def test_tag_resolution_failure_does_not_create_vm(backend, monkeypatch):
    provider, api, _ = backend

    async def denied(ref):
        raise ProviderStartError("registry access denied")

    monkeypatch.setattr(orchard, "resolve_registry_reference", denied)
    with pytest.raises(ProviderStartError, match="registry access denied"):
        await provider.prepare_image(
            ImageRef(kind=ImageKind.VM, reference="registry.example/macos:v1")
        )
    assert not api.created


@pytest.mark.asyncio
async def test_prepared_image_cannot_reintroduce_mutable_tag(backend):
    provider, api, _ = backend
    prepared = await provider.prepare_image(ImageRef(kind=ImageKind.VM, reference=REF))
    altered = prepared.model_copy(update={"runtime_ref": "registry.example/macos:v1"})
    with pytest.raises(ProviderCapabilityError, match="already be pinned"):
        await provider.prepare_image(altered)
    assert not api.created


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["192.168.64.1", "192.168.77.1"])
async def test_local_gateway_is_discovered_from_guest_route(backend, host):
    provider, _, guest = backend
    guest.route = (0, f"route to: default\n    gateway: {host}\ninterface: en0\n", "", False)
    sandbox = await provider.create(await request(provider, gateway_url="http://0.0.0.0:4567/v1"))
    assert sandbox.gateway_url == f"http://{host}:4567/v1"
    assert provider.config.gateway_host is None
    route_calls = [c for c in guest.commands if c[0][0] == "/sbin/route"]
    assert len(route_calls) == 1
    assert route_calls[0][0] == ["/sbin/route", "-n", "get", "-inet", "default"]
    assert route_calls[0][1]["run_as"] == "user"
    await sandbox.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["10.0.0.9", "ale.example.test", "2001:db8::1"])
async def test_gateway_override_skips_route_discovery(backend, host):
    provider, _, guest = backend
    provider.config.gateway_host = host
    guest.route = (1, "", "route unavailable", False)
    sandbox = await provider.create(await request(provider, gateway_url="http://127.0.0.1:42/v1"))
    authority = f"[{host}]" if ":" in host else host
    assert sandbox.gateway_url == f"http://{authority}:42/v1"
    assert not any(c[0][0] == "/sbin/route" for c in guest.commands)
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_no_model_gateway_needs_no_route_discovery(backend):
    provider, _, guest = backend
    guest.route = (1, "", "route unavailable", False)
    sandbox = await provider.create(await request(provider))
    assert sandbox.gateway_url is None
    assert not any(c[0][0] == "/sbin/route" for c in guest.commands)
    await sandbox.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route",
    [
        (1, "", "missing", False),
        (0, "gateway: 192.168.64.1", "", True),
        (0, "interface: en0", "", False),
        (0, "gateway: 127.0.0.1", "", False),
        (0, "gateway: 0.0.0.0", "", False),
        (0, "gateway: 224.0.0.1", "", False),
        (0, "gateway: not-an-ip", "", False),
    ],
)
async def test_invalid_gateway_discovery_cleans_up_vm(backend, route):
    provider, api, guest = backend
    guest.route = route
    with pytest.raises(ProviderStartError, match=r"vm\.orchard\.gateway_host"):
        await provider.create(await request(provider, gateway_url="http://0.0.0.0:4567"))
    assert len(api.deleted) == 1


@pytest.mark.parametrize("fail_authorization", [False, True])
async def test_bootstrap_installs_authorizes_then_probes_and_cleans_failure(
    backend, monkeypatch, fail_authorization
):
    provider, api, guest = backend
    provider.config = OrchardProviderConfig(
        endpoint="https://orchard.example", desktop_bootstrap={}
    )
    guest.sudo = True
    monkeypatch.setattr(orchard.orchard_permissions, "preflight", lambda config: None)
    steps = []

    async def install(*args):
        steps.append("install")

    async def authorize(*args):
        steps.append("authorize")
        if fail_authorization:
            raise ProviderStartError("UI failed")

    async def probe(*args, **kwargs):
        steps.append("actual_capture")

    monkeypatch.setattr(orchard.orchard_permissions, "install_driver", install)
    monkeypatch.setattr(orchard.orchard_permissions, "authorize_driver", authorize)
    monkeypatch.setattr(orchard, "await_desktop", probe)
    req = await request(provider, sudo=True)
    if fail_authorization:
        with pytest.raises(ProviderStartError, match="UI failed"):
            await provider.create(req)
        assert steps == ["install", "authorize"]
        assert len(api.deleted) == 1
    else:
        sandbox = await provider.create(req)
        assert steps == ["install", "authorize", "actual_capture"]
        await sandbox.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "short", "corrupt", "oversize", "cancel", "capture"])
async def test_screenshots_use_bounded_reads_and_always_remove_the_guest_file(
    backend, monkeypatch, failure
):
    import hashlib

    provider, _, guest = backend
    sandbox = await provider.create(await request(provider))
    png = b"\x89PNG\r\n\x1a\n" + b"pixels" * 800_000
    removed = []
    reads = []

    async def call(op, params, **kwargs):
        path = params["path"]
        if op == "screenshot":
            guest.files[path] = png
            if failure == "capture":
                raise GuestUnreachableError("capture failed")
            return {
                "bytes": 100 * 1024**2 if failure == "oversize" else len(png),
                "sha256": hashlib.sha256(png).hexdigest(),
            }, []
        assert op == "remove"
        removed.append(path)
        guest.files.pop(path, None)
        return {}, []

    async def read(path, *, offset=0, length=None):
        assert length is not None and 0 < length <= 64 * 1024
        reads.append((offset, length))
        if failure == "cancel":
            raise asyncio.CancelledError
        block = png[offset : offset + length]
        if failure == "short":
            return block[:-1]
        if failure == "corrupt":
            return b"x" + block[1:]
        return block

    monkeypatch.setattr(guest, "call", call)
    monkeypatch.setattr(guest, "read_file", read)
    if failure is None:
        assert await sandbox.screenshot() == png
        assert len(reads) > 64
    else:
        with pytest.raises(
            asyncio.CancelledError if failure == "cancel" else GuestUnreachableError
        ):
            await sandbox.screenshot()
    assert len(removed) == 1 and not guest.files
    assert removed[0].startswith("/opt/ale/screenshot-")
    if failure in ("oversize", "capture"):
        assert not reads


@pytest.mark.asyncio
async def test_large_screenshot_round_trip_over_bounded_guest_responses(
    backend, monkeypatch, tmp_path
):
    import io
    import json
    import random
    import sys

    from PIL import Image

    from ale.run.guestd import gui
    from ale.run.guestd.main import Handler
    from ale.run.transport import GuestClient

    buffer = io.BytesIO()
    Image.frombytes("RGB", (1400, 1200), random.Random(1).randbytes(1400 * 1200 * 3)).save(
        buffer, format="PNG"
    )
    png = buffer.getvalue()
    assert len(png) > 4 * 1024**2
    monkeypatch.setitem(sys.modules, "gui", gui)
    monkeypatch.setattr(gui, "capture_screen", lambda: png)
    monkeypatch.setattr(orchard, "_ROOT", str(tmp_path.resolve()))
    provider, _, _ = backend
    sandbox = await provider.create(await request(provider))

    class BoundedTransport:
        def __init__(self):
            self.frames = []
            self.largest = 0
            self.handler = Handler(self.emit)

        def emit(self, message):
            frame = json.dumps(message)
            self.largest = max(self.largest, len(frame))
            assert len(frame) < 100_000, "unbounded guest response"
            self.frames.append(frame)

        async def send(self, data):
            message = json.loads(data)
            self.emit(self.handler.dispatch(message["id"], message["op"], message["params"]))

        async def recv(self):
            return self.frames.pop(0)

    transport = BoundedTransport()
    sandbox._client = GuestClient(transport)
    sandbox._file_client = sandbox._client
    assert await sandbox.screenshot() == png
    assert transport.largest < 100_000
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offset,length", [(0, None), (7, None), (9, 200_000), (0, 0), (2_000_000, None), (0, 64 * 1024)]
)
async def test_file_reads_bound_each_response_and_preserve_ranges(
    backend, monkeypatch, offset, length
):
    provider, _, guest = backend
    sandbox = await provider.create(await request(provider))
    content = bytes(range(256)) * 4096
    calls = []

    async def read(path, *, offset=0, length=None):
        assert path == "/Users/user/log.jsonl"
        assert length is not None and 0 <= length <= 64 * 1024
        calls.append((offset, length))
        return content[offset : offset + length]

    monkeypatch.setattr(guest, "read_file", read)
    result = await sandbox.read_file("/Users/user/log.jsonl", offset=offset, length=length)
    assert result == content[offset : None if length is None else offset + length]
    assert calls


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,length", [(-1, None), (0, -1)])
async def test_file_reads_reject_invalid_ranges_before_transport(
    backend, monkeypatch, offset, length
):
    provider, _, guest = backend
    sandbox = await provider.create(await request(provider))

    async def read(*args, **kwargs):
        pytest.fail("invalid range reached the guest")

    monkeypatch.setattr(guest, "read_file", read)
    with pytest.raises(ValueError, match="nonnegative"):
        await sandbox.read_file("/missing", offset=offset, length=length)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [GuestUnreachableError("transfer interrupted"), asyncio.CancelledError()]
)
async def test_file_read_failure_does_not_return_partial_data_or_retry(
    backend, monkeypatch, failure
):
    provider, _, guest = backend
    sandbox = await provider.create(await request(provider))
    calls = 0

    async def read(path, *, offset=0, length=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        assert length == 64 * 1024
        return b"x" * length

    monkeypatch.setattr(guest, "read_file", read)
    with pytest.raises(type(failure)):
        await sandbox.read_file("/log")
    assert calls == 2
