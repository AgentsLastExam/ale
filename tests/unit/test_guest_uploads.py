"""Streamed directory uploads through the guest service."""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from ale.core.errors import ProviderStartError
from ale.run.guestd.main import Handler, ProtocolError, _Server, _TCPHandler, serve_stream
from ale.run.transport import GuestClient, TcpTransport

pytestmark = pytest.mark.unit

TOKEN = "t" * 43
#: Around the guest reader's buffer and the host's block sizes, where framing breaks first.
SIZES = (0, 1, 8191, 8192, 8193, 5 * 1024 * 1024 + 3)


class _PipeTransport:
    """A guest session over two OS pipes, as a container's exec session is."""

    def __init__(self) -> None:
        self._requests_read, self._requests_write = os.pipe()
        self._replies_read, self._replies_write = os.pipe()
        self._replies = os.fdopen(self._replies_read, "rb")
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        with (
            os.fdopen(self._requests_read, "rb") as reader,
            os.fdopen(self._replies_write, "w") as writer,
        ):
            serve_stream(reader, writer)

    async def send(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[await asyncio.to_thread(os.write, self._requests_write, view) :]

    async def recv(self) -> str:
        return (await asyncio.to_thread(self._replies.readline)).decode().strip()

    async def close(self) -> None:
        os.close(self._requests_write)
        await asyncio.to_thread(self._thread.join, 2)
        self._replies.close()


@asynccontextmanager
async def client(kind: str) -> AsyncIterator[GuestClient]:
    if kind == "pipe":
        guest = GuestClient(_PipeTransport())
        try:
            yield guest
        finally:
            await guest.close()
        return
    with _Server(("127.0.0.1", 0), _TCPHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        transport = TcpTransport(str(host), int(port), token=TOKEN)
        await transport.start(timeout_sec=5, interval_sec=0.05)
        guest = GuestClient(transport)
        try:
            yield guest
        finally:
            await guest.close()
            server.shutdown()
            thread.join(timeout=2)


def tree(root: Path) -> None:
    (root / "empty").mkdir(parents=True)
    (root / "nested/deeper").mkdir(parents=True)
    (root / ".hidden").write_text("hidden")
    for index, size in enumerate(SIZES):
        (root / "nested" / f"file-{index}").write_bytes(os.urandom(size))
    (root / "nested/deeper/after-large").write_text("still framed")
    (root / "run.sh").write_text("printf ok")
    (root / "run.sh").chmod(0o700)
    (root / "link").symlink_to("nested/file-1")


@pytest.mark.parametrize("kind", ["pipe", "tcp"])
async def test_a_tree_arrives_intact_with_upload_modes(kind: str, tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    tree(source)
    async with client(kind) as guest:
        await guest.upload_dir(str(source), str(target))
        assert (await guest.health())["proto"] >= 2  # the session is still in step

    for path in source.rglob("*"):
        copy = target / path.relative_to(source)
        if path.is_symlink():
            assert copy.is_symlink() and os.readlink(copy) == os.readlink(path)
        elif path.is_dir():
            assert stat.S_IMODE(copy.lstat().st_mode) == 0o755
        else:
            assert copy.read_bytes() == path.read_bytes()
            assert stat.S_IMODE(copy.lstat().st_mode) == 0o644
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


async def test_links_already_in_the_target_are_replaced_not_followed(tmp_path: Path) -> None:
    source, target, outside = tmp_path / "source", tmp_path / "target", tmp_path / "outside"
    (source / "sub").mkdir(parents=True)
    (source / "data.txt").write_text("upload")
    (source / "sub/inner.txt").write_text("upload")
    outside.mkdir()
    (outside / "secret").write_text("secret")
    target.mkdir()
    (target / "data.txt").symlink_to(outside / "secret")
    (target / "sub").symlink_to(outside)
    os.link(outside / "secret", target / "hard")
    (source / "hard").write_text("upload")

    async with client("pipe") as guest:
        await guest.upload_dir(str(source), str(target))

    assert (outside / "secret").read_text() == "secret"
    assert sorted(path.name for path in outside.iterdir()) == ["secret"]
    assert not (target / "data.txt").is_symlink()
    assert (target / "data.txt").read_text() == "upload"
    assert (target / "sub").is_dir() and not (target / "sub").is_symlink()
    assert (target / "hard").stat().st_ino != (outside / "secret").stat().st_ino


@pytest.mark.parametrize("kind", ["pipe", "tcp"])
async def test_a_local_failure_aborts_the_upload_and_keeps_the_session(
    kind: str, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a-large").write_bytes(os.urandom(3 * 1024 * 1024))
    os.mkfifo(source / "b-pipe")
    async with client(kind) as guest:
        with pytest.raises(ProviderStartError, match="unsupported file type"):
            await guest.upload_dir(str(source), str(tmp_path / "target"))
        assert (await guest.health())["proto"] >= 2


def body(*frames: dict[str, object] | bytes) -> io.BufferedReader:
    raw = b"".join(
        frame if isinstance(frame, bytes) else json.dumps({"id": 1, **frame}).encode() + b"\n"
        for frame in frames
    )
    return io.BufferedReader(io.BytesIO(raw))


def test_unsafe_entry_names_fail_without_losing_the_framing(tmp_path: Path) -> None:
    target = tmp_path / "root" / "target"
    reader = body(
        {"entry": "../escaped", "kind": "file", "size": 3},
        b"abc",
        {"kind": "file", "size": 4},
        b"none",
        {"entry": "fine", "kind": "file", "size": 2},
        b"ok",
        {"end": True},
        b'{"id":2,"op":"health","params":{}}\n',
    )
    with pytest.raises(ProtocolError, match="unsafe entry name"):
        Handler(lambda _: None, reader).dispatch(1, "put_tree", {"path": str(target)})
    assert not (tmp_path / "root" / "escaped").exists()
    assert not (target / "None").exists()
    assert json.loads(reader.readline())["op"] == "health"


def test_a_body_that_ends_early_closes_the_session(tmp_path: Path) -> None:
    replies = io.StringIO()
    serve_stream(
        body(
            {"op": "put_tree", "params": {"path": str(tmp_path / "t")}},
            {"entry": "f", "kind": "file", "size": 10},
            b"short",
        ),
        replies,
    )
    reply = json.loads(replies.getvalue().splitlines()[-1])
    assert reply["ok"] is False and "ended" in reply["error"]["message"]
