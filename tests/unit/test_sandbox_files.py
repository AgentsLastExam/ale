"""File discovery and range reads remain available during a running command."""

from __future__ import annotations

import asyncio
import base64
import hashlib
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


def listing(path: Path) -> list[SandboxFile]:
    result = guestd.Handler(lambda _: None).dispatch(1, "list_files", {"path": str(path)})
    return [SandboxFile.model_validate(item) for item in result["data"]["files"]]


def test_listing_returns_regular_files_below_a_path_without_following_links(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "agents" / "main" / "sessions"
    root = sessions / "root.jsonl"
    child = sessions / "children" / "child.jsonl"
    child.parent.mkdir(parents=True)
    root.write_bytes(b"root\n")
    child.write_bytes(b"child\n")
    secret = tmp_path / "auth"
    secret.mkdir()
    (secret / "key.json").write_text("secret")
    (sessions / "outside").symlink_to(secret, target_is_directory=True)
    (sessions / "link.json").symlink_to(secret / "key.json")
    (tmp_path / "linked-sessions").symlink_to(sessions, target_is_directory=True)

    files = listing(sessions)
    assert [item.path for item in files] == sorted([str(root), str(child)])
    assert all(item.size == Path(item.path).stat().st_size for item in files)
    assert [item.path for item in listing(root)] == [str(root)]
    assert listing(tmp_path / "missing") == []
    for through_link in (tmp_path / "linked-sessions", tmp_path / "linked-sessions" / "children"):
        with pytest.raises(guestd.ProtocolError, match="passes through a link"):
            listing(through_link)
    with pytest.raises(guestd.ProtocolError, match="absolute"):
        guestd.Handler(lambda _: None).dispatch(1, "list_files", {"path": "relative"})


def test_listing_skips_windows_reparse_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    allowed = sessions / "root.jsonl"
    junction = sessions / "junction"
    for path in (allowed, junction / "secret.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"record\n")
    real_lstat = os.lstat

    def windows_lstat(path):  # type: ignore[no-untyped-def]
        info = real_lstat(path)
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_size=info.st_size,
            st_file_attributes=(stat.FILE_ATTRIBUTE_REPARSE_POINT if Path(path) == junction else 0),
        )

    monkeypatch.setattr(
        guestd, "os", SimpleNamespace(name="nt", lstat=windows_lstat, scandir=os.scandir)
    )
    assert [item.path for item in listing(sessions)] == [str(allowed)]


def test_guest_digest_covers_exactly_the_requested_prefix(tmp_path: Path) -> None:
    source = tmp_path / "stream"
    source.write_bytes(b"abcdef")
    handler = guestd.Handler(lambda _: None)
    for length, expected in ((0, b""), (3, b"abc"), (6, b"abcdef"), (20, b"abcdef")):
        result = handler.dispatch(1, "file_digest", {"path": str(source), "length": length})
        assert result["data"]["digest"] == f"sha256:{hashlib.sha256(expected).hexdigest()}"
    with pytest.raises(guestd.ProtocolError, match="nonnegative"):
        handler.dispatch(1, "file_digest", {"path": str(source), "length": -1})
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(guestd.ProtocolError, match="passes through a link"):
        handler.dispatch(1, "file_digest", {"path": str(link), "length": 1})


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
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    for path in (link, linked / "stream"):
        with pytest.raises(guestd.ProtocolError, match="passes through a link"):
            handler.dispatch(1, "read_file", {"path": str(path)})
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
            first_line = hashlib.sha256(b"first\n").hexdigest()
            assert await sandbox.file_digest(str(source), length=6) == f"sha256:{first_line}"
            assert not command.done()
        finally:
            release.touch()
            await asyncio.wait_for(command, timeout=5)
            await asyncio.gather(*(client.close() for client in clients))


def test_guest_writes_replace_links_instead_of_writing_through_them(tmp_path: Path) -> None:
    handler = guestd.Handler(lambda _: None)
    protected = tmp_path / "protected"
    protected.write_bytes(b"system")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def write(path: Path, data: bytes, **params: object) -> None:
        payload = {"path": str(path), "b64": base64.b64encode(data).decode(), **params}
        handler.dispatch(1, "write_file", payload)

    symlink, hardlink = tmp_path / "prompt.md", tmp_path / "invocations.jsonl"
    symlink.symlink_to(protected)
    os.link(protected, hardlink)
    write(symlink, b"one")
    write(hardlink, b"two")
    write(hardlink, b"+", append=True)
    assert protected.read_bytes() == b"system"
    assert not symlink.is_symlink() and symlink.read_bytes() == b"one"
    assert hardlink.stat().st_nlink == 1 and hardlink.read_bytes() == b"two+"

    kept = tmp_path / "kept.sh"
    kept.write_bytes(b"old")
    kept.chmod(0o750)
    write(kept, b"new")
    assert kept.read_bytes() == b"new" and stat.S_IMODE(kept.stat().st_mode) == 0o750

    directory = tmp_path / "skills"
    directory.symlink_to(elsewhere, target_is_directory=True)
    handler.dispatch(1, "mkdirs", {"path": str(directory)})
    assert directory.is_dir() and not directory.is_symlink()

    (elsewhere / "keep").write_bytes(b"keep")
    removed = tmp_path / "removed"
    removed.symlink_to(elsewhere, target_is_directory=True)
    handler.dispatch(1, "remove", {"path": str(removed)})
    assert not removed.exists() and (elsewhere / "keep").read_bytes() == b"keep"
