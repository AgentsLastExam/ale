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
import struct
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import pytest

from orbit.core.errors import (
    GuestUnreachableError,
    MemoryExhaustedError,
    OrbitError,
    ProviderCapabilityError,
    StorageExhaustedError,
)
from orbit.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    DesktopResolution,
    Identity,
    ImageRef,
    PreparedTaskImage,
    Provider,
    Sandbox,
    SandboxRequest,
    SandboxRole,
)
from orbit.core.taskspec import (
    ContainerFeature,
    ImageKind,
    NetworkMode,
    NetworkPolicy,
    OperatingSystem,
    Resources,
)

__all__ = ["ProviderConformance"]

#: An address off every host, reached by IP so no resolver is involved.
INTERNET = "1.1.1.1:443"

#: One process takes memory 8 MiB at a time until the kernel ends it, each a copy of random
#: bytes, which a kernel that compresses memory, as macOS does, cannot shrink.
_ALLOCATE = """
import os
block, held = os.urandom(8 << 20), []
while True:
    held.append(bytearray(block))
"""

#: Processes of 16 MiB of random bytes each, three times the Sandbox's memory of them, which
#: then end.
_MANY = """
import os, signal, sys, time
children = []
for _ in range(int(sys.argv[1]) * 3 // 16):
    child = os.fork()
    if child == 0:
        os.close(1), os.close(2)
        held = bytearray()
        for _ in range(16):
            held += os.urandom(1 << 20)
        time.sleep(600)
        os._exit(0)
    children.append(child)
    time.sleep(0.02)
time.sleep(5)
for child in children:
    try:
        os.kill(child, signal.SIGKILL)
    except ProcessLookupError:
        pass
"""

#: The MiB free to the agent on the filesystem that holds ``$1``.
_FREE = "import shutil, sys; print(shutil.disk_usage(sys.argv[1]).free >> 20)"

#: Writes ``$2`` MiB of random bytes, which no filesystem compresses, to ``$1`` in blocks of
#: 1 MiB, and says how many MiB it wrote: all of them, or as many as fitted before the disk
#: was full.
_FILL = """
import errno, os, sys
block, written = os.urandom(1 << 20), 0
try:
    with open(sys.argv[1], "wb") as target:
        while written < int(sys.argv[2]):
            target.write(block)
            written += 1
        target.flush()
        os.fsync(target.fileno())
except OSError as error:
    if error.errno != errno.ENOSPC:
        raise
    print("full", written)
else:
    print("wrote", written)
"""

#: Reports, per ``kind=address`` argument, whether the sandbox reached it: ``http`` gets an
#: answer, ``tls`` completes a handshake (verified against the address), ``tcp`` connects.
_REACH = """
import socket, ssl, sys, urllib.request
for target in sys.argv[1:]:
    kind, _, address = target.partition("=")
    try:
        if kind == "http":
            urllib.request.urlopen(address, timeout=10).read()
        elif kind == "tls":
            host, port = address.rsplit(":", 1)
            with socket.create_connection((host, int(port)), timeout=10) as raw:
                ssl.create_default_context().wrap_socket(raw, server_hostname=host).close()
        else:
            host, port = address.rsplit(":", 1)
            socket.create_connection((host, int(port)), timeout=10).close()
        print(target, "open")
    except Exception:
        print(target, "closed")
"""


#: A directory of every image of the system that the framework may write, as a volume
#: candidate: macOS's own volume is read-only, and it has no ``/srv``.
_VOLUME = {OperatingSystem.LINUX: "/srv", OperatingSystem.MACOS: "/Users/Shared"}
#: A container image to run inside a Sandbox, pinned so the check stays the same.
_BUSYBOX = "busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
#: What Tasks do with a container engine, once it runs: a container deletes a file of its
#: image, which its layer marks with a whiteout, reaches the Internet, resolves another on a
#: network of their own and reaches the port that one publishes.
NESTED_CHECKS = f"""
set -ex
docker run --rm {_BUSYBOX} sh -c 'rm /bin/ls && ! test -e /bin/ls'
docker run --rm {_BUSYBOX} wget -q -T 30 -O /dev/null http://example.com/
docker network create orbit-nested >/dev/null
docker run -d --name orbit-nested-web --network orbit-nested -p 18080:80 {_BUSYBOX} \\
    sh -c 'mkdir /w && echo served >/w/index.html && exec httpd -f -p 80 -h /w' >/dev/null
docker run --rm --network orbit-nested {_BUSYBOX} wget -q -T 30 -O - http://orbit-nested-web/ \\
    | grep -qx served
docker run --rm --network host {_BUSYBOX} wget -q -T 30 -O - http://127.0.0.1:18080/ \\
    | grep -qx served
docker rm -f orbit-nested-web >/dev/null
docker network rm orbit-nested >/dev/null
echo nested-container-ok
"""
#: Installs Docker from the image's distribution, starts its daemon by hand, and checks it.
_NESTED = f"""
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq docker.io >/dev/null || exit 2
(dockerd >/tmp/dockerd.log 2>&1 &)
for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
{NESTED_CHECKS}
"""


def _png_size(png: bytes) -> tuple[int, int]:
    """A PNG's width and height, from its header."""
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def _shows(png: bytes, resolution: DesktopResolution, operating_system: OperatingSystem) -> bool:
    """Whether a screenshot is of a desktop at ``resolution``.

    A capture is the desktop's size, except on macOS: there the Retina desktop has two
    pixels per point each way and Cua Driver scales its captures down, so only the shape
    can be checked, and the size is what the Provider checked and recorded.
    """
    width, height = _png_size(png)
    if operating_system is not OperatingSystem.MACOS:
        return (width, height) == (resolution.width, resolution.height)
    return abs(width * resolution.height - height * resolution.width) <= max(
        resolution.width, resolution.height
    )


async def _elevates(sandbox: Sandbox) -> bool:
    """Whether the agent can elevate without a password."""
    result = await sandbox.exec(["sh", "-c", "sudo -n true 2>/dev/null"], identity=Identity.AGENT)
    return result.ok


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
    """Assertions every :class:`~orbit.core.sandbox.Provider` must satisfy."""

    provider: Provider
    image: ImageRef | PreparedTaskImage
    """The image every assertion runs on: a pinned release, or one the backend's suite built."""

    gui_image: ImageRef | PreparedTaskImage | None = None
    """An image that starts a desktop, if this backend has one to test with.

    A desktop is a property of the *image*, not of the backend: a provider that can host
    one says so in its profiles, but whether a given sandbox has a screen depends on
    what it was built from. So the GUI assertions need an image that claims a desktop,
    and skip rather than fail where none is configured."""

    framework: str | None = None
    """The framework identity the engine adds for this suite's requests, if it records one.

    Every sandbox carries it in ``/opt/orbit/framework`` whatever image it was handed. The
    suite depends on no engine package, so a backend's suite supplies the value; ``None``
    skips the check."""

    storage_sec: float = 60.0
    """How long after it wrote past its ``storage_mb`` a Sandbox that a Provider holds to it by
    measuring what it writes may go on before it ends."""

    missing_features: (
        Callable[[Sandbox, Iterable[ContainerFeature]], Awaitable[Mapping[ContainerFeature, str]]]
        | None
    ) = None
    """The engine's probe of container features, as a staticmethod: those a Sandbox lacks,
    each with why. The suite depends on no engine package, so a backend's suite supplies it;
    ``None`` checks only ``nested_containers``, by starting a container."""

    async def request(self, **overrides: object) -> SandboxRequest:
        image = overrides.pop("image", self.image)
        assert isinstance(image, ImageRef | PreparedTaskImage)
        base: dict[str, object] = {
            "episode_id": f"conformance-{uuid.uuid4().hex[:8]}",
            "prepared_image": await self.provider.prepare_image(image),
            "resources": Resources(cpus=1, memory_mb=512),
            "network": NetworkPolicy(mode=NetworkMode.BLOCK),
        }
        return SandboxRequest(**(base | overrides))  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_preflight_is_explicit(self) -> None:
        """A provider either works here or says why, before anything is provisioned; what
        only a running Sandbox of it shows, it checks in one."""
        check = await self.provider.preflight()
        if check is not None:
            async with await self.provider.create(await self.request()) as sandbox:
                await check(sandbox)

    @pytest.mark.asyncio
    async def test_declares_profiles(self) -> None:
        profiles = await self.provider.profiles()
        assert profiles, "a provider must declare what it can start"
        for profile in profiles:
            assert profile.network_modes, "a profile must declare at least one network mode"

    @pytest.mark.asyncio
    async def test_gives_every_container_feature_it_declares(self) -> None:
        """A container Sandbox that asks for all the features a profile declares has each,
        as root: what a profile declares is what selection and placement trust."""
        declared = {
            profile.container_features
            for profile in await self.provider.profiles()
            if profile.kind is ImageKind.CONTAINER and profile.container_features
        }
        if self.missing_features is None:
            # Only a container engine shows itself without the suite's probe.
            declared = {
                features for features in declared if ContainerFeature.NESTED_CONTAINERS in features
            }
        if not declared:
            pytest.skip("it declares no container features this suite can show")
        for features in declared:
            nested = ContainerFeature.NESTED_CONTAINERS in features
            request = await self.request(
                resources=Resources(
                    cpus=1, memory_mb=2048, container_features=tuple(sorted(features))
                ),
                # Its container engine and the container's image are installed from the
                # Internet.
                network=NetworkPolicy(mode=NetworkMode.OPEN if nested else NetworkMode.BLOCK),
            )
            async with await self.provider.create(request) as sandbox:
                if self.missing_features is not None:
                    assert await self.missing_features(sandbox, features) == {}
                if nested:
                    result = await sandbox.exec(["sh", "-c", _NESTED], timeout_sec=900)
                    assert "nested-container-ok" in result.stdout, result.stderr[-2000:]

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
            # The agent's home, since a system's temporary directory may be a link
            # (macOS's /tmp), which file operations refuse to pass through.
            path = PurePosixPath(sandbox.agent_home) / "conformance.bin"
            payload = b"\x00binary\xffdata"
            await sandbox.write_file(path, payload)
            assert await sandbox.read_file(path) == payload

    @pytest.mark.asyncio
    async def test_large_replies_arrive_whatever_python_the_image_configures(self) -> None:
        """The guest service runs on the framework's Python whatever the image's variables
        say: under ``PYTHONUNBUFFERED`` a pipe that takes less than one write offers, as
        gVisor's do above 64 KiB, would drop the rest of a reply. (``PYTHONHOME`` would end
        a desktop image's own command, whose session starts Python programs.)"""
        variables = {"PYTHONUNBUFFERED": "1", "PYTHONPATH": "/nonexistent"}
        async with await self.provider.create(await self.request(env=variables)) as sandbox:
            path = PurePosixPath(sandbox.agent_home) / "large.bin"
            payload = bytes(range(256)) * 4096
            await sandbox.write_file(path, payload)
            assert await sandbox.read_file(path) == payload
            printed = await sandbox.exec(["sh", "-c", "head -c 1048576 /dev/zero | tr '\\0' x"])
            assert printed.ok and printed.stdout == "x" * (1 << 20)

    @pytest.mark.asyncio
    async def test_file_reads_progress_while_a_command_is_running(self) -> None:
        async with await self.provider.create(await self.request()) as sandbox:
            directory = str(PurePosixPath(sandbox.agent_home) / "concurrent-read")
            ready = asyncio.Event()

            async def output(stream: str, data: bytes) -> None:
                if stream == "stdout" and b"ready" in data:
                    ready.set()

            command = asyncio.create_task(
                sandbox.exec(
                    [
                        "python3",
                        "-c",
                        "import sys, time; from pathlib import Path; d = Path(sys.argv[1]); "
                        "d.mkdir(); (d / 'session.jsonl').write_bytes(b'first\\nsecond\\n'); "
                        "(d / 'link').symlink_to('/etc/hostname'); "
                        "print('ready', flush=True); time.sleep(5)",
                        directory,
                    ],
                    timeout_sec=15,
                    output_sink=output,
                )
            )
            try:
                await asyncio.wait_for(ready.wait(), timeout=10)
                files = await asyncio.wait_for(sandbox.list_files(directory), timeout=2)
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
            assert sandbox.allocation.provider == self.provider.id
            # The leaf of one of its profiles: its own, or one a scheduler placed it on.
            leaves = {profile.provider for profile in await self.provider.profiles()}
            assert sandbox.allocation.leaf in leaves

    @pytest.mark.asyncio
    async def test_a_sandbox_is_driven_again_by_its_handle(self) -> None:
        """Another Sandbox object drives the same Sandbox by its handle, and ``discard``
        destroys it, idempotently, after which reattaching fails."""
        if type(self.provider).reattach is Provider.reattach:
            pytest.skip(f"{self.provider.name} does not reattach to Sandboxes")
        sandbox = await self.provider.create(await self.request())
        opened = [sandbox]
        try:
            handle = sandbox.handle
            assert handle is not None
            path = PurePosixPath(sandbox.agent_home) / "reattached.bin"
            await sandbox.write_file(path, b"kept")
            recorded = (sandbox.request, sandbox.allocation, sandbox.resolved_image)
            again = await self.provider.reattach(handle, *recorded)
            opened.append(again)
            assert again.handle == handle
            assert await again.read_file(path) == b"kept"
            assert (await again.exec(["echo", "again"])).stdout.strip() == "again"
            await self.provider.discard(handle)
            await self.provider.discard(handle)  # it is gone: nothing to do
            with pytest.raises(OrbitError):
                opened.append(await self.provider.reattach(handle, *recorded))
        finally:
            for each in opened:
                # Destroying what discard destroyed may fail; it still closes connections.
                with contextlib.suppress(OrbitError):
                    await each.destroy()

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
    async def test_a_sandbox_is_held_to_its_storage(self) -> None:
        """A Sandbox that names ``storage_mb`` cannot fill the Host's disk. Either it writes
        to storage of its own no larger than its allocation records, where a write past the
        free space fails as on a full disk and the Sandbox goes on; or it sees more room
        than that, and once it writes past its allocation the Provider ends it within
        :attr:`storage_sec`, its calls raising :class:`StorageExhaustedError`. A Provider
        that does neither refuses the Sandbox."""
        resources = Resources(cpus=1, memory_mb=1024, storage_mb=512)
        try:
            sandbox = await self.provider.create(await self.request(resources=resources))
        except ProviderCapabilityError:
            return
        fill = str(PurePosixPath(sandbox.agent_home) / "fill")
        try:
            allotted = sandbox.allocation.storage_mb
            assert allotted is not None and allotted >= 512
            free = await sandbox.exec(
                ["python3", "-c", _FREE, sandbox.agent_home], identity=Identity.AGENT
            )
            assert free.ok, free.stderr
            own = int(free.stdout) <= allotted
            if own and int(free.stdout) > 4096:
                return  # its own disk bounds it; filling that much proves no more
            size = int(free.stdout) + 64 if own else allotted + 256
            try:
                result = await sandbox.exec(
                    ["python3", "-c", _FILL, fill, str(size)],
                    identity=Identity.AGENT,
                    timeout_sec=600,
                )
            except StorageExhaustedError as ended:
                assert not own and f"{allotted} MiB" in str(ended), ended
                return
            assert result.ok, result.stderr
            outcome, count = result.stdout.split()
            if own:
                assert outcome == "full", f"wrote {count} MiB, past what df said was free"
                assert (await sandbox.exec(["rm", "-f", fill])).ok
                return
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.storage_sec
            while loop.time() < deadline:
                try:
                    await sandbox.exec(["true"], timeout_sec=30)
                except StorageExhaustedError as ended:
                    assert f"{allotted} MiB" in str(ended), ended
                    return
                await asyncio.sleep(1)
            pytest.fail(f"it wrote {count} MiB, past its storage_mb of {allotted}, and went on")
        finally:
            await sandbox.destroy()

    @pytest.mark.asyncio
    async def test_one_image_serves_an_agent_with_and_without_elevation(self) -> None:
        """Elevation is set at every start, never built into the image.

        So one prepared image serves a Task with and without sudo, and its solver and
        verifier alike.
        """
        prepared = await self.provider.prepare_image(self.image)
        for sudo in (True, False):
            request = await self.request(
                prepared_image=prepared,
                resources=Resources(cpus=1, memory_mb=512, sudo=sudo),
                sudo=sudo,
            )
            async with await self.provider.create(request) as sandbox:
                assert sandbox.allocation.sudo is sudo
                assert await _elevates(sandbox) is sudo

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
                "cat /opt/orbit/guestd/main.py >/dev/null 2>&1 && echo read-guestd; "
                "touch /etc/orbit-conformance 2>/dev/null && echo wrote-etc; true"
            )
            escaped = await sandbox.exec(["sh", "-c", probe], identity=Identity.AGENT)
            assert escaped.ok and escaped.stdout.split() == [], escaped.stdout

    @pytest.mark.asyncio
    async def test_a_sandbox_carries_the_engines_framework(self) -> None:
        """Any image a Provider accepts runs the guest service of the engine driving it.

        An image handed straight to a Provider may carry an older guest service or none;
        booted unchanged, it would speak another protocol or could not be driven at all.
        """
        if self.framework is None:
            pytest.skip("this backend's suite names no framework identity")
        async with await self.provider.create(await self.request()) as sandbox:
            recorded = await sandbox.read_file("/opt/orbit/framework")
            assert recorded.decode().strip() == self.framework

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
            (source / "test.sh").write_text("printf stage-ok")
            (source / "link").symlink_to("assets/data.txt")
            async with await self.provider.create(await self.request()) as sandbox:
                target = "/tmp/orbit-conformance-upload"
                for identity, mode in [(Identity.AGENT, 0o777), (Identity.FRAMEWORK, 0o600)]:
                    for path in source.rglob("*"):
                        if not path.is_symlink():
                            path.chmod(0o700 if path.is_dir() else mode)
                    await sandbox.upload_dir(str(source), target, identity=identity)
                    owner = sandbox.agent_user if identity is Identity.AGENT else "root"
                    modes = await sandbox.exec(["python3", "-c", _UPLOAD_MODES, target, owner])
                    assert modes.ok, modes.stderr
                    stage = await sandbox.exec(["sh", f"{target}/test.sh"], identity=identity)
                    assert stage.stdout == "stage-ok", stage.stderr
                    edit = await sandbox.exec(
                        ["sh", "-c", f"echo edit > {target}/assets/data.txt"],
                        identity=Identity.AGENT,
                    )
                    assert edit.ok == (identity is Identity.AGENT), edit.stderr

    @pytest.mark.asyncio
    async def test_a_sealed_sandbox_reaches_its_gateway_and_nothing_else(self) -> None:
        """The isolation claim a ``block`` RunLock makes, across the phase transitions.

        Framework phases have the network; a sealed phase, the agent's or a verifier's,
        reaches only the episode's Gateway, not the Internet and not any other service on the
        Host, and a verifier sealed after the agent stays so; an open phase gets the network
        back, once opening an open Sandbox. The Gateway's address never moves in between,
        and is at the same port whatever port the Host serves it on. Reaching the Internet
        means completing a TLS handshake with a public host: a backend's network may accept a
        connection outside the sandbox and pass nothing on.
        """
        async with _host_services() as (gateway_port, other_port):
            request = await self.request(gateway_url=f"http://127.0.0.1:{gateway_port}")
            async with await self.provider.create(request) as sandbox:
                gateway = sandbox.gateway_url
                assert gateway is not None
                assert urlsplit(gateway).port == SANDBOX_GATEWAY_PORT
                host = urlsplit(gateway).hostname
                await sandbox.open_egress()
                opened = await _reach(sandbox, f"tls={INTERNET}", identity=Identity.FRAMEWORK)
                assert opened[f"tls={INTERNET}"], "framework phases must have the network"

                await sandbox.close_egress()
                # The agent's phase, then a verifier's sealed in the same Sandbox.
                await sandbox.close_egress()
                sealed = await _reach(
                    sandbox, f"http={gateway}", f"tls={INTERNET}", f"tcp={host}:{other_port}"
                )
                assert sealed == {
                    f"http={gateway}": True,
                    f"tls={INTERNET}": False,
                    f"tcp={host}:{other_port}": False,
                }, sealed
                assert sandbox.gateway_url == gateway

                await sandbox.open_egress()
                await sandbox.open_egress()
                reopened = await _reach(sandbox, f"tls={INTERNET}", identity=Identity.FRAMEWORK)
                assert reopened[f"tls={INTERNET}"], "an open phase must get the network back"

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
    async def test_a_reattached_sandbox_reaches_a_later_gateway_at_the_same_address(
        self,
    ) -> None:
        """A later run serves its own Gateway; a kept agent finds it where it found the first."""
        async with _host_services() as (first_port, _), _host_services() as (later_port, _):
            request = await self.request(
                retention="keep", gateway_url=f"http://127.0.0.1:{first_port}"
            )
            sandbox = await self.provider.create(request)
            try:
                retained = await sandbox.retain(roles=("solver",), reason="conformance")
            except ProviderCapabilityError:
                await sandbox.destroy()
                pytest.skip(f"{self.provider.name} does not retain sandboxes")
            try:
                attached = await self.provider.attach_retained(
                    retained.handle,
                    request.model_copy(update={"gateway_url": f"http://127.0.0.1:{later_port}"}),
                    sandbox.allocation,
                    sandbox.resolved_image,
                )
                gateway = attached.gateway_url
                assert gateway == sandbox.gateway_url
                assert (await _reach(attached, f"http={gateway}"))[f"http={gateway}"]
                await attached.retain(roles=("solver",), reason="conformance")
            finally:
                await self.provider.destroy_retained(retained.handle)

    @pytest.mark.asyncio
    async def test_a_snapshot_boots_new_sandboxes_with_its_files(self) -> None:
        """A snapshot keeps the disk, and every Sandbox booted from it starts from that disk.

        Each gets the role, size, network and desktop resolution its own request asks for,
        whatever the snapshot's had, and an agent's also its elevation: an agent's Sandbox
        with and without sudo, and a verifier's, larger and open, as one booted from its
        solver's is.
        """
        if not any(profile.snapshot for profile in await self.provider.profiles()):
            with pytest.raises(ProviderCapabilityError):
                await self.provider.create(await self.request(retention="snapshot"))
            return
        original = await self.request(retention="snapshot", sudo=True)
        sandbox = await self.provider.create(original)
        kept = str(PurePosixPath(sandbox.agent_home) / "kept.txt")
        try:
            await sandbox.write_file(kept, b"kept", identity=Identity.AGENT)
            retained = await sandbox.snapshot(roles=("solver",), reason="conformance")
        finally:
            await sandbox.destroy()
        larger = original.resources.model_copy(
            update={
                "cpus": original.resources.cpus + 1,
                "memory_mb": original.resources.memory_mb + 1024,
            }
        )
        try:
            assert retained.handle.startswith(f"{self.provider.name}:")
            listed = {record["handle"] for record in await self.provider.list_retained()}
            assert retained.handle in listed
            requests = [
                await self.request(
                    snapshot=retained.handle, sudo=True, desktop_resolution=DesktopResolution()
                ),
                await self.request(
                    snapshot=retained.handle,
                    sudo=False,
                    desktop_resolution=DesktopResolution(width=1366, height=768),
                ),
                await self.request(
                    snapshot=retained.handle,
                    role=SandboxRole.VERIFIER,
                    resources=larger,
                    network=NetworkPolicy(mode=NetworkMode.OPEN),
                    desktop_resolution=DesktopResolution(width=1366, height=768),
                ),
            ]
            # The agents' two at once, then the verifier's: a Mac runs no more than two macOS
            # VMs, and a third would wait for room the first two hold.
            for group in (requests[:2], requests[2:]):
                booted = await asyncio.gather(
                    *(self.provider.create(item) for item in group), return_exceptions=True
                )
                forks = [fork for fork in booted if isinstance(fork, Sandbox)]
                try:
                    for fork, request in zip(booted, group, strict=True):
                        if isinstance(fork, BaseException):
                            raise fork
                        assert await fork.read_file(kept) == b"kept"
                        if request.role is not SandboxRole.VERIFIER:
                            # Verification runs as root; nothing is promised of a verifier's
                            # agent account.
                            assert await _elevates(fork) is request.sudo
                        assert fork.allocation.desktop_resolution in (
                            None,
                            request.desktop_resolution,
                        )
                        assert fork.request.role is request.role
                        assert (fork.allocation.cpus, fork.allocation.memory_mb) == (
                            request.resources.cpus,
                            request.resources.memory_mb,
                        )
                        assert fork.allocation.network_mode is request.network.mode
                finally:
                    for fork in forks:
                        await fork.destroy()
        finally:
            await self.provider.destroy_retained(retained.handle)
        listed = {record["handle"] for record in await self.provider.list_retained()}
        assert retained.handle not in listed

    @pytest.mark.asyncio
    async def test_a_volume_is_kept_by_its_snapshot_and_seen_through_views(self) -> None:
        """A Provider that mounts volumes mounts each candidate that is a directory in the
        image: its snapshot keeps it, and a Sandbox booted from the snapshot, or one of its
        own image that mounts the snapshot's volumes, sees it through a view of its own,
        whose writes reach no other Sandbox. One that mounts none leaves them on the disk."""
        path, absent = _VOLUME[(await self.request()).os], "/orbit-conformance-absent"
        kept = f"{path}/kept.txt"
        mounts = any(profile.volumes for profile in await self.provider.profiles())
        saves = any(profile.snapshot for profile in await self.provider.profiles())
        sandbox = await self.provider.create(
            await self.request(volumes=(path, absent), retention="snapshot" if saves else "destroy")
        )
        try:
            assert sandbox.volumes == ((path,) if mounts else ())
            await sandbox.write_file(kept, b"kept")
            if not (mounts and saves):
                assert await sandbox.read_file(kept) == b"kept"
                return
            retained = await sandbox.snapshot(roles=("solver",), reason="conformance")
        finally:
            await sandbox.destroy()
        try:
            booted = await self.provider.create(
                await self.request(volumes=(path, absent), snapshot=retained.handle)
            )
            try:
                assert booted.volumes == (path,)
                viewer = await self.provider.create(
                    await self.request(
                        volumes_from=retained.handle,
                        role=SandboxRole.VERIFIER,
                        network=NetworkPolicy(mode=NetworkMode.OPEN),
                    )
                )
                try:
                    assert viewer.volumes == (path,)
                    assert await booted.read_file(kept) == b"kept"
                    assert await viewer.read_file(kept) == b"kept"
                    await booted.write_file(kept, b"the booted Sandbox's")
                    await viewer.write_file(kept, b"the viewer's")
                    assert await booted.read_file(kept) == b"the booted Sandbox's"
                    assert await viewer.read_file(kept) == b"the viewer's"
                finally:
                    await viewer.destroy()
            finally:
                await booted.destroy()
            # The snapshot holds what it saved, whatever its views wrote.
            async with await self.provider.create(
                await self.request(volumes=(path,), snapshot=retained.handle)
            ) as again:
                assert await again.read_file(kept) == b"kept"
        finally:
            await self.provider.destroy_retained(retained.handle)

    @pytest.mark.asyncio
    async def test_one_process_past_the_sandboxs_memory_dies_alone(self) -> None:
        """The kernel ends the process that takes more than the memory the Task declares, and
        the Sandbox goes on, as its episode does."""
        async with await self.provider.create(await self.request()) as sandbox:
            result = await sandbox.exec(
                ["python3", "-c", _ALLOCATE], identity=Identity.AGENT, timeout_sec=900
            )
            assert not result.ok and not result.timed_out
            assert (await sandbox.exec(["true"])).ok

    @pytest.mark.asyncio
    async def test_a_sandbox_lost_to_its_own_memory_says_so(self) -> None:
        """Processes that together take more than its memory may end the whole Sandbox, or
        the guest service in it, or only themselves; a Sandbox lost after the OOM kills
        raises :class:`MemoryExhaustedError`, never a bare lost connection, which a retry
        answers. Which a Provider's Sandbox does is the kernel's; a backend's own suite
        asserts the loss where it was measured."""
        request = await self.request()
        async with await self.provider.create(request) as sandbox:
            try:
                await sandbox.exec(
                    ["python3", "-c", _MANY, str(request.resources.memory_mb)],
                    identity=Identity.AGENT,
                    timeout_sec=900,
                )
            except GuestUnreachableError as lost:
                assert isinstance(lost, MemoryExhaustedError), lost
                return
            assert (await sandbox.exec(["true"])).ok

    @pytest.mark.asyncio
    async def test_dev_shm_holds_half_the_sandboxs_memory(self) -> None:
        """A Linux Sandbox's ``/dev/shm`` is a tmpfs of about half its memory, as on a Linux
        machine of that memory, where processes that share memory through it, such as
        PyTorch's DataLoader workers, need more than a container engine's 64 MiB. What it
        holds is the Sandbox's memory: a write past it fails as on a full disk, and with it
        full, a process past the rest dies alone and the Sandbox goes on."""
        request = await self.request(resources=Resources(cpus=1, memory_mb=2048))
        # A backend's suite may give its Sandboxes more.
        memory_mb = request.resources.memory_mb
        if request.os is not OperatingSystem.LINUX:
            pytest.skip("only a Linux Sandbox has /dev/shm")
        async with await self.provider.create(request) as sandbox:
            free = await sandbox.exec(["python3", "-c", _FREE, "/dev/shm"], identity=Identity.AGENT)
            assert free.ok, free.stderr
            assert 0.4 * memory_mb <= int(free.stdout) <= 0.6 * memory_mb, free.stdout
            filled = await sandbox.exec(
                ["python3", "-c", _FILL, "/dev/shm/fill", str(memory_mb)],
                identity=Identity.AGENT,
                timeout_sec=600,
            )
            assert filled.ok, filled.stderr
            outcome, count = filled.stdout.split()
            assert outcome == "full" and int(count) >= int(free.stdout) - 8, filled.stdout
            allocated = await sandbox.exec(
                ["python3", "-c", _ALLOCATE], identity=Identity.AGENT, timeout_sec=900
            )
            assert not allocated.ok and not allocated.timed_out
            assert (await sandbox.exec(["true"])).ok

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
    async def test_refuses_up_front_a_container_feature_none_of_its_profiles_gives(
        self,
    ) -> None:
        """What it would refuse to create, it reports as unserved, which a Run selects by
        before anything starts."""
        offered = {
            feature
            for profile in await self.provider.profiles()
            for feature in profile.container_features
        }
        lacking = sorted(set(ContainerFeature) - offered)
        if not lacking:
            pytest.skip("its profiles give every container feature")
        request = await self.request(
            resources=Resources(cpus=1, memory_mb=512, container_features=(lacking[0],))
        )
        assert await self.provider.unserved(request.needs, user=request.user, role=request.role)
        with pytest.raises(ProviderCapabilityError):
            sandbox = await self.provider.create(request)
            await sandbox.destroy()

    @pytest.mark.asyncio
    async def test_never_gives_a_gpu_model_the_task_does_not_accept(self) -> None:
        resources = Resources(gpus=1, gpu_types=("ORBIT-NO-SUCH-GPU",))
        try:
            sandbox = await self.provider.create(await self.request(resources=resources))
        except ProviderCapabilityError:
            return
        await sandbox.destroy()
        pytest.fail(f"a Sandbox accepting only {resources.gpu_types} got {sandbox.allocation.gpu}")

    @pytest.mark.asyncio
    async def test_a_declared_desktop_can_actually_be_used(self) -> None:
        """A provider claiming a desktop is held to it.

        Declaring the capability and not supplying it is the failure mode this suite
        exists to catch, and it is quiet: an image with no graphical session looks
        healthy until the first screenshot, which then reports a display error rather
        than a missing capability.

        Skipped where the provider does not claim a desktop — that is an honest answer.
        """
        if not (self.gui_image and any(profile.gui for profile in await self.provider.profiles())):
            pytest.skip("no desktop image configured for this backend")

        request = await self.request(image=self.gui_image)
        async with await self.provider.create(request) as sandbox:
            png = await sandbox.screenshot()
            assert png.startswith(b"\x89PNG"), "a desktop was declared but produced no image"
            assert len(png) > 1000, "a capture this small is an empty screen, not a desktop"
            # Unset, every backend starts a desktop at one shared default size.
            assert sandbox.allocation.desktop_resolution == DesktopResolution()
            assert _shows(png, DesktopResolution(), request.os), _png_size(png)

    @pytest.mark.asyncio
    async def test_a_desktop_takes_the_runs_resolution(self) -> None:
        """The Run, not the image, decides how large the desktop is.

        The same prepared image serves every resolution, so one the image ignored would
        leave the agent another screen than the one its RunLock records.
        """
        if not (self.gui_image and any(profile.gui for profile in await self.provider.profiles())):
            pytest.skip("no desktop image configured for this backend")

        resolution = DesktopResolution(width=1366, height=768)
        request = await self.request(image=self.gui_image, desktop_resolution=resolution)
        async with await self.provider.create(request) as sandbox:
            assert sandbox.allocation.desktop_resolution == resolution
            png = await sandbox.screenshot()
            assert _shows(png, resolution, request.os), _png_size(png)

    @pytest.mark.asyncio
    async def test_a_declared_desktop_accepts_input(self) -> None:
        """The other half of a desktop: an agent can act on it, not only look at it."""
        if not (self.gui_image and any(profile.gui for profile in await self.provider.profiles())):
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
            assert sandbox.allocation.desktop_resolution is None
            with pytest.raises(Exception):  # noqa: B017 — providers raise their own types
                await sandbox.screenshot()
        finally:
            await sandbox.destroy()
