"""Provider conformance suite.

Subclass, set ``provider``, and the same assertions that keep the container backend
honest apply to any other backend. The suite deliberately tests *contract* behaviour —
what callers are entitled to assume — not implementation details. Each guarantee in
``docs/specs/sandbox-image.md`` that a Provider enforces has its test here, so a backend
that passes this suite gives the same guarantees, not merely the same method names.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import pytest

from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import Identity, ImageRef, Provider, Sandbox, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources

__all__ = ["ProviderConformance"]

#: An address off every host, reached by IP so no resolver is involved.
INTERNET = "1.1.1.1:443"

#: Reports, per ``kind=address`` argument, whether the sandbox reached it.
_REACH = """
import socket, sys, urllib.request
for target in sys.argv[1:]:
    kind, _, address = target.partition("=")
    try:
        if kind == "http":
            urllib.request.urlopen(address, timeout=10).read()
        else:
            host, port = address.rsplit(":", 1)
            socket.create_connection((host, int(port)), timeout=10).close()
        print(target, "open")
    except Exception:
        print(target, "closed")
"""

_UPLOAD_MODES = """
import os, pwd, stat, sys
root, owner = sys.argv[1:]
account = pwd.getpwnam(owner)
for parent, directories, files in os.walk(root):
    for path in [parent, *(os.path.join(parent, name) for name in files)]:
        info = os.lstat(path)
        assert (info.st_uid, info.st_gid) == (account.pw_uid, account.pw_gid), path
        if not stat.S_ISLNK(info.st_mode):
            expected = 0o755 if stat.S_ISDIR(info.st_mode) else 0o644
            assert stat.S_IMODE(info.st_mode) == expected, (path, oct(info.st_mode))
assert os.readlink(os.path.join(root, "link")) == "assets/data.txt"
"""


@contextlib.asynccontextmanager
async def _host_services() -> AsyncIterator[tuple[int, int]]:
    """A stand-in Gateway answering HTTP, and another Host service that must stay unseen."""

    async def gateway(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        writer.close()

    async def other(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    servers = [await asyncio.start_server(gateway, "0.0.0.0", 0)]
    servers.append(await asyncio.start_server(other, "0.0.0.0", 0))
    try:
        yield tuple(server.sockets[0].getsockname()[1] for server in servers)  # type: ignore[misc]
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


async def _reach(
    sandbox: Sandbox, *targets: str, identity: Identity = Identity.AGENT
) -> dict[str, bool]:
    result = await sandbox.exec(
        ["python3", "-c", _REACH, *targets], identity=identity, timeout_sec=90
    )
    assert result.ok, result.stderr
    states = dict(line.rsplit(" ", 1) for line in result.stdout.strip().splitlines())
    return {target: states[target] == "open" for target in targets}


class ProviderConformance:
    """Assertions every :class:`~ale.core.sandbox.Provider` must satisfy."""

    provider: Provider
    image: ImageRef
    """The image every assertion runs on; a backend's suite names a pinned release."""

    gui_image: ImageRef | None = None
    """An image that starts a desktop, if this backend has one to test with.

    A desktop is a property of the *image*, not of the backend: a provider that can host
    one says so in its capabilities, but whether a given sandbox has a screen depends on
    what it was built from. So the GUI assertions need an image that claims a desktop,
    and skip rather than fail where none is configured."""

    async def request(self, **overrides: object) -> SandboxRequest:
        image = overrides.pop("image", self.image)
        assert isinstance(image, ImageRef)
        base: dict[str, object] = {
            "episode_id": f"conformance-{uuid.uuid4().hex[:8]}",
            "prepared_image": await self.provider.prepare_image(image),
            "resources": Resources(cpus=1, memory_mb=512),
            "network": NetworkPolicy(mode=NetworkMode.BLOCK),
        }
        return SandboxRequest(**(base | overrides))  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_preflight_is_explicit(self) -> None:
        """A provider either works here or says why, before anything is provisioned."""
        await self.provider.preflight()

    @pytest.mark.asyncio
    async def test_declares_capabilities(self) -> None:
        caps = self.provider.capabilities()
        assert caps.network_modes, "a provider must declare at least one network mode"
        assert caps.operating_systems

    @pytest.mark.asyncio
    async def test_exec_reports_exit_codes_and_streams(self) -> None:
        async with await self.provider.create(await self.request()) as sandbox:
            result = await sandbox.exec(["echo", "conformance"])
            assert result.ok
            assert "conformance" in result.stdout

            failure = await sandbox.exec(["sh", "-c", "exit 3"])
            assert failure.exit_code == 3

    @pytest.mark.asyncio
    async def test_file_round_trip(self) -> None:
        async with await self.provider.create(await self.request()) as sandbox:
            path = PurePosixPath("/tmp/conformance.bin")
            payload = b"\x00binary\xffdata"
            await sandbox.write_file(path, payload)
            assert await sandbox.read_file(path) == payload

    @pytest.mark.asyncio
    async def test_file_reads_progress_while_a_command_is_running(self) -> None:
        async with await self.provider.create(await self.request()) as sandbox:
            ready = asyncio.Event()

            async def output(stream: str, data: bytes) -> None:
                if stream == "stdout" and b"ready" in data:
                    ready.set()

            command = asyncio.create_task(
                sandbox.exec(
                    [
                        "python3",
                        "-c",
                        "from pathlib import Path; import time; "
                        "Path('/tmp/concurrent-read').mkdir(); "
                        "Path('/tmp/concurrent-read/session.jsonl')"
                        ".write_bytes(b'first\\nsecond\\n'); "
                        "Path('/tmp/concurrent-read/link').symlink_to('/etc/hostname'); "
                        "print('ready', flush=True); time.sleep(5)",
                    ],
                    timeout_sec=15,
                    output_sink=output,
                )
            )
            try:
                await asyncio.wait_for(ready.wait(), timeout=10)
                files = await asyncio.wait_for(
                    sandbox.list_files("/tmp/concurrent-read"), timeout=2
                )
                assert len(files) == 1 and files[0].size == 13
                data = await asyncio.wait_for(
                    sandbox.read_file(files[0].path, offset=6, length=7), timeout=2
                )
                assert data == b"second\n"
                expected = hashlib.sha256(b"first\n").hexdigest()
                assert await sandbox.file_digest(files[0].path, length=6) == f"sha256:{expected}"
                assert await sandbox.list_files(files[0].path) == files
                assert not command.done()
            finally:
                assert (await command).ok

    @pytest.mark.asyncio
    async def test_ready_sandbox_reports_observed_image_and_resources(self) -> None:
        request = await self.request()
        async with await self.provider.create(request) as sandbox:
            assert sandbox.resolved_image.kind is request.prepared_image.kind
            assert sandbox.resolved_image.prepared_identity == (
                request.prepared_image.prepared_identity
            )
            assert sandbox.resolved_image.observed_identity.startswith("sha256:")
            assert sandbox.resolved_image.observed_ref
            assert sandbox.allocation.cpus == request.resources.cpus
            assert sandbox.allocation.memory_mb == request.resources.memory_mb
            assert sandbox.allocation.network_mode is request.network.mode
            assert sandbox.allocation.provider == self.provider.name

    @pytest.mark.asyncio
    async def test_storage_and_sudo_are_enforced_or_rejected(self) -> None:
        resources = Resources(cpus=1, memory_mb=512, storage_mb=512, sudo=True)
        try:
            sandbox = await self.provider.create(await self.request(resources=resources, sudo=True))
        except ProviderCapabilityError:
            return
        try:
            assert sandbox.allocation.storage_mb is not None
            assert sandbox.allocation.storage_mb >= 512
            assert sandbox.allocation.sudo is True
            elevated = await sandbox.exec(["sudo", "-n", "true"], identity=Identity.AGENT)
            assert elevated.ok
        finally:
            await sandbox.destroy()

    @pytest.mark.asyncio
    async def test_the_agent_and_the_framework_are_different_accounts(self) -> None:
        """Agent work runs as the Task's account; framework work runs as root.

        The agent cannot elevate, read the guest service that drives it, or change system
        configuration: any of these would let it alter the conditions it is measured under.
        """
        request = await self.request()
        async with await self.provider.create(request) as sandbox:
            agent = await sandbox.exec(
                ["sh", "-c", 'id -un; printf %s "$HOME"'], identity=Identity.AGENT
            )
            assert agent.ok, agent.stderr
            assert agent.stdout.split("\n", 1) == [request.user, sandbox.agent_home]
            assert sandbox.agent_user == request.user
            framework = await sandbox.exec(["id", "-u"])
            assert framework.stdout.strip() == "0"
            probe = (
                "command -v sudo >/dev/null && sudo -n true 2>/dev/null && echo elevated; "
                "cat /opt/ale/guestd/main.py >/dev/null 2>&1 && echo read-guestd; "
                "touch /etc/ale-conformance 2>/dev/null && echo wrote-etc; true"
            )
            escaped = await sandbox.exec(["sh", "-c", probe], identity=Identity.AGENT)
            assert escaped.ok and escaped.stdout.split() == [], escaped.stdout

    @pytest.mark.asyncio
    async def test_uploads_ignore_host_modes_and_belong_to_their_identity(self) -> None:
        """The same tree lands the same way on every backend.

        Host modes never travel (a Task fetched from object storage has none to give),
        ownership follows the identity, links stay links, and re-uploading over a tree the
        other identity owns replaces it.
        """
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "assets/empty").mkdir(parents=True)
            (source / "assets/data.txt").write_text("input")
            (source / ".hidden").write_text("hidden")
            (source / "run.sh").write_text("printf stage-ok")
            (source / "link").symlink_to("assets/data.txt")
            async with await self.provider.create(await self.request()) as sandbox:
                target = "/tmp/ale-conformance-upload"
                for identity, mode in [(Identity.AGENT, 0o777), (Identity.FRAMEWORK, 0o600)]:
                    for path in source.rglob("*"):
                        if not path.is_symlink():
                            path.chmod(0o700 if path.is_dir() else mode)
                    await sandbox.upload_dir(str(source), target, identity=identity)
                    owner = sandbox.agent_user if identity is Identity.AGENT else "root"
                    modes = await sandbox.exec(["python3", "-c", _UPLOAD_MODES, target, owner])
                    assert modes.ok, modes.stderr
                    stage = await sandbox.exec(["sh", f"{target}/run.sh"], identity=identity)
                    assert stage.stdout == "stage-ok", stage.stderr
                    edit = await sandbox.exec(
                        ["sh", "-c", f"echo edit > {target}/assets/data.txt"],
                        identity=Identity.AGENT,
                    )
                    assert edit.ok == (identity is Identity.AGENT), edit.stderr

    @pytest.mark.asyncio
    async def test_a_sealed_sandbox_reaches_its_gateway_and_nothing_else(self) -> None:
        """The isolation claim a ``block`` RunLock makes, across the phase transitions.

        Framework phases have the network; the agent phase reaches only the episode's
        Gateway, not the Internet and not any other service on the Host; verification
        gets the network back. The Gateway's address never moves in between.
        """
        async with _host_services() as (gateway_port, other_port):
            request = await self.request(gateway_url=f"http://127.0.0.1:{gateway_port}")
            async with await self.provider.create(request) as sandbox:
                gateway = sandbox.gateway_url
                assert gateway is not None
                host = urlsplit(gateway).hostname
                await sandbox.open_egress()
                opened = await _reach(sandbox, f"tcp={INTERNET}", identity=Identity.FRAMEWORK)
                assert opened[f"tcp={INTERNET}"], "framework phases must have the network"

                await sandbox.close_egress()
                sealed = await _reach(
                    sandbox, f"http={gateway}", f"tcp={INTERNET}", f"tcp={host}:{other_port}"
                )
                assert sealed == {
                    f"http={gateway}": True,
                    f"tcp={INTERNET}": False,
                    f"tcp={host}:{other_port}": False,
                }, sealed
                assert sandbox.gateway_url == gateway

                await sandbox.open_egress()
                reopened = await _reach(sandbox, f"tcp={INTERNET}", identity=Identity.FRAMEWORK)
                assert reopened[f"tcp={INTERNET}"], "verification must get the network back"

    @pytest.mark.asyncio
    async def test_a_task_that_declared_open_is_never_sealed(self) -> None:
        request = await self.request(network=NetworkPolicy(mode=NetworkMode.OPEN))
        async with await self.provider.create(request) as sandbox:
            await sandbox.close_egress()
            reached = await _reach(sandbox, f"tcp={INTERNET}")
            assert reached[f"tcp={INTERNET}"], "an open task lost its network"

    @pytest.mark.asyncio
    async def test_a_retained_sandbox_can_be_listed_and_destroyed(self) -> None:
        sandbox = await self.provider.create(await self.request(retention="keep"))
        try:
            retained = await sandbox.retain(roles=("solver",), reason="conformance")
        except ProviderCapabilityError:
            await sandbox.destroy()
            pytest.skip(f"{self.provider.name} does not retain sandboxes")
        try:
            assert retained.provider == self.provider.name
            assert retained.handle.startswith(f"{self.provider.name}:")
            listed = {record["handle"] for record in await self.provider.list_retained()}
            assert retained.handle in listed
        finally:
            await self.provider.destroy_retained(retained.handle)
        listed = {record["handle"] for record in await self.provider.list_retained()}
        assert retained.handle not in listed

    @pytest.mark.asyncio
    async def test_destroy_is_idempotent(self) -> None:
        """Teardown runs on failure paths too, sometimes more than once."""
        sandbox = await self.provider.create(await self.request())
        await sandbox.destroy()
        await sandbox.destroy()

    @pytest.mark.asyncio
    async def test_rejects_a_request_it_cannot_serve(self) -> None:
        try:
            sandbox = await self.provider.create(await self.request(resources=Resources(gpus=1)))
        except ProviderCapabilityError:
            return
        try:
            allocation = sandbox.allocation.gpu
            assert allocation is not None
            assert allocation.requested_count == 1
            assert len(allocation.provider_addresses) == 1
            assert len(allocation.lease_keys) == 1
            assert len(allocation.observed_devices) == 1
        finally:
            await sandbox.destroy()

    @pytest.mark.asyncio
    async def test_a_declared_desktop_can_actually_be_used(self) -> None:
        """A provider claiming a desktop is held to it.

        Declaring the capability and not supplying it is the failure mode this suite
        exists to catch, and it is quiet: an image with no graphical session looks
        healthy until the first screenshot, which then reports a display error rather
        than a missing capability.

        Skipped where the provider does not claim a desktop — that is an honest answer.
        """
        if not (self.provider.capabilities().gui and self.gui_image):
            pytest.skip("no desktop image configured for this backend")

        request = await self.request(image=self.gui_image)
        async with await self.provider.create(request) as sandbox:
            png = await sandbox.screenshot()
            assert png.startswith(b"\x89PNG"), "a desktop was declared but produced no image"
            assert len(png) > 1000, "a capture this small is an empty screen, not a desktop"

    @pytest.mark.asyncio
    async def test_a_declared_desktop_accepts_input(self) -> None:
        """The other half of a desktop: an agent can act on it, not only look at it."""
        if not (self.provider.capabilities().gui and self.gui_image):
            pytest.skip("no desktop image configured for this backend")

        request = await self.request(image=self.gui_image)
        async with await self.provider.create(request) as sandbox:
            applied = await sandbox.inject_input([{"type": "move", "coordinate": [500, 500]}])
            assert applied == 1, "the desktop accepted no input"

    @pytest.mark.asyncio
    async def test_a_missing_desktop_is_reported_not_faked(self) -> None:
        """A sandbox without a screen says so when asked for one.

        Admission does not refuse it: whether a desktop exists belongs to the image, and
        the engine has no business predicting the answer to a question the sandbox can be
        asked directly. What matters is that the answer is an error rather than a blank
        image — an agent can act on the first and cannot tell the second from a dark
        screen.
        """
        if self.image == self.gui_image:
            pytest.skip("this backend has only one image and it carries a desktop")

        sandbox = await self.provider.create(await self.request())  # the headless image
        try:
            with pytest.raises(Exception):  # noqa: B017 — providers raise their own types
                await sandbox.screenshot()
        finally:
            await sandbox.destroy()
