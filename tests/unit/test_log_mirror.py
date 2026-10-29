"""The Host log mirror under appends, rewrites, races and interrupted reads."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

import pytest

from ale.core.sandbox import Sandbox, SandboxFile
from ale.core.taskspec import OperatingSystem
from ale.run.protocol import log_mirror
from ale.run.protocol.log_mirror import LogMirror

pytestmark = pytest.mark.unit


class MemoryFiles:
    """Only the public Sandbox file operations, with transfer accounting."""

    def __init__(self, home: str = "/home/user") -> None:
        self.home = home
        self.path_type = PureWindowsPath if ":" in home else PurePosixPath
        self.files: dict[str, bytes] = {}
        self.reads: list[tuple[str, int, int | None]] = []
        self.digests = 0
        self.transferred = 0
        self.unreadable: set[str] = set()
        self.after_read: Callable[[], None] | None = None

    def path(self, relative: str) -> str:
        return str(self.path_type(self.home) / relative)

    def put(self, relative: str, data: bytes) -> str:
        self.files[self.path(relative)] = data
        return self.path(relative)

    def append(self, relative: str, data: bytes) -> None:
        self.put(relative, self.files[self.path(relative)] + data)

    async def list_files(self, root: str) -> tuple[SandboxFile, ...]:
        base = self.path_type(root)
        return tuple(
            SandboxFile(path=path, size=len(data))
            for path, data in sorted(self.files.items())
            if self.path_type(path) == base or base in self.path_type(path).parents
        )

    async def read_file(self, path: str, *, offset: int = 0, length: int | None = None) -> bytes:
        if path in self.unreadable:
            raise PermissionError(path)
        self.reads.append((path, offset, length))
        data = self.files[path]
        result = data[offset : None if length is None else offset + length]
        self.transferred += len(result)
        if self.after_read is not None:
            callback, self.after_read = self.after_read, None
            callback()
        return result

    async def file_digest(self, path: str, *, length: int) -> str:
        self.digests += 1
        return f"sha256:{hashlib.sha256(self.files[path][:length]).hexdigest()}"


def mirror(tmp_path: Path, guest: MemoryFiles, *paths: str) -> LogMirror:
    return LogMirror(
        cast(Sandbox, guest),
        home=guest.home,
        os=OperatingSystem.WINDOWS if ":" in guest.home else OperatingSystem.LINUX,
        paths=paths or (".ale-harness", ".codex-ale/sessions"),
        target=tmp_path,
    )


def copied(tmp_path: Path, relative: str) -> bytes:
    return (tmp_path / relative).read_bytes()


async def test_copies_declared_files_and_directories_at_their_home_relative_paths(
    tmp_path: Path,
) -> None:
    guest = MemoryFiles()
    guest.put(".ale-harness/stdout.log", b"stdout")
    guest.put(".ale-harness/stderr.log", b"")
    guest.put(".codex-ale/sessions/2026/rollout-a.jsonl", b"root\n")
    guest.put(".codex-ale/sessions/2026/subagents/child.jsonl", b"child\n")
    guest.put(".codex-ale/auth.json", b"not declared")
    logs = mirror(tmp_path, guest, ".ale-harness", ".codex-ale/sessions")

    await logs.sync()
    assert sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file()
    ) == [
        ".ale-harness/stderr.log",
        ".ale-harness/stdout.log",
        ".codex-ale/sessions/2026/rollout-a.jsonl",
        ".codex-ale/sessions/2026/subagents/child.jsonl",
    ]
    assert not logs.incomplete


async def test_appends_transfer_only_new_bytes_and_unchanged_files_cost_nothing(
    tmp_path: Path,
) -> None:
    guest = MemoryFiles()
    character = "跨".encode()
    guest.put(".ale-harness/stdout.log", b'{"text":"' + character[:1])
    logs = mirror(tmp_path, guest)
    await logs.sync()
    guest.reads.clear()
    guest.transferred = 0

    await logs.sync()
    assert guest.reads == [] and guest.digests == 0

    suffix = character[1:] + ' Windows 的内容"}\n'.encode()
    guest.append(".ale-harness/stdout.log", suffix)
    await logs.sync()
    assert guest.transferred == len(suffix)
    assert (
        copied(tmp_path, ".ale-harness/stdout.log")
        == guest.files[guest.path(".ale-harness/stdout.log")]
    )


@pytest.mark.parametrize("home", ["/home/user", r"C:\Users\agent"])
async def test_cost_grows_linearly_on_linux_and_windows(tmp_path: Path, home: str) -> None:
    guest = MemoryFiles(home)
    guest.put(".ale-harness/stdout.log", b"")
    logs = mirror(tmp_path, guest)
    block = b"x" * (1024 * 1024)
    for _ in range(10):
        guest.append(".ale-harness/stdout.log", block)
        await logs.sync()
    # No file identity is needed and nothing is hashed while the agent runs.
    assert guest.transferred == 10 * len(block)
    assert guest.digests == 0
    await logs.sync(final=True)
    assert guest.transferred == 10 * len(block)
    assert guest.digests == 1
    assert (tmp_path / ".ale-harness/stdout.log").stat().st_size == 10 * len(block)


async def test_a_file_that_shrank_is_copied_again_from_its_start(tmp_path: Path) -> None:
    guest = MemoryFiles()
    path = ".codex-ale/sessions/history.jsonl"
    guest.put(path, b'{"turn":1}\n{"turn":2}\n')
    logs = mirror(tmp_path, guest)
    await logs.sync()
    guest.put(path, b"short\n")
    await logs.sync()
    assert copied(tmp_path, path) == b"short\n"
    assert guest.digests == 0


@pytest.mark.parametrize("rewritten", [b'{"turn":"compacted"}\n{"turn":2}\n', b'{"turn":9}\n'])
async def test_final_sync_copies_a_rewritten_file_again(tmp_path: Path, rewritten: bytes) -> None:
    guest = MemoryFiles()
    path = ".codex-ale/sessions/index.json"
    guest.put(path, b'{"turn":1}\n')
    logs = mirror(tmp_path, guest)
    await logs.sync()
    guest.put(path, rewritten)
    await logs.sync()
    await logs.sync(final=True)
    assert copied(tmp_path, path) == rewritten
    assert not logs.incomplete


async def test_file_rewritten_during_the_final_copy_is_reported_then_recovered(
    tmp_path: Path,
) -> None:
    guest = MemoryFiles()
    path = ".codex-ale/sessions/state.json"
    guest.put(path, b'{"state":"old"}')
    logs = mirror(tmp_path, guest)
    await logs.sync()
    guest.put(path, b'{"state":"new"}')
    guest.after_read = lambda: guest.put(path, b'{"state":"end"}')
    await logs.sync(final=True)
    assert logs.incomplete
    assert copied(tmp_path, path) == b""
    await logs.sync(final=True)
    assert not logs.incomplete
    assert copied(tmp_path, path) == b'{"state":"end"}'


async def test_catching_up_reads_pieces_and_hashes_only_at_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(log_mirror, "_PIECE_BYTES", 4)
    guest = MemoryFiles()
    guest.put(".ale-harness/stdout.log", b"x" * 40)
    logs = mirror(tmp_path, guest)
    await logs.sync()
    assert guest.digests == 0 and len(guest.reads) == 10
    guest.append(".ale-harness/stdout.log", b"y" * 40)
    await logs.sync(final=True)
    assert guest.digests == 1 and len(guest.reads) == 20
    assert (
        copied(tmp_path, ".ale-harness/stdout.log")
        == guest.files[guest.path(".ale-harness/stdout.log")]
    )


async def test_missing_file_keeps_its_copy_and_returns_when_it_reappears(tmp_path: Path) -> None:
    guest = MemoryFiles()
    path = guest.put(".ale-harness/stderr.log", b"warning\n")
    logs = mirror(tmp_path, guest)
    await logs.sync()
    del guest.files[path]
    await logs.sync()
    assert copied(tmp_path, ".ale-harness/stderr.log") == b"warning\n"
    guest.put(".ale-harness/stderr.log", b"warning\nagain\n")
    await logs.sync()
    assert copied(tmp_path, ".ale-harness/stderr.log") == b"warning\nagain\n"


async def test_one_unreadable_file_does_not_block_the_others(tmp_path: Path) -> None:
    guest = MemoryFiles()
    blocked = guest.put(".codex-ale/sessions/blocked.jsonl", b"secret")
    guest.put(".ale-harness/stdout.log", b"stdout")
    guest.unreadable.add(blocked)
    logs = mirror(tmp_path, guest)

    await logs.sync()
    assert logs.incomplete
    assert copied(tmp_path, ".ale-harness/stdout.log") == b"stdout"
    guest.unreadable.clear()
    await logs.sync()
    assert not logs.incomplete


async def test_large_backlogs_take_turns_and_a_cancelled_piece_leaves_a_valid_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(log_mirror, "_PIECE_BYTES", 4)
    guest = MemoryFiles()
    first = guest.put(".codex-ale/sessions/a.jsonl", b"aaaaaaaaaaaa")
    second = guest.put(".codex-ale/sessions/b.jsonl", b"bbbbbbbbbbbb")
    logs = mirror(tmp_path, guest, ".codex-ale/sessions")
    await logs.sync()
    assert [path for path, _, _ in guest.reads] == [first, second] * 3

    guest.append(".codex-ale/sessions/a.jsonl", b"AAAAAAAA")
    original = guest.read_file

    async def cancelled_second_piece(path: str, **kwargs: int) -> bytes:
        if kwargs.get("offset", 0) > 12:
            raise asyncio.CancelledError
        return await original(path, **kwargs)

    guest.read_file = cancelled_second_piece  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await logs.sync()
    assert copied(tmp_path, ".codex-ale/sessions/a.jsonl") == b"aaaaaaaaaaaaAAAA"
    guest.read_file = original  # type: ignore[method-assign]
    await logs.sync()
    assert copied(tmp_path, ".codex-ale/sessions/a.jsonl") == b"aaaaaaaaaaaaAAAAAAAA"


async def test_windows_guest_paths_map_to_a_posix_host_layout(tmp_path: Path) -> None:
    guest = MemoryFiles(r"C:\Users\agent")
    guest.put(".openclaw-ale/agents/main/sessions/root.jsonl", b"root\n")
    logs = mirror(tmp_path, guest, ".openclaw-ale/agents/main/sessions")
    await logs.sync()
    assert copied(tmp_path, ".openclaw-ale/agents/main/sessions/root.jsonl") == b"root\n"


async def test_copy_left_by_an_earlier_mirror_is_kept_only_if_still_a_prefix(
    tmp_path: Path,
) -> None:
    guest = MemoryFiles()
    guest.put(".ale-harness/stdout.log", b"first\nsecond\n")
    (tmp_path / ".ale-harness").mkdir()
    (tmp_path / ".ale-harness/stdout.log").write_bytes(b"first\n")
    await mirror(tmp_path, guest).sync(final=True)
    assert guest.transferred == len(b"second\n")
    (tmp_path / ".ale-harness/stdout.log").write_bytes(b"other\n")
    await mirror(tmp_path, guest).sync(final=True)
    assert copied(tmp_path, ".ale-harness/stdout.log") == b"first\nsecond\n"
