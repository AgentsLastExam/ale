"""Ownership, provenance and failure semantics of the macOS Provider."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orbit.core.capability import Arch, SandboxNeeds
from orbit.core.errors import GuestUnreachableError, ProviderCapabilityError, ProviderStartError
from orbit.core.sandbox import (
    DesktopResolution,
    Identity,
    ImageKind,
    ImageRef,
    SandboxRequest,
    SandboxRole,
    SandboxState,
)
from orbit.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from orbit.run.providers import privileges
from orbit.run.providers.fleet import FleetProvider
from orbit.run.providers.orchard import client as orchard_client
from orbit.run.providers.orchard import framing as orchard_framing
from orbit.run.providers.orchard import provider as orchard
from orbit.run.providers.orchard.client import OrchardExecResult
from orbit.run.providers.orchard.framing import FramedImage

pytestmark = pytest.mark.unit

REF = "registry.example/macos@sha256:" + "a" * 64
FRAMED = FramedImage(
    name="orbit-framed-" + "f" * 32,
    identity="sha256:" + "f" * 64,
    agent_home="/Users/user",
    agent_uid=501,
)


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
        #: What happened to the VM, in order: its policy changes and ended SSH connections.
        self.events = []
        #: How many polls a worker takes to apply a changed policy.
        self.lag = 1
        #: How a worker that cannot change a policy in place goes on: it stops the VM where a
        #: poll sees it ("stopped"), or between two polls ("rebooted").
        self.fallback = None
        self.guest = None

    async def start(self):
        pass

    async def close(self):
        self.closed = True

    async def list_workers(self):
        self.worker_reads += 1
        return self.workers

    async def get_vm(self, name):
        if self.record is not None and self.record.get("applying"):
            self.record["applying"] -= 1
            if not self.record["applying"]:
                self.record["observedGeneration"] = self.record["generation"]
        return self.record

    async def create_vm(self, spec):
        self.created.append(spec)
        self.record = dict(spec) | {
            "powerState": "running",
            "generation": 0,
            "observedGeneration": 0,
            "conditions": [{"type": "running", "state": "true"}],
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
        if spec.get("powerState") != "stopped":
            return self.change_policy(spec)
        self.record.update(spec)
        self.record.update(
            generation=2, observedGeneration=2, conditions=[{"type": "running", "state": "false"}]
        )
        return self.record

    def change_policy(self, spec):
        """Apply a PUT that changes only the Softnet rules, as a worker does at its next sync."""
        self.events.append(("policy", spec["netSoftnetAllow"], spec["netSoftnetBlock"]))
        self.put = dict(spec)
        self.record.update(spec, generation=self.record["generation"] + 1, applying=self.lag)
        if self.fallback == "stopped":
            self.record["conditions"] = [{"type": "running", "state": "false"}]
        elif self.fallback == "rebooted":
            self.guest.boot = "boot-2"
        return dict(self.record)

    async def delete_vm(self, name):
        self.deleted.append(name)
        self.record = None

    async def exec(self, name, argv, **kwargs):
        assert argv[:2] == ["sudo", "-n"]
        if "kill -TERM" in argv[-1]:
            self.events.append(("end-ssh", name))
            raise GuestUnreachableError("the command's connection ended")
        return OrchardExecResult(0, b'{"agent_home":"/Users/user"}', b"")

    async def connect_guest(self, name, argv, *, deadline=None):
        self.events.append(("connect", name))
        return object()


class FakeGuest:
    def __init__(self, transport):
        self.commands = []
        self.route = (0, "gateway: 192.168.64.1\ninterface: en0\n", "", False)
        self.relay = (0, "7412 4242\n", "", False)
        self.files = {}
        self.sudo = False
        self.failure = None
        self.health_override = {}
        self.uploads = []
        self.calls = []
        self.screen = (1024, 768)
        self.boot = "boot-1"
        self.closes = 0
        #: Every egress relay guestd was asked for: the proxy URL, bypass and desktop flag.
        self.relays = []

    async def screen_size(self):
        return self.screen

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
        if argv[:1] == ["/usr/sbin/sysctl"]:
            return 0, self.boot + "\n", "", False
        if orchard._GATEWAY_RELAY in argv:
            return self.relay
        if argv[:2] == ["sh", "-c"]:
            user = kwargs.get("run_as")
            return 0, ("501 yes" if self.sudo else "501 no") if user else "0 yes", "", False
        return 0, "done", "", False

    async def disk_usage(self, path):
        return 64 * 1024**3, 0, 64 * 1024**3

    async def close(self):
        self.closes += 1

    async def egress_relay(self, proxy, *, bypass=(), desktop=False):
        self.relays.append((proxy, list(bypass), desktop))
        return "127.0.0.1:41000" if proxy else None

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
    provider = orchard.OrchardProvider(SimpleNamespace(client=lambda: api))
    api.guest = guest
    api.framed = FRAMED
    api.frames = []

    async def frame(local, image, *, user):
        api.frames.append((image, user))
        return api.framed

    api.desktops = []

    async def desktop(client, *, agent_user, agent_uid, timeout_sec):
        api.desktops.append((agent_user, agent_uid))

    async def at_resolution(local, framed, resolution):
        return f"{framed.name}-{resolution}"

    monkeypatch.setattr(orchard, "frame_image", frame)
    monkeypatch.setattr(orchard, "at_resolution", at_resolution)
    monkeypatch.setattr(orchard, "await_desktop", desktop)
    monkeypatch.setattr(orchard, "check_framer", lambda: None)
    monkeypatch.setattr(orchard, "GuestClient", lambda transport, **kwargs: guest)
    return provider, api, guest


async def request(provider, **kwargs):
    image = await provider.prepare_image(ImageRef(kind=ImageKind.VM, reference=REF))
    return SandboxRequest(
        **{
            "episode_id": "episode-1",
            "os": OperatingSystem.MACOS,
            "prepared_image": image,
            "resources": Resources(),
            "network": NetworkPolicy(mode=NetworkMode.OPEN),
        }
        | kwargs
    )


@pytest.mark.asyncio
async def test_fresh_owned_vm_and_idempotent_teardown(backend):
    provider, api, guest = backend
    sandbox = await provider.create(await request(provider))
    assert sandbox.state == SandboxState.READY
    assert "labels" not in api.created[0]
    assert api.created[0]["image"] == f"{FRAMED.name}-1024x768"
    assert api.frames == [(sandbox.request.prepared_image, "user")]
    assert sandbox.resolved_image.prepared_identity == "sha256:" + "a" * 64
    assert sandbox.resolved_image.observed_identity == FRAMED.identity
    assert sandbox.resolved_image.observed_ref == FRAMED.name
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
    monkeypatch.setattr(orchard_client, "PROVISION_TIMEOUT_SEC", 1)
    monkeypatch.setattr(orchard, "_BOOT_TIMEOUT_SEC", 0.02)
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
    monkeypatch.setattr(orchard_client, "PROVISION_TIMEOUT_SEC", 0.02)
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
    monkeypatch.setattr(orchard, "_BOOT_TIMEOUT_SEC", 0.01)

    async def silent_guest():
        await asyncio.Event().wait()

    monkeypatch.setattr(guest, "health", silent_guest)
    with pytest.raises(ProviderStartError, match="guest readiness"):
        await provider.create(await request(provider))
    assert len(api.deleted) == 1


@pytest.mark.asyncio
async def test_guest_connections_share_the_startup_deadline(backend, monkeypatch):
    provider, api, _ = backend
    deadlines = []
    connect_guest = api.connect_guest

    async def connect(name, argv, *, deadline):
        assert 299 < deadline - asyncio.get_running_loop().time() <= 300
        assert argv[argv.index("-i") + 1 :][-6:-2] == [orchard.MACOS_PYTHON, "-I", "-S", "-c"]
        deadlines.append(deadline)
        return await connect_guest(name, argv, deadline=deadline)

    monkeypatch.setattr(api, "connect_guest", connect)
    sandbox = await provider.create(await request(provider))
    assert len(deadlines) == 2
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


def relays(guest):
    return [c[0][-3:] for c in guest.commands if orchard._GATEWAY_RELAY in c[0]]


@pytest.mark.asyncio
async def test_gateway_is_reached_at_the_fixed_port_on_loopback(backend):
    provider, _, guest = backend
    sandbox = await provider.create(await request(provider, gateway_url="http://127.0.0.1:42/v1"))
    assert sandbox.gateway_url == "http://127.0.0.1:7412/v1"
    assert relays(guest) == [["192.168.64.1", "42", "7412"]]
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_failed_gateway_relay_deletes_the_vm(backend):
    provider, api, guest = backend
    guest.relay = (1, "", "Address already in use", False)
    with pytest.raises(ProviderStartError, match="Gateway relay"):
        await provider.create(await request(provider, gateway_url="http://127.0.0.1:42/v1"))
    assert api.deleted == [api.created[0]["name"]]


@pytest.mark.asyncio
async def test_gateway_relay_carries_both_directions():
    async def echo(reader, writer):
        while data := await reader.read(1024):
            writer.write(data.upper())
            await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]
    started = subprocess.run(
        [sys.executable, "-I", "-S", "-c", orchard._GATEWAY_RELAY, "127.0.0.1", str(port), "0"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    listen, pid = map(int, started.stdout.split())
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", listen)
        writer.write(b"model request")
        await writer.drain()
        assert await reader.readexactly(13) == b"MODEL REQUEST"
        writer.close()
    finally:
        os.kill(pid, signal.SIGKILL)
        upstream.close()
        await upstream.wait_closed()


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
        {"image": "orbit-framed-other"},
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
async def test_file_contract_and_streamed_upload_with_unprivileged_guestd(
    backend, tmp_path, monkeypatch
):
    """Exercise real guest protocol locally; neither Orchard nor a VM is started."""
    import hashlib
    import os
    import pwd
    import sys
    from pathlib import Path

    from orbit.core.sandbox import ResolvedImage, ResourceAllocation
    from orbit.run import guestd
    from orbit.run.transport import GuestClient, StdioTransport

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
        local=SimpleNamespace(client=lambda: api),
        orchard=api,
        client=GuestClient(command_transport),
        file_client=GuestClient(file_transport),
        source=FRAMED,
        agent=True,
        host=None,
        name="unit-guest-files",
        uid="uid-1",
        agent_home=str(tmp_path),
        allocation=ResourceAllocation(
            cpus=1,
            memory_mb=1024,
            sudo=False,
            network_mode=NetworkMode.OPEN,
            provider="orchard",
            leaf="orchard",
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
        monkeypatch.setattr(orchard, "MACOS_PYTHON", sys.executable)
        me = pwd.getpwuid(os.getuid())
        assert await sandbox.account_home(me.pw_name) == me.pw_dir
        assert await sandbox.account_home("orbit-no-such-account") is None
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
        ([], "no ARM64 Tart worker"),
        ([{"name": "mac-1", "arch": "amd64", "runtime": "tart"}], "ARM64 Tart worker"),
        ([{"name": "mac-1", "arch": "arm64", "runtime": "docker"}], "ARM64 Tart worker"),
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
        ({"scheduling_paused": True}, "taking VMs"),
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
    with pytest.raises(ProviderStartError, match="no ARM64 Tart worker"):
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
    assert api.frames[0][0].runtime_ref == REF
    assert resolved == [reference]
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
    assert relays(guest) == [[host, "4567", "7412"]]
    route_calls = [c for c in guest.commands if c[0][0] == "/sbin/route"]
    assert len(route_calls) == 1
    assert route_calls[0][0] == ["/sbin/route", "-n", "get", "-inet", "default"]
    assert route_calls[0][1]["run_as"] == "user"
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_no_model_gateway_needs_no_route_discovery(backend):
    provider, _, guest = backend
    guest.route = (1, "", "route unavailable", False)
    sandbox = await provider.create(await request(provider))
    assert sandbox.gateway_url is None
    assert not any(c[0][0] == "/sbin/route" for c in guest.commands)
    assert not relays(guest)
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
    with pytest.raises(ProviderStartError, match="default IPv4 route"):
        await provider.create(await request(provider, gateway_url="http://0.0.0.0:4567"))
    assert len(api.deleted) == 1


@pytest.fixture
def softnet(monkeypatch):
    """A Mac whose local Orchard changes a running VM's Softnet rules in place."""
    checked = []

    async def check(local):
        checked.append(local)

    monkeypatch.setattr(orchard, "check_live_policy", check)
    monkeypatch.setattr(orchard, "check_softnet", lambda: None)
    return checked


async def test_restricted_modes_are_declared_only_where_softnet_is_the_workers(monkeypatch) -> None:
    """A Fleet places Tasks by the declared modes; a Mac without Softnet refuses them early,
    saying what to run."""

    def missing() -> None:
        raise ProviderCapabilityError("restricted macOS networking needs Softnet; run setup")

    monkeypatch.setattr(orchard, "check_softnet", missing)
    provider = orchard.OrchardProvider(SimpleNamespace())  # type: ignore[arg-type]
    (profile,) = await provider.profiles()
    assert profile.network_modes == {NetworkMode.OPEN}
    needs = SandboxNeeds(
        kind=ImageKind.VM,
        os=OperatingSystem.MACOS,
        arch=Arch.ARM64,
        network_mode=NetworkMode.BLOCK,
        resources=Resources(),
    )
    assert await provider.unserved(needs, user="user", role=SandboxRole.SOLVER) == (
        "network block is not one it applies (open)",
        "restricted macOS networking needs Softnet; run setup",
    )
    monkeypatch.setattr(orchard, "check_softnet", lambda: None)
    (profile,) = await provider.profiles()
    assert profile.network_modes == set(NetworkMode)


async def test_a_solver_is_refused_by_its_snapshot_verifiers_own_rules(monkeypatch) -> None:
    """The leaf that saves a solver's snapshot boots its snapshot verifier too, so the
    solver is refused by the rules that would refuse the verifier, as the Fleet refuses it
    by its leaves': here a verifier's storage in no whole GiB."""
    monkeypatch.setattr(orchard, "check_softnet", lambda: None)
    provider = orchard.OrchardProvider(SimpleNamespace())  # type: ignore[arg-type]
    macos = {"kind": ImageKind.VM, "os": OperatingSystem.MACOS, "arch": Arch.ARM64}
    verifier = SandboxNeeds(
        **macos,
        network_mode=NetworkMode.OPEN,
        resources=Resources(storage_mb=1536),
        snapshot=True,
    )
    needs = SandboxNeeds(
        **macos,
        network_mode=NetworkMode.OPEN,
        resources=Resources(storage_mb=4096),
        snapshot=True,
        verifier=verifier,
    )
    refused = ("the verifier booted from its snapshot: Orchard storage_mb must be a whole GiB",)
    assert await provider.unserved(needs, user="user", role=SandboxRole.SOLVER) == refused
    fleet = FleetProvider(SimpleNamespace())  # type: ignore[arg-type]
    fleet._profiles = await provider.profiles()
    assert await fleet.unserved(needs, user="user", role=SandboxRole.SOLVER) == refused


BLOCK = NetworkPolicy(mode=NetworkMode.BLOCK)
ALLOWLIST = NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",))
BLOCKLIST = NetworkPolicy(mode=NetworkMode.BLOCKLIST, blocked_hosts=("example.com",))
OPEN_RULES = (["0.0.0.0/0"], [])


@pytest.mark.asyncio
async def test_a_restricted_vm_starts_open_on_softnet_and_is_sealed_outside_it(backend, softnet):
    """The rules change in place, on new guest sessions over a new SSH connection."""
    provider, api, guest = backend
    api.lag = 2
    sandbox = await provider.create(
        await request(provider, network=BLOCK, gateway_url="http://0.0.0.0:4567/v1")
    )
    name = sandbox.sandbox_id
    created = api.created[0]
    assert created["netSoftnet"] is True
    assert (created["netSoftnetAllow"], created["netSoftnetBlock"]) == OPEN_RULES
    assert len(softnet) == 1
    api.events.clear()
    await sandbox.open_egress()  # setup's, on a VM that is open already
    assert api.events == []

    closes = guest.closes
    await sandbox.close_egress()
    sealed = (["in proto tcp @host port 22", "out proto tcp @host port 4567"], ["0.0.0.0/0"])
    replaced = [("connect", name), ("connect", name)]
    assert api.events == [("end-ssh", name), ("policy", *sealed), *replaced]
    assert guest.closes == closes + 2
    # The PUT replaces the whole VMSpec: everything else in it is forwarded unchanged.
    assert {key: api.put[key] for key in ("os", "arch", "runtime", "netSoftnet")} == {
        "os": "darwin",
        "arch": "arm64",
        "runtime": "tart",
        "netSoftnet": True,
    }
    assert api.put["powerState"] == "running" and "uid" not in api.put
    await sandbox.exec(["true"], identity=Identity.AGENT)
    assert guest.commands[-1][1]["env"] is None and guest.relays == []

    api.events.clear()
    await sandbox.open_egress()
    assert api.events == [("end-ssh", name), ("policy", *OPEN_RULES), *replaced]
    await sandbox.destroy()
    assert api.deleted == [name]


@pytest.mark.asyncio
@pytest.mark.parametrize("network", [ALLOWLIST, BLOCKLIST])
async def test_a_sealed_agent_under_a_list_reaches_its_proxy_through_the_relay(
    backend, softnet, network: NetworkPolicy
):
    provider, api, guest = backend
    host = "192.168.70.1"
    guest.route = (0, f"gateway: {host}\n", "", False)
    sandbox = await provider.create(
        await request(
            provider,
            network=network,
            gateway_url="http://0.0.0.0:4567/v1",
            proxy_url="http://0.0.0.0:4568",
            proxy_token="token",
        )
    )
    await sandbox.close_egress()
    assert api.record["netSoftnetAllow"] == [
        "in proto tcp @host port 22",
        "out proto tcp @host port 4567",
        "out proto tcp @host port 4568",
    ]
    assert guest.relays == [(f"http://token:@{host}:4568", [host, "localhost", "127.0.0.1"], True)]
    await sandbox.exec(["curl"], identity=Identity.AGENT)
    env = guest.commands[-1][1]["env"]
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:41000" and host in env["NO_PROXY"].split(",")
    # A verifier's command too, under its own allowlist.
    await sandbox.exec(["true"])
    assert guest.commands[-1][1]["env"]["HTTPS_PROXY"] == "http://127.0.0.1:41000"

    await sandbox.open_egress()
    assert guest.relays[-1] == (None, [], False)
    assert (api.record["netSoftnetAllow"], api.record["netSoftnetBlock"]) == OPEN_RULES
    await sandbox.exec(["curl"], identity=Identity.AGENT)
    assert guest.commands[-1][1]["env"] is None
    await sandbox.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["close_egress", "open_egress"])
@pytest.mark.parametrize(
    ("fallback", "message"),
    [("stopped", "instead of changing it in place"), ("rebooted", "restarted")],
)
async def test_a_vm_the_worker_restarts_for_its_rules_fails_the_change(
    backend, softnet, change, fallback, message
):
    """A worker that cannot change the rules in place stops and starts the VM instead."""
    provider, api, _ = backend
    sandbox = await provider.create(await request(provider, network=BLOCK))
    if change == "open_egress":
        await sandbox.close_egress()
    api.fallback = fallback
    with pytest.raises(ProviderStartError, match=message):
        await getattr(sandbox, change)()
    assert api.deleted == []  # the episode's teardown deletes it, as after any failed seal
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_rules_the_worker_does_not_apply_fail_the_seal(backend, softnet, monkeypatch):
    provider, api, _ = backend
    sandbox = await provider.create(await request(provider, network=BLOCK))
    api.lag = 10**6
    monkeypatch.setattr(orchard_client, "POLICY_TIMEOUT_SEC", 0.05)
    with pytest.raises(ProviderStartError, match="did not apply"):
        await sandbox.close_egress()
    await sandbox.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        OrchardExecResult(0, b"", b""),
        OrchardExecResult(1, b"", b"sudo: a password is required"),
        orchard_client.ExecConnectionError("Orchard exec connection failed"),
    ],
    ids=["exited", "sudo-refused", "never-connected"],
)
async def test_an_ssh_connection_the_command_did_not_end_fails_the_seal(
    backend, softnet, monkeypatch, outcome
):
    """Only the session Orchard reports lost with its connection shows the command ended
    it; an exit of any kind, or no session at all, would leave it to strand the next."""
    provider, api, _ = backend
    sandbox = await provider.create(await request(provider, network=BLOCK))

    async def survived(name, argv, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(api, "exec", survived)
    with pytest.raises(ProviderStartError, match="SSH connection"):
        await sandbox.close_egress()
    assert not any(event[0] == "policy" for event in api.events)
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_an_open_task_keeps_tarts_own_network(backend, softnet):
    provider, api, _ = backend
    sandbox = await provider.create(await request(provider))
    assert "netSoftnet" not in api.created[0] and softnet == []
    await sandbox.close_egress()
    await sandbox.open_egress()
    assert not any(event[0] in {"policy", "end-ssh"} for event in api.events)
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_restricted_networking_is_refused_before_any_vm_without_softnet(
    backend, monkeypatch, tmp_path
):
    provider, api, _ = backend
    monkeypatch.setattr(orchard_client, "SOFTNET", tmp_path / "softnet")
    with pytest.raises(ProviderCapabilityError, match=r"setup_macos\.py"):
        await provider.create(await request(provider, network=BLOCK))
    assert not api.created and not api.frames


def test_softnet_must_be_setups_root_owned_set_uid_program(monkeypatch, tmp_path):
    """Tart starts it as root only so, and it must be the ``softnet`` the worker finds."""
    program = tmp_path / "libexec/softnet"
    root = tmp_path / "state"
    (root / "worker-bin").mkdir(parents=True)

    class RootOwned(type(program)):
        def stat(self, **kwargs):
            return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | stat.S_ISUID | 0o755)

    monkeypatch.setattr(orchard_client, "SOFTNET", program)
    with pytest.raises(ProviderCapabilityError, match="Softnet"):
        orchard_client.check_softnet(root)
    program.parent.mkdir()
    program.write_bytes(b"softnet")
    program.chmod(0o4755)
    with pytest.raises(ProviderCapabilityError, match="Softnet"):
        orchard_client.check_softnet(root)  # not the worker's
    (root / "worker-bin/softnet").symlink_to(program)
    with pytest.raises(ProviderCapabilityError, match="Softnet"):
        orchard_client.check_softnet(root)  # this account's, not root's
    monkeypatch.setattr(orchard_client, "SOFTNET", RootOwned(program))
    orchard_client.check_softnet(root)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("controller", "tart", "refused"),
    [
        ("0.58.0", "2.41.1", None),
        ("0.57.0", "2.34.0", None),
        ("0.56.1", "2.41.1", "Orchard 0.57.0"),
        ("unknown", "2.41.1", "Orchard 0.57.0"),
        ("0.58.0", "2.33.0", "Tart 2.34.0"),
    ],
)
async def test_restricted_networking_needs_policy_changes_in_place(
    monkeypatch, controller, tart, refused
):
    """The running controller and the worker's Tart, not the binaries setup pins."""
    monkeypatch.setattr(orchard_client, "check_softnet", lambda: None)
    api = FakeOrchard()
    api.controller_info = AsyncMock(return_value={"version": controller})

    async def tart_version(*args, **kwargs):
        assert args == ("--version",)
        return tart

    local = SimpleNamespace(client=lambda: api, tart=tart_version)
    if refused is None:
        await orchard_client.check_live_policy(local)
    else:
        with pytest.raises(ProviderCapabilityError, match=refused):
            await orchard_client.check_live_policy(local)
    assert api.closed


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
    assert removed[0].startswith("/opt/orbit/screenshot-")
    if failure in ("oversize", "capture"):
        assert not reads


@pytest.mark.asyncio
async def test_large_screenshot_round_trip_over_bounded_guest_responses(
    backend, monkeypatch, tmp_path
):
    import json
    import random
    import struct
    import sys
    import zlib

    from orbit.run.guestd import gui
    from orbit.run.guestd.main import Handler
    from orbit.run.transport import GuestClient

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    # Random pixels barely compress, so the PNG is several megabytes.
    width, height = 1400, 1200
    pixels = random.Random(1).randbytes(width * height * 3)
    rows = b"".join(b"\0" + pixels[y * width * 3 : (y + 1) * width * 3] for y in range(height))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 1))
        + chunk(b"IEND", b"")
    )
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


def elevation_changes(guest):
    return [argv for argv, _ in guest.commands if privileges._SET in argv]


@pytest.mark.parametrize(
    ("sudo", "role", "change"),
    [
        (True, SandboxRole.SOLVER, "true"),
        (False, SandboxRole.SOLVER, "false"),
        (True, SandboxRole.VERIFIER, None),
        (False, SandboxRole.VERIFIER, None),
    ],
)
async def test_sudo_is_granted_or_taken_away_per_boot(backend, sudo, role, change):
    # As on Linux: the root guestd sets the last sudoers rule on the framework's Python.
    provider, api, guest = backend
    guest.sudo = sudo
    sandbox = await provider.create(await request(provider, sudo=sudo, role=role))
    expected = [orchard.MACOS_PYTHON, "-I", "-c", privileges._SET, "user", change]
    assert elevation_changes(guest) == ([expected] if change else [])
    assert api.frames == [(sandbox.request.prepared_image, "user")]
    await sandbox.destroy()


async def test_the_control_account_cannot_run_a_task_without_sudo(backend):
    provider, api, _ = backend
    with pytest.raises(ProviderCapabilityError, match=r"must declare \[agent\]\.sudo = true"):
        await provider.create(await request(provider, user=orchard.CONTROL_ACCOUNT))
    assert not api.created
    await provider.accepts(
        await request(provider, user=orchard.CONTROL_ACCOUNT, role=SandboxRole.VERIFIER)
    )
    await provider.accepts(await request(provider, user=orchard.CONTROL_ACCOUNT, sudo=True))


async def test_a_build_vm_changes_nothing_for_the_agent_and_keeps_its_disk(backend, monkeypatch):
    provider, api, guest = backend
    argvs = []
    connect_guest = api.connect_guest

    async def connect(name, argv, *, deadline):
        argvs.append(argv)
        return await connect_guest(name, argv, deadline=deadline)

    monkeypatch.setattr(api, "connect_guest", connect)
    sandbox = await provider.create_builder(await request(provider, sudo=False))
    assert not elevation_changes(guest)
    assert not api.desktops
    assert not any(f"ORBIT_CUA_DRIVER={orchard.MACOS_DRIVER}" in argv for argv in argvs)
    # Agent commands search this PATH too, so it names no directory only root may enter;
    # a lookup through one fails with EACCES instead of "not found".
    assert argvs and all("PATH=/usr/bin:/bin:/usr/sbin:/sbin" in argv for argv in argvs)
    saved = []
    shut_down = AsyncMock()
    monkeypatch.setattr(
        sandbox,
        "_local",
        SimpleNamespace(
            save=AsyncMock(side_effect=lambda *a: saved.append(a)), shut_down=shut_down
        ),
    )
    image = sandbox.request.prepared_image.model_copy(update={"runtime_ref": "orbit-built-x"})
    await sandbox.save_image(image)
    # The guest shuts itself down, so everything the build wrote is on the kept disk.
    shut_down.assert_awaited_once_with(api, sandbox._owned)
    assert saved and saved[0][1] == "orbit-built-x"
    await sandbox.destroy()


@pytest.mark.parametrize(
    ("role", "probed"), [(SandboxRole.SOLVER, True), (SandboxRole.VERIFIER, False)]
)
async def test_an_agent_waits_for_its_desktop(backend, monkeypatch, role, probed):
    provider, api, guest = backend
    guest.sudo = True
    argvs = []
    connect_guest = api.connect_guest

    async def connect(name, argv, *, deadline):
        argvs.append(argv)
        return await connect_guest(name, argv, deadline=deadline)

    monkeypatch.setattr(api, "connect_guest", connect)
    guest.screen = (1366, 768)
    wanted = DesktopResolution(width=1366, height=768)
    sandbox = await provider.create(
        await request(provider, sudo=True, role=role, desktop_resolution=wanted)
    )
    assert api.desktops == ([("user", 501)] if probed else [])
    driver = f"ORBIT_CUA_DRIVER={orchard.MACOS_DRIVER}"
    assert all((driver in argv) is probed for argv in argvs)
    # Every VM boots the framed image's clone for the Run's resolution; the agent's
    # desktop is checked to have it, and the image the RunLock records stays the same.
    assert api.created[-1]["image"] == f"{FRAMED.name}-1366x768"
    assert sandbox.allocation.desktop_resolution == (wanted if probed else None)
    assert sandbox.resolved_image.observed_ref == FRAMED.name
    await sandbox.destroy()


async def test_a_desktop_of_another_size_deletes_its_vm(backend):
    provider, api, guest = backend
    guest.sudo = True
    guest.screen = (1024, 768)
    wanted = DesktopResolution(width=1366, height=768)
    with pytest.raises(ProviderCapabilityError, match="1024x768, not the requested 1366x768"):
        await provider.create(await request(provider, sudo=True, desktop_resolution=wanted))
    assert api.deleted == [api.created[-1]["name"]]


async def test_framing_requires_the_installed_framer(monkeypatch, tmp_path):
    framer = tmp_path / "framework-macos"
    monkeypatch.setattr(orchard_client, "FRAMER", framer)
    with pytest.raises(ProviderStartError, match=r"setup_macos\.py"):
        orchard_client.check_framer()
    framer.write_bytes(orchard_client.MACOS_FRAMER.read_bytes() + b"# changed\n")
    with pytest.raises(ProviderStartError, match="differs"):
        orchard_client.check_framer()
    framer.write_bytes(orchard_client.MACOS_FRAMER.read_bytes())
    orchard_client.check_framer()


class FakeMac(FakeOrchard):
    """A Mac's local Orchard that runs several VMs, and its local Tart images.

    A VM starts with its image's files and display, as a Tart clone does, and saving a
    stopped VM keeps them as an image.
    """

    def __init__(self):
        super().__init__()
        self.vms = {}
        self.guests = {}
        self.images = {FRAMED.name: {}}
        self.displays = {FRAMED.name: (1024, 768)}
        self.frames = []
        self.tarts = []
        self.events = []

    def client(self):
        return self

    async def get_vm(self, name):
        return self.vms.get(name)

    async def list_vms(self):
        return list(self.vms.values())

    async def create_vm(self, spec):
        self.created.append(spec)
        name, image = spec["name"], spec["image"]
        self.vms[name] = dict(spec) | {
            "uid": f"uid-{len(self.created)}",
            "status": "running",
            "powerState": "running",
            "generation": 0,
            "assignedCPU": spec["cpu"],
            "assignedMemory": spec["memory"],
        }
        guest = self.guests[name] = FakeGuest(None)
        guest.files, guest.screen = dict(self.images[image]), self.displays[image]
        return self.vms[name]

    async def update_vm(self, name, spec):
        vm = self.vms[name]
        if spec.get("powerState") != "stopped":
            vm.update(spec, generation=vm["generation"] + 1)
            vm["observedGeneration"] = vm["generation"]
            return dict(vm)
        stopped = [{"type": "running", "state": "false"}]
        vm.update(spec, generation=2, observedGeneration=2, conditions=stopped)
        return vm

    async def delete_vm(self, name):
        self.deleted.append(name)
        del self.vms[name]

    async def exec(self, name, argv, **kwargs):
        self.events.append(("exec", name, list(argv)))
        if "kill -TERM" in argv[-1]:
            raise GuestUnreachableError("the command's connection ended")
        return OrchardExecResult(0, b"", b"")

    async def connect_guest(self, name, argv, *, deadline=None):
        return name

    async def local_images(self):
        return set(self.images)

    async def tart(self, command, *args, **kwargs):
        self.tarts.append((command, *args))
        if command == "clone":
            self.images[args[1]] = dict(self.images[args[0]])
            self.displays[args[1]] = self.displays[args[0]]
        elif command == "set":
            width, height = args[2].removesuffix("pt").split("x")
            self.displays[args[0]] = (int(width), int(height))
        elif command == "rename":
            self.images[args[1]] = self.images.pop(args[0])
            self.displays[args[1]] = self.displays.pop(args[0])
        elif command == "delete":
            del self.images[args[0]], self.displays[args[0]]
        return ""

    async def shut_down(self, client, owned):
        self.events.append(("shut_down", owned.name))
        owned.stopped = True

    async def save(self, owned, name):
        assert owned.stopped
        self.events.append(("save", owned.name, name))
        guest = self.guests[owned.name]
        self.images[name], self.displays[name] = dict(guest.files), guest.screen


@pytest.fixture
def mac(backend, monkeypatch, tmp_path):
    """The Provider on a Mac whose local Orchard runs several VMs, records retention and
    keeps local images, which it clones at each resolution as framing does."""
    fake = FakeMac()

    async def frame(local, image, *, user):
        fake.frames.append((image, user))
        return FRAMED

    monkeypatch.setattr(orchard_client, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(orchard_framing, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(orchard, "frame_image", frame)
    monkeypatch.setattr(orchard, "at_resolution", orchard_framing.at_resolution)
    monkeypatch.setattr(orchard, "GuestClient", lambda name, **kwargs: fake.guests[name])
    return orchard.OrchardProvider(fake), fake


async def saved_snapshot(provider):
    """A snapshot of a VM that wrote ``work.txt`` in its account's home."""
    source = await provider.create(await request(provider, retention="snapshot"))
    await source.write_file("/Users/user/work.txt", b"saved")
    saved = await source.snapshot(roles=("solver",), reason="saved")
    await source.destroy()
    return source, saved


@pytest.mark.asyncio
async def test_a_kept_vm_is_listed_continued_and_destroyed(mac):
    provider, fake = mac
    sandbox = await provider.create(
        await request(provider, retention="keep", gateway_url="http://127.0.0.1:9001")
    )
    retained = await sandbox.retain(roles=("solver",), reason="kept")
    assert (retained.provider, retained.handle) == ("orchard", sandbox.handle)
    assert retained.cleanup_command == f"orbit sandbox destroy {sandbox.handle}"
    assert fake.deleted == []
    listed = await provider.list_retained()
    assert [(item["handle"], item["running"]) for item in listed] == [(sandbox.handle, True)]

    # A later episode, with its own Gateway, continues in it.
    later = (await request(provider, gateway_url="http://127.0.0.1:9002")).model_copy(
        update={"episode_id": "episode-2"}
    )
    continued = await provider.attach_retained(
        sandbox.handle, later, sandbox.allocation, sandbox.resolved_image
    )
    guest = fake.guests[sandbox.sandbox_id]
    stops = [c for c in guest.commands if c[0] == ["/bin/sh", "-c", orchard._STOP_GATEWAY_RELAY]]
    assert len(stops) == 1
    assert relays(guest)[-1][1] == "9002"
    with pytest.raises(ProviderCapabilityError, match="does not run an image recorded here"):
        other = sandbox.resolved_image.model_copy(
            update={"observed_identity": "sha256:" + "0" * 64}
        )
        await provider.attach_retained(sandbox.handle, later, sandbox.allocation, other)
    # It keeps the VM again, which stays recorded as the episode that first kept it.
    again = await continued.retain(roles=("solver",), reason="kept again")
    assert (again.handle, again.episode_id) == (sandbox.handle, "episode-2")
    assert [item["episode"] for item in await provider.list_retained()] == ["episode-1"]

    await provider.destroy_retained(sandbox.handle)
    assert fake.deleted == [sandbox.sandbox_id]
    assert await provider.list_retained() == []
    with pytest.raises(ProviderCapabilityError, match="not a retained Orchard solver VM"):
        await provider.attach_retained(
            sandbox.handle, later, sandbox.allocation, sandbox.resolved_image
        )


@pytest.mark.asyncio
async def test_a_sealed_vm_continued_in_relays_to_this_runs_proxy(mac, softnet):
    """Its rules say it is sealed; the relay is this run's, until this run's setup opens it."""
    provider, fake = mac
    first = await request(
        provider,
        retention="keep",
        network=ALLOWLIST,
        gateway_url="http://127.0.0.1:9001",
        proxy_url="http://0.0.0.0:9002",
        proxy_token="first",
    )
    sandbox = await provider.create(first)
    await sandbox.close_egress()
    retained = await sandbox.retain(roles=("solver",), reason="kept")
    later = first.model_copy(
        update={
            "gateway_url": "http://127.0.0.1:9101",
            "proxy_url": "http://0.0.0.0:9102",
            "proxy_token": "later",
        }
    )
    attached = await provider.attach_retained(
        retained.handle, later, sandbox.allocation, sandbox.resolved_image
    )
    guest = fake.guests[sandbox.sandbox_id]
    assert guest.relays[-1][0] == "http://later:@192.168.64.1:9102"
    await attached.exec(["curl"], identity=Identity.AGENT)
    assert guest.commands[-1][1]["env"]["HTTPS_PROXY"] == "http://127.0.0.1:41000"
    await attached.open_egress()
    assert guest.relays[-1] == (None, [], False)
    assert fake.vms[sandbox.sandbox_id]["netSoftnetBlock"] == []
    # Sealed again by this run, the rules name this run's Gateway and proxy, not the first's.
    await attached.close_egress()
    allowed = fake.vms[sandbox.sandbox_id]["netSoftnetAllow"]
    assert "out proto tcp @host port 9101" in allowed
    assert "out proto tcp @host port 9102" in allowed
    assert not any(rule.endswith(("9001", "9002")) for rule in allowed)
    await provider.destroy_retained(retained.handle)


@pytest.mark.asyncio
async def test_a_snapshot_of_a_sealed_vm_gives_the_account_its_settings_back_first(mac, softnet):
    provider, fake = mac
    sandbox = await provider.create(
        await request(
            provider,
            retention="snapshot",
            network=ALLOWLIST,
            proxy_url="http://0.0.0.0:9002",
            proxy_token="token",
        )
    )
    await sandbox.close_egress()
    guest = fake.guests[sandbox.sandbox_id]
    shut_down = fake.shut_down
    relays_at_shutdown = []

    async def recorded(client, owned):
        relays_at_shutdown.append(list(guest.relays))
        await shut_down(client, owned)

    fake.shut_down = recorded
    await sandbox.snapshot(roles=("solver",), reason="saved")
    assert relays_at_shutdown[0][-1] == (None, [], False)
    await sandbox.destroy()


@pytest.mark.asyncio
async def test_a_kept_verifier_is_not_continued_in(mac):
    provider, fake = mac
    verifier = await provider.create(
        await request(provider, role=SandboxRole.VERIFIER, retention="keep")
    )
    retained = await verifier.retain(roles=("verifier",), reason="kept")
    with pytest.raises(ProviderCapabilityError, match="not a retained Orchard solver VM"):
        await provider.attach_retained(
            retained.handle, await request(provider), verifier.allocation, verifier.resolved_image
        )
    await provider.destroy_retained(retained.handle)
    assert fake.deleted == [verifier.sandbox_id]


@pytest.mark.asyncio
async def test_a_vm_deleted_outside_orbit_is_no_longer_listed(mac):
    provider, fake = mac
    sandbox = await provider.create(await request(provider, retention="keep"))
    await sandbox.retain(roles=("solver",), reason="kept")
    del fake.vms[sandbox.sandbox_id]
    assert await provider.list_retained() == []
    with pytest.raises(ProviderCapabilityError):
        await provider.destroy_retained(sandbox.handle)


@pytest.mark.asyncio
async def test_a_snapshot_boots_later_vms_with_its_files(mac):
    provider, fake = mac
    source, saved = await saved_snapshot(provider)
    snapshot_id = source.sandbox_id.removeprefix("orbit-")
    image = f"orbit-snapshot-{snapshot_id}"
    assert saved.handle == f"orchard:snapshot:{snapshot_id}"
    # The display mode the desktop saved is forgotten before the guest shuts down, so each
    # VM booted from the snapshot starts at its own resolution.
    assert fake.events[-3:] == [
        ("exec", source.sandbox_id, ["sudo", "-n", *orchard_framing.FORGET_DISPLAYS]),
        ("shut_down", source.sandbox_id),
        ("save", source.sandbox_id, image),
    ]
    frames = len(fake.frames)

    wide = DesktopResolution(width=1366, height=768)
    booted = [
        await provider.create(await request(provider, snapshot=saved.handle)),
        await provider.create(
            await request(provider, snapshot=saved.handle, desktop_resolution=wide)
        ),
    ]
    assert [vm["image"] for vm in fake.created[-2:]] == [f"{image}-1024x768", f"{image}-1366x768"]
    assert len(fake.frames) == frames  # booting a snapshot frames nothing
    for each in booted:
        assert each.resolved_image.observed_ref == image
        assert await each.read_file("/Users/user/work.txt") == b"saved"
    # A crashed process's VM booted from it is driven again from the same record.
    again = await provider.reattach(
        booted[0].handle, booted[0].request, booted[0].allocation, booted[0].resolved_image
    )
    assert await again.read_file("/Users/user/work.txt") == b"saved"
    await again.destroy()
    await booted[1].destroy()
    assert set(fake.deleted) == {source.sandbox_id, *(each.sandbox_id for each in booted)}

    other = await provider.prepare_image(
        ImageRef(kind=ImageKind.VM, reference=REF.replace("a" * 64, "c" * 64))
    )
    with pytest.raises(ProviderCapabilityError, match="another Task image or account"):
        await provider.create(
            (await request(provider, snapshot=saved.handle)).model_copy(
                update={"prepared_image": other}
            )
        )


@pytest.mark.asyncio
async def test_a_snapshot_goes_with_its_clones_once_no_vm_boots_from_it(mac, tmp_path):
    provider, fake = mac
    source, saved = await saved_snapshot(provider)
    image = f"orbit-snapshot-{source.sandbox_id.removeprefix('orbit-')}"
    wide = DesktopResolution(width=1366, height=768)
    await (await provider.create(await request(provider, snapshot=saved.handle))).destroy()
    booted = await provider.create(
        await request(provider, snapshot=saved.handle, desktop_resolution=wide)
    )
    with pytest.raises(ProviderStartError, match="VMs still boot from"):
        await provider.destroy_retained(saved.handle)
    assert image in fake.images
    await booted.destroy()

    listed = await provider.list_retained()
    assert [(item["handle"], item["image"], item["running"]) for item in listed] == [
        (saved.handle, image, False)
    ]
    fake.tarts.clear()
    await provider.destroy_retained(saved.handle)
    # Its clones go first, so a failure leaves the image, and its record, to try again from.
    assert fake.tarts == [
        ("delete", f"{image}-1024x768"),
        ("delete", f"{image}-1366x768"),
        ("delete", image),
    ]
    assert set(fake.images) == {FRAMED.name, f"{FRAMED.name}-1024x768"}
    locks = tmp_path / "cache" / "macos-frames"
    assert [lock.name for lock in locks.glob("*.lock")] == [f"{FRAMED.name}-1024x768.lock"]
    assert await provider.list_retained() == []
    with pytest.raises(ProviderCapabilityError, match="there is no Orchard snapshot"):
        await provider.create(await request(provider, snapshot=saved.handle))


@pytest.mark.asyncio
async def test_a_kept_vm_booted_from_a_snapshot_is_driven_again_from_its_record(mac):
    provider, _fake = mac
    _source, saved = await saved_snapshot(provider)
    kept = await provider.create(await request(provider, snapshot=saved.handle, retention="keep"))
    await kept.retain(roles=("solver",), reason="kept")
    # A later Run on the Fleet continues in it, by a request that names no snapshot; the
    # Node then restarts and drives it again.
    adopted = kept.request.model_copy(update={"snapshot": None, "episode_id": "episode-2"})
    again = await provider.reattach(kept.handle, adopted, kept.allocation, kept.resolved_image)
    assert await again.read_file("/Users/user/work.txt") == b"saved"
    await again.destroy()
    assert [item["handle"] for item in await provider.list_retained()] == [saved.handle]


def test_guestd_starts_in_the_root_directory_isolated() -> None:
    """Orchard's exec starts it where the session does, which the agent may own."""
    assert orchard._GUESTD.startswith("import os,runpy,sys; os.chdir('/');")
