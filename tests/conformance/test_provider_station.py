r"""The station Provider against the shared Provider contract, on a real Station.

It leases the Lanes of the Station whose ``ale-host`` SSH destination ``ALE_TEST_STATION``
names, so it runs only when that is set and is selected with ``-m needs_station``. The
Station's manifest must offer ``ALE_TEST_STATION_SOFTWARE`` (``notepad`` unless set), the
Run's resolution is the Station's own, and the preflight installs this engine's framework
there when no Lane is leased. A Lane is Windows, so the tests the shared suite writes for
POSIX shells are written again here for Windows, with the same claims; the tests of the two
guarantees a machine that serves several Sandboxes must hold also check what only a Station
shows: that a Lane comes back reset at the next generation, and that another Lane's agent
reaches neither its files, its processes, its desktop nor its stage directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import struct
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

import pytest

from ale.core.config import StationProviderConfig
from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    DesktopResolution,
    Identity,
    ImageRef,
    Sandbox,
    SandboxRequest,
)
from ale.core.taskspec import ImageKind, NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.core.testkit import ProviderConformance
from ale.run.framework import station_framework_identity
from ale.run.providers.station import StationProvider, StationSandbox
from ale.run.providers.station.provider import _Stationd

DESTINATION = os.environ.get("ALE_TEST_STATION", "")
SOFTWARE = os.environ.get("ALE_TEST_STATION_SOFTWARE", "notepad")

pytestmark = [
    pytest.mark.conformance,
    pytest.mark.needs_station,
    pytest.mark.skipif(not DESTINATION, reason="ALE_TEST_STATION names no Station"),
]

#: Who a command runs as, by its token, where its home is, and whether it is elevated.
_WHO = r"""
import ctypes, os
size = ctypes.c_ulong(256)
name = ctypes.create_unicode_buffer(256)
ctypes.windll.advapi32.GetUserNameW(name, ctypes.byref(size))
print(name.value)
print(os.environ.get("USERPROFILE", ""))
print(int(ctypes.windll.shell32.IsUserAnAdmin()))
"""

#: Run as the agent: what of the framework and the system it can read or change.
_ESCAPE = r"""
for what, path, mode in (
    ("read-guestd", r"C:\ProgramData\ALE\guestd\main.py", "rb"),
    ("wrote-windows", r"C:\Windows\ale-conformance", "wb"),
):
    try:
        open(path, mode).close()
        print(what)
    except OSError:
        pass
"""

#: Run as another Lane's agent: whether it opens the first Lane's file, process, Cua
#: Driver pipe and stage directory. Each answer is "refused" when Windows denies access.
_NEIGHBOUR = r"""
import ctypes, os, sys
path, pid, pipe, stage = sys.argv[1:]
def attempt(action):
    try:
        action()
        return "opened"
    except PermissionError:
        return "refused"
    except OSError as error:
        return f"failed:{error.winerror}"
print(attempt(lambda: open(path, "rb").close()))
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
handle = kernel.OpenProcess(0x0001, False, int(pid))  # PROCESS_TERMINATE
print("opened" if handle else "refused" if ctypes.get_last_error() == 5 else "gone")
print(attempt(lambda: open(pipe, "r+b").close()))
print(attempt(lambda: os.listdir(stage)))
"""

#: Every entry below ``$target``, links aside, with its owner and access rules, as JSON.
_ACCESS = r"""
$entries = Get-ChildItem -LiteralPath $target -Recurse -Force |
    Where-Object { -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) }
@($entries | ForEach-Object {
    $acl = Get-Acl -LiteralPath $_.FullName
    [pscustomobject]@{
        path = $_.FullName
        owner = $acl.Owner
        rules = @($acl.Access | ForEach-Object { "$($_.IdentityReference)" })
    }
}) | ConvertTo-Json -Depth 3 -Compress
"""


def _access(target: str) -> list[str]:
    command = f"$target = '{target}'\n{_ACCESS}"
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]


def _png_size(png: bytes) -> tuple[int, int]:
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def _lane(sandbox: Sandbox) -> tuple[str, int]:
    """The Lane a Sandbox ran in and its generation, as the RunLock records them."""
    lane, _, generation = sandbox.resolved_image.observed_ref.rpartition("/")[2].partition("@")
    return lane, int(generation)


@contextlib.asynccontextmanager
async def _gateway() -> AsyncIterator[int]:
    """A stand-in Gateway on the Host that answers any request with 200."""

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "0.0.0.0", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture(scope="module")
def resolution() -> DesktopResolution:
    """The size every Lane of the Station has, which the Run's must be."""
    hello = asyncio.run(_Stationd(DESTINATION).call("hello", timeout_sec=60))
    return DesktopResolution.model_validate(hello["resolution"])


class TestStationProvider(ProviderConformance):
    """Every assertion in the shared suite, run against the Lanes of a Station."""

    image = ImageRef(kind=ImageKind.STATION, reference=SOFTWARE)
    gui_image = image  # every Lane is a desktop session
    framework = station_framework_identity()

    @pytest.fixture(autouse=True)
    def _provider(self, resolution: DesktopResolution) -> None:
        self.resolution = resolution
        self.provider = StationProvider(
            StationProviderConfig(destination=DESTINATION, lane_wait_sec=900), resolution
        )

    async def request(self, **overrides: object) -> SandboxRequest:
        overrides.setdefault("os", OperatingSystem.WINDOWS)
        overrides.setdefault("network", NetworkPolicy(mode=NetworkMode.OPEN))
        overrides.setdefault("desktop_resolution", self.resolution)
        return await super().request(**overrides)

    # --- the shared suite's POSIX tests, on Windows -----------------------------------

    @pytest.mark.asyncio
    async def test_exec_reports_exit_codes_and_streams(self) -> None:
        async with await self.provider.create(await self.request()) as sandbox:
            result = await sandbox.exec(["cmd.exe", "/d", "/c", "echo conformance"])
            assert result.ok and "conformance" in result.stdout
            failure = await sandbox.exec(["cmd.exe", "/d", "/c", "exit 3"])
            assert failure.exit_code == 3

    @pytest.mark.asyncio
    async def test_large_replies_arrive_whatever_python_the_image_configures(self) -> None:
        variables = {"PYTHONUNBUFFERED": "1", "PYTHONPATH": "C:\\nonexistent"}
        async with await self.provider.create(await self.request(env=variables)) as sandbox:
            path = str(PureWindowsPath(sandbox.agent_home) / "large.bin")
            payload = bytes(range(256)) * 4096
            await sandbox.write_file(path, payload)
            assert await sandbox.read_file(path) == payload
            printed = await sandbox.exec(
                ["python.exe", "-c", "import sys; sys.stdout.write('x' * 1048576)"]
            )
            assert printed.ok and printed.stdout == "x" * (1 << 20)

    @pytest.mark.asyncio
    async def test_file_reads_progress_while_a_command_is_running(self) -> None:
        async with await self.provider.create(await self.request()) as sandbox:
            directory = str(PureWindowsPath(sandbox.agent_home) / "concurrent-read")
            ready = asyncio.Event()

            async def output(stream: str, data: bytes) -> None:
                if stream == "stdout" and b"ready" in data:
                    ready.set()

            command = asyncio.create_task(
                sandbox.exec(
                    [
                        "python.exe",
                        "-c",
                        "import sys, time; from pathlib import Path; d = Path(sys.argv[1]); "
                        "d.mkdir(); (d / 'session.jsonl').write_bytes(b'first\\nsecond\\n'); "
                        "(d / 'link').symlink_to(r'C:\\Windows\\win.ini'); "
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
                assert not command.done()
            finally:
                assert (await command).ok

    @pytest.mark.asyncio
    async def test_one_image_serves_an_agent_with_and_without_elevation(self) -> None:
        """A Lane's agent never elevates: a request for it is refused before a Lane is
        leased, and every other gets a limited token."""
        prepared = await self.provider.prepare_image(self.image)
        with pytest.raises(ProviderCapabilityError, match="never elevates"):
            await self.provider.create(
                await self.request(
                    prepared_image=prepared, resources=Resources(sudo=True), sudo=True
                )
            )
        async with await self.provider.create(
            await self.request(prepared_image=prepared)
        ) as sandbox:
            assert sandbox.allocation.sudo is False
            who = await sandbox.exec(["python.exe", "-c", _WHO], identity=Identity.AGENT)
            assert who.stdout.split()[-1] == "0", who.stdout

    @pytest.mark.asyncio
    async def test_the_agent_and_the_framework_are_different_accounts(self) -> None:
        """The agent runs as its Lane with a limited token; the framework as SYSTEM. The
        agent reads no part of the framework and changes no system file."""
        async with await self.provider.create(await self.request()) as sandbox:
            assert isinstance(sandbox, StationSandbox)
            agent = await sandbox.exec(["python.exe", "-c", _WHO], identity=Identity.AGENT)
            assert agent.ok, agent.stderr
            name, home, elevated = agent.stdout.splitlines()[:3]
            assert name.casefold() == sandbox.agent_user.casefold() == sandbox.lane.casefold()
            assert PureWindowsPath(home) == PureWindowsPath(sandbox.agent_home)
            assert elevated == "0"
            framework = (await sandbox.exec(["python.exe", "-c", _WHO])).stdout.splitlines()
            assert (framework[0], framework[2]) == ("SYSTEM", "1")
            escaped = await sandbox.exec(["python.exe", "-c", _ESCAPE], identity=Identity.AGENT)
            assert escaped.ok and escaped.stdout.split() == [], escaped.stdout

    @pytest.mark.asyncio
    async def test_one_process_past_the_sandboxs_memory_dies_alone(self) -> None:
        pytest.skip(
            "a Lane is held to no memory_mb: its memory is the Station's, which a process "
            "filling it would take from every other Lane (the Provider's AGENTS.md)"
        )

    @pytest.mark.asyncio
    async def test_a_sandbox_lost_to_its_own_memory_says_so(self) -> None:
        pytest.skip(
            "a Lane is held to no memory_mb: its memory is the Station's, which processes "
            "filling it would take from every other Lane (the Provider's AGENTS.md)"
        )

    @pytest.mark.asyncio
    async def test_a_sandbox_carries_the_engines_framework(self) -> None:
        async with await self.provider.create(await self.request()) as sandbox:
            recorded = await sandbox.read_file("C:\\ProgramData\\ALE\\framework")
            assert recorded.decode().strip() == self.framework

    @pytest.mark.asyncio
    async def test_uploads_ignore_host_modes_and_belong_to_their_identity(self) -> None:
        """As on every Provider, and private: no account but the identity's own, SYSTEM and
        Administrators may open an upload, since the others are other Lanes' agents."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "assets/empty").mkdir(parents=True)
            (source / "assets/data.txt").write_text("input")
            (source / ".hidden").write_text("hidden")
            (source / "test.ps1").write_text("Write-Output stage-ok")
            (source / "link").symlink_to("assets/data.txt")
            async with await self.provider.create(await self.request()) as sandbox:
                target = str(PureWindowsPath(sandbox.agent_home) / "ale-conformance-upload")
                for identity in (Identity.AGENT, Identity.FRAMEWORK):
                    await sandbox.upload_dir(str(source), target, identity=identity)
                    listed = await sandbox.exec(_access(target))
                    assert listed.ok, listed.stderr
                    entries = json.loads(listed.stdout)
                    assert len(entries) == 5, entries  # the link is skipped
                    for entry in entries:
                        accounts = {rule.rpartition("\\")[2].casefold() for rule in entry["rules"]}
                        owner = entry["owner"].rpartition("\\")[2].casefold()
                        if identity is Identity.AGENT:
                            assert owner == sandbox.agent_user.casefold(), entry
                            assert accounts == {"system", "administrators", owner}, entry
                        else:
                            assert owner == "administrators", entry
                            assert accounts == {"system", "administrators"}, entry
                    stage = await sandbox.exec(
                        [
                            "powershell.exe",
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            f"{target}\\test.ps1",
                        ],
                        identity=identity,
                    )
                    assert stage.stdout.strip() == "stage-ok", stage.stderr
                    edit = await sandbox.exec(
                        ["cmd.exe", "/d", "/c", f"echo edit> {target}\\assets\\data.txt"],
                        identity=Identity.AGENT,
                    )
                    assert edit.ok == (identity is Identity.AGENT), edit.stderr

    @pytest.mark.asyncio
    async def test_a_sealed_sandbox_reaches_its_gateway_and_nothing_else(self) -> None:
        """Lanes share the Station's network, so a Task that must be sealed is refused before
        a Lane is leased. An open Lane reaches its Gateway at the fixed port of a loopback
        address drawn for its lease, which no other Lane listens on."""
        with pytest.raises(ProviderCapabilityError, match="network block"):
            await self.provider.create(
                await self.request(network=NetworkPolicy(mode=NetworkMode.BLOCK))
            )
        async with _gateway() as port:
            request = await self.request(gateway_url=f"http://127.0.0.1:{port}")
            async with await self.provider.create(request) as sandbox:
                gateway = urlsplit(sandbox.gateway_url or "")
                assert gateway.port == SANDBOX_GATEWAY_PORT
                assert gateway.hostname and gateway.hostname != "127.0.0.1"
                assert ipaddress.ip_address(gateway.hostname).is_loopback
                fetched = await sandbox.exec(
                    [
                        "python.exe",
                        "-c",
                        "import sys, urllib.request; "
                        "print(urllib.request.urlopen(sys.argv[1], timeout=10).read().decode())",
                        str(sandbox.gateway_url),
                    ],
                    identity=Identity.AGENT,
                )
                assert fetched.stdout.strip() == "ok", fetched.stderr

    @pytest.mark.asyncio
    async def test_a_task_that_declared_open_is_never_sealed(self) -> None:
        async with await self.provider.create(await self.request()) as sandbox:
            await sandbox.close_egress()
            reached = await sandbox.exec(
                [
                    "python.exe",
                    "-c",
                    "import socket; socket.create_connection(('1.1.1.1', 443), 10).close()",
                ],
                identity=Identity.AGENT,
            )
            assert reached.ok, "an open task lost its network: " + reached.stderr

    @pytest.mark.asyncio
    async def test_a_declared_desktop_can_actually_be_used(self) -> None:
        """At the one size every Lane has, which the Run's must be."""
        async with await self.provider.create(await self.request()) as sandbox:
            png = await sandbox.screenshot()
            assert png.startswith(b"\x89PNG") and len(png) > 1000
            assert sandbox.allocation.desktop_resolution == self.resolution
            assert _png_size(png) == (self.resolution.width, self.resolution.height)

    @pytest.mark.asyncio
    async def test_a_desktop_takes_the_runs_resolution(self) -> None:
        """A Lane's size is its keep-alive connection's, which no Run changes: another one is
        refused rather than handed out under a RunLock that says otherwise."""
        other = (
            DesktopResolution(width=1366, height=768)
            if self.resolution != DesktopResolution(width=1366, height=768)
            else DesktopResolution()
        )
        with pytest.raises(ProviderCapabilityError):
            await self.provider.create(await self.request(desktop_resolution=other))

    # --- what a machine that serves several Sandboxes must hold -------------------------

    @pytest.mark.asyncio
    async def test_a_sandbox_starts_from_its_image_whatever_an_earlier_one_left(self) -> None:
        """The Lane an agent left comes back reset, at the next generation, without what the
        agent left in its home. The other Lanes are held meanwhile, so the Station hands out
        that one once its reset passes."""
        first = await self.provider.create(await self.request())
        lane, generation = _lane(first)
        name = f"ale-left-{uuid.uuid4().hex[:8]}"
        try:
            left = str(PureWindowsPath(first.agent_home) / name)
            await first.write_file(left, b"left behind", identity=Identity.AGENT)
        finally:
            await first.destroy()
        held: list[Sandbox] = []
        try:
            while True:
                again = await self.provider.create(await self.request())
                held.append(again)
                if _lane(again)[0] == lane:
                    break
            assert _lane(again)[1] > generation
            assert await again.list_files(str(PureWindowsPath(again.agent_home) / name)) == ()
        finally:
            for each in held:
                await each.destroy()

    @pytest.mark.asyncio
    async def test_concurrent_sandboxes_reach_none_of_each_others_files_or_processes(
        self,
    ) -> None:
        """Two Lanes at once: neither's agent opens a file the other's keeps in its home, a
        process the other runs, the other's Cua Driver, which sees and drives its desktop,
        or the other's stage directory."""
        requests = [await self.request() for _ in range(2)]
        started = await asyncio.gather(
            *(self.provider.create(request) for request in requests), return_exceptions=True
        )
        sandboxes = [each for each in started if isinstance(each, StationSandbox)]
        try:
            for each in started:
                if isinstance(each, BaseException):
                    raise each
            first, second = sandboxes
            kept = str(PureWindowsPath(first.agent_home) / f"ale-kept-{uuid.uuid4().hex[:8]}")
            await first.write_file(kept, b"first's", identity=Identity.AGENT)
            # Named for the lease; guestd tells the agent's commands.
            told = await first.exec(
                ["cmd.exe", "/d", "/c", "echo %ALE_CUA_DRIVER_SOCKET%"], identity=Identity.AGENT
            )
            pipe = told.stdout.strip()
            assert pipe.startswith(f"\\\\.\\pipe\\ale-cua-driver-{first.lane}-"), pipe
            started_pid: asyncio.Future[str] = asyncio.get_running_loop().create_future()

            async def output(stream: str, data: bytes) -> None:
                if stream == "stdout" and not started_pid.done():
                    started_pid.set_result(data.decode().strip())

            running = asyncio.create_task(
                first.exec(
                    ["python.exe", "-c",
                     "import os, time; print(os.getpid(), flush=True); time.sleep(10)"],
                    identity=Identity.AGENT,
                    timeout_sec=60,
                    output_sink=output,
                )
            )  # fmt: skip
            try:
                pid = await asyncio.wait_for(started_pid, timeout=30)
                seen = await second.exec(
                    ["python.exe", "-c", _NEIGHBOUR, kept, pid, pipe, first.stage_root],
                    identity=Identity.AGENT,
                )
                assert seen.ok, seen.stderr
                assert seen.stdout.split() == ["refused"] * 4, seen.stdout
            finally:
                assert (await running).ok
        finally:
            for each in sandboxes:
                await each.destroy()
