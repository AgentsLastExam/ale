"""File discovery and range reads remain available during a running command."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.test_guest_authentication import TOKEN, guest

from ale.core.sandbox import SandboxFile
from ale.run.guestd import main as guestd
from ale.run.providers.docker import DockerSandbox
from ale.run.providers.qemu import QemuSandbox
from ale.run.transport import GuestClient, StdioTransport, TcpTransport

pytestmark = pytest.mark.unit


def test_scan_expands_directory_roots_and_excludes_links(tmp_path: Path) -> None:
    first = tmp_path / "agents" / "first" / "sessions" / "root.jsonl"
    child = first.parent / "children" / "child.jsonl"
    child.parent.mkdir(parents=True)
    first.write_bytes(b"root\n")
    child.write_bytes(b"child\n")
    secret = tmp_path / "auth"
    secret.mkdir()
    (secret / "key.json").write_text("secret")
    (first.parent / "outside").symlink_to(secret, target_is_directory=True)
    (first.parent / "link.json").symlink_to(secret / "key.json")
    (tmp_path / "agents" / "linked").symlink_to(first.parent.parent, target_is_directory=True)
    result = guestd.Handler(lambda _: None).dispatch(
        1, "scan_files", {"path": str(tmp_path / "agents" / "*" / "sessions")}
    )
    files = [SandboxFile.model_validate(item) for item in result["data"]["files"]]
    assert [item.path for item in files] == sorted([str(first), str(child)])
    assert all(item.size == Path(item.path).stat().st_size for item in files)
    assert all(item.mtime_ns == Path(item.path).stat().st_mtime_ns for item in files)
    assert all(item.file_id for item in files)
    missing = guestd.Handler(lambda _: None).dispatch(
        2, "scan_files", {"path": str(tmp_path / "missing")}
    )
    assert missing["data"]["files"] == []


def test_scan_excludes_windows_reparse_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = tmp_path / "agents" / "allowed" / "sessions" / "root.jsonl"
    linked_agent = tmp_path / "agents" / "linked"
    linked_child = allowed.parent / "junction"
    for path in (allowed, linked_agent / "sessions" / "secret.json", linked_child / "secret.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"record\n")
    reparse_directories = {str(linked_agent), str(linked_child)}
    real_scandir = os.scandir

    @contextlib.contextmanager
    def windows_scandir(path):
        with real_scandir(path) as children:
            entries = []
            for child in children:

                def windows_stat(*, follow_symlinks, entry=child):
                    info = entry.stat(follow_symlinks=follow_symlinks)
                    return SimpleNamespace(
                        st_mode=info.st_mode,
                        st_size=info.st_size,
                        st_mtime_ns=info.st_mtime_ns,
                        st_dev=info.st_dev,
                        st_ino=info.st_ino,
                        st_file_attributes=(
                            stat.FILE_ATTRIBUTE_REPARSE_POINT
                            if entry.path in reparse_directories
                            else 0
                        ),
                    )

                entries.append(
                    SimpleNamespace(
                        path=child.path,
                        name=child.name,
                        is_dir=child.is_dir,
                        is_symlink=child.is_symlink,
                        stat=windows_stat,
                    )
                )
            yield entries

    monkeypatch.setattr(guestd, "os", SimpleNamespace(name="nt", scandir=windows_scandir))
    result = guestd.Handler(lambda _: None).dispatch(
        1, "scan_files", {"path": str(tmp_path / "agents" / "*" / "sessions")}
    )
    assert [item["path"] for item in result["data"]["files"]] == [str(allowed)]


@pytest.mark.parametrize(
    ("offset", "length", "expected"), [(2, 3, b"cde"), (2, 0, b""), (20, 3, b""), (4, None, b"ef")]
)
def test_guest_file_ranges(
    tmp_path: Path, offset: int, length: int | None, expected: bytes
) -> None:
    source = tmp_path / "stream"
    source.write_bytes(b"abcdef")
    events = []
    result = guestd.Handler(events.append).dispatch(
        1, "read_file", {"path": str(source), "offset": offset, "length": length}
    )
    assert result["data"]["bytes"] == len(expected)
    assert b"".join(base64.b64decode(item["data"]["b64"]) for item in events) == expected


def test_guest_rejects_special_files_and_negative_ranges(tmp_path: Path) -> None:
    source = tmp_path / "stream"
    source.write_bytes(b"bytes")
    handler = guestd.Handler(lambda _: None)
    for params in ({"offset": -1}, {"length": -1}):
        with pytest.raises(guestd.ProtocolError, match="nonnegative"):
            handler.dispatch(1, "read_file", {"path": str(source), **params})
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(guestd.ProtocolError, match="symbolic links"):
        handler.dispatch(1, "read_file", {"path": str(link)})
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(guestd.ProtocolError, match="regular file"):
            handler.dispatch(1, "read_file", {"path": str(fifo)})


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["docker", "qemu"])
async def test_provider_reads_use_a_separate_guest_connection(tmp_path: Path, backend: str) -> None:
    with guest() as address:
        transports = (
            [StdioTransport([sys.executable, guestd.__file__, "--stdio"]) for _ in range(2)]
            if backend == "docker"
            else [TcpTransport(*address, token=TOKEN) for _ in range(2)]
        )
        clients = [GuestClient(transport) for transport in transports]
        for transport in transports:
            await transport.start()
        sandbox_type = DockerSandbox if backend == "docker" else QemuSandbox
        sandbox = sandbox_type.__new__(sandbox_type)
        sandbox._client, sandbox._file_client = clients
        sandbox.agent_user = "unused"
        sandbox.request = SimpleNamespace()
        ready = asyncio.Event()

        async def output(stream: str, data: bytes) -> None:
            if stream == "stdout" and b"ready" in data:
                ready.set()

        source = tmp_path / "session.jsonl"
        release = tmp_path / "release"
        command = asyncio.create_task(
            sandbox.exec(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys,time; "
                    "Path(sys.argv[1]).write_bytes(b'first\\nsecond\\n'); "
                    "print('ready',flush=True); "
                    "exec('while not Path(sys.argv[2]).exists(): time.sleep(.01)')",
                    str(source),
                    str(release),
                ],
                timeout_sec=5,
                output_sink=output,
            )
        )
        try:
            await asyncio.wait_for(ready.wait(), timeout=2)
            files = await asyncio.wait_for(sandbox.list_files(str(tmp_path)), timeout=1)
            assert [item.path for item in files] == [str(source)]
            assert (
                await asyncio.wait_for(
                    sandbox.read_file(str(source), offset=6, length=7), timeout=1
                )
                == b"second\n"
            )
            assert not command.done()
        finally:
            release.touch()
            await asyncio.wait_for(command, timeout=5)
            await asyncio.gather(*(client.close() for client in clients))
