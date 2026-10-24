"""Public collector behavior under appends, file races, and interrupted reads."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

import pytest
from tests.acceptance.trajectory import native_session_file

from ale.core.harness import HarnessSession
from ale.core.sandbox import Sandbox, SandboxFile
from ale.core.taskspec import OperatingSystem
from ale.run.harnesses import logs as collection
from ale.run.harnesses.codex_cli import CodexCliHarness
from ale.run.harnesses.logs import CliLogCollector
from ale.run.harnesses.openclaw_cli import OpenClawCliHarness

pytestmark = pytest.mark.unit


@dataclass
class GuestFile:
    data: bytes
    mtime: int
    identity: str


class MemoryFiles:
    """Only the public file protocol, with byte and request accounting."""

    def __init__(self, home: str = "/home/user") -> None:
        self.home = home
        self.path_type = PureWindowsPath if ":" in home else PurePosixPath
        self.files: dict[str, GuestFile] = {}
        self.scans: list[str] = []
        self.reads: list[tuple[str, int, int | None]] = []
        self.transferred = 0
        self.clock = 0
        self.after_read: Callable[[], None] | None = None
        for name in ("stdout.log", "stderr.log", "invocations.jsonl"):
            self.put(f".ale-harness/{name}", b"")

    def path(self, relative: str) -> str:
        return str(self.path_type(self.home) / relative)

    def put(self, relative: str, data: bytes, *, replace: bool = False) -> str:
        path = self.path(relative)
        self.clock += 1
        previous = self.files.get(path)
        identity = str(self.clock) if replace or previous is None else previous.identity
        self.files[path] = GuestFile(data, self.clock, identity)
        return path

    async def list_files(self, root: str) -> tuple[SandboxFile, ...]:
        self.scans.append(root)
        return tuple(
            SandboxFile(path=path, size=len(item.data), mtime_ns=item.mtime, file_id=item.identity)
            for path, item in sorted(self.files.items())
            if any(
                fnmatch.fnmatchcase(str(parent), root) for parent in self.path_type(path).parents
            )
        )

    async def read_file(self, path: str, *, offset: int = 0, length: int | None = None) -> bytes:
        self.reads.append((path, offset, length))
        data = self.files[path].data
        end = None if length is None else offset + length
        result = data[offset:end]
        self.transferred += len(result)
        if self.after_read is not None:
            callback, self.after_read = self.after_read, None
            callback()
        return result


def collector(
    tmp_path: Path,
    guest: MemoryFiles,
    *,
    native_logs: bool = False,
) -> CliLogCollector:
    session = HarnessSession(
        episode_id="episode",
        gateway_url="http://gateway",
        token="episode-token",
        model="test-model",
        home=guest.home,
        os=(OperatingSystem.WINDOWS if ":" in guest.home else OperatingSystem.LINUX),
    )
    return CliLogCollector(
        cast(Sandbox, guest), session, CodexCliHarness(), tmp_path, native_logs=native_logs
    )


async def test_default_collection_skips_native_scans_and_ignores_other_framework_files(
    tmp_path: Path,
) -> None:
    guest = MemoryFiles()
    guest.put(".ale-harness/stdout.log", b"stdout")
    guest.put(".ale-harness/stderr.log", b"stderr")
    guest.put(".ale-harness/invocations.jsonl", b'{"stdout_offset":0}\n')
    guest.put(".ale-harness/unrelated", b"not a stream")
    guest.put(".codex-ale/sessions/root.jsonl", b"large native file")
    mirror = collector(tmp_path, guest)

    assert await mirror.sync()
    assert set(guest.scans) == {guest.path(".ale-harness")}
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "invocations.jsonl",
        "stderr.log",
        "stdout.log",
    ]
    assert (tmp_path / "stdout.log").read_bytes() == b"stdout"
    assert (tmp_path / "stderr.log").read_bytes() == b"stderr"
    assert not mirror.incomplete


async def test_append_copies_only_new_bytes_and_unchanged_tick_reads_nothing(
    tmp_path: Path,
) -> None:
    guest = MemoryFiles()
    first_character = "跨".encode()
    initial = b'{"text":"' + first_character[:1]
    path = guest.put(".ale-harness/stdout.log", initial)
    mirror = collector(tmp_path, guest)
    assert await mirror.sync()
    guest.reads.clear()
    guest.transferred = 0

    assert not await mirror.sync()
    assert guest.reads == []
    suffix = first_character[1:] + ' Windows 的内容"}\n'.encode()
    guest.put(".ale-harness/stdout.log", initial + suffix)
    assert await mirror.sync()
    assert guest.reads == [(path, len(initial), len(suffix))]
    assert guest.transferred == len(suffix)
    assert (tmp_path / "stdout.log").read_bytes() == initial + suffix


async def test_native_opt_in_discovers_children_and_keeps_relative_paths(tmp_path: Path) -> None:
    guest = MemoryFiles()
    root = ".codex-ale/sessions/date/root.jsonl"
    child = ".codex-ale/sessions/date/subagents/child.jsonl"
    guest.put(root, b"root\n")
    mirror = collector(tmp_path, guest, native_logs=True)
    assert await mirror.sync()
    guest.reads.clear()
    guest.put(child, b"child\n")

    assert await mirror.sync()
    assert (tmp_path / "native" / root).read_bytes() == b"root\n"
    assert (tmp_path / "native" / child).read_bytes() == b"child\n"
    assert [path for path, _, _ in guest.reads] == [guest.path(child)]


@pytest.mark.parametrize(
    ("replace", "replacement"),
    [
        (False, b"short"),
        (False, b"same length"),
        (True, b"short"),
        (True, b"same length"),
        (True, b"a longer replacement"),
    ],
)
async def test_rewritten_stream_is_replaced_and_prior_bytes_are_preserved(
    tmp_path: Path, replace: bool, replacement: bytes
) -> None:
    guest = MemoryFiles()
    original = b"old content"
    guest.put(".ale-harness/stdout.log", original)
    mirror = collector(tmp_path, guest)
    assert await mirror.sync()
    guest.reads.clear()
    guest.put(".ale-harness/stdout.log", replacement, replace=replace)

    assert await mirror.sync()
    assert guest.reads[0][1] == 0
    assert (tmp_path / "stdout.log").read_bytes() == replacement
    assert [path.read_bytes() for path in (tmp_path / "superseded").rglob("stdout.log.*")] == [
        original
    ]


async def test_snapshot_rewrite_uses_whole_new_file_and_skips_unchanged_snapshot(
    tmp_path: Path,
) -> None:
    guest = MemoryFiles()
    relative = ".codex-ale/sessions/index.json"
    path = guest.put(relative, b'{"sessions":[]}')
    mirror = collector(tmp_path, guest, native_logs=True)
    assert await mirror.sync()
    guest.reads.clear()
    assert not await mirror.sync()
    assert guest.reads == []
    updated = b'{"sessions":["root"]}'
    guest.put(relative, updated)

    assert await mirror.sync()
    assert guest.reads == [(path, 0, len(updated))]
    assert (tmp_path / "native" / relative).read_bytes() == updated


async def test_missing_file_identity_uses_snapshot_instead_of_assuming_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guest = MemoryFiles()
    relative = ".ale-harness/stdout.log"
    guest.put(relative, b"original")
    original_scan = guest.list_files

    async def without_identity(root: str) -> tuple[SandboxFile, ...]:
        return tuple(
            entry.model_copy(update={"file_id": None}) for entry in await original_scan(root)
        )

    monkeypatch.setattr(guest, "list_files", without_identity)
    mirror = collector(tmp_path, guest)
    assert await mirror.sync()
    guest.reads.clear()
    replacement = b"longer replacement"
    guest.put(relative, replacement, replace=True)

    assert await mirror.sync()
    assert guest.reads[0][1] == 0
    assert (tmp_path / "stdout.log").read_bytes() == replacement


async def test_snapshot_mutated_during_copy_is_not_published_and_retries(tmp_path: Path) -> None:
    guest = MemoryFiles()
    relative = ".codex-ale/sessions/index.json"
    original, unstable, stable = b"original", b"unstable", b"stable!!"
    guest.put(relative, original)
    mirror = collector(tmp_path, guest, native_logs=True)
    assert await mirror.sync()
    guest.put(relative, unstable)
    guest.after_read = lambda: guest.put(relative, stable)

    assert not await mirror.sync()
    assert mirror.incomplete
    assert (tmp_path / "native" / relative).read_bytes() == original
    assert not list(tmp_path.rglob("*.pull"))
    assert await mirror.sync()
    assert not mirror.incomplete
    assert (tmp_path / "native" / relative).read_bytes() == stable


@pytest.mark.parametrize("replace", [False, True])
async def test_snapshot_truncated_mid_read_keeps_last_valid_copy_and_retries(
    tmp_path: Path, replace: bool
) -> None:
    guest = MemoryFiles()
    relative = ".codex-ale/sessions/index.json"
    guest.put(relative, b"original")
    mirror = collector(tmp_path, guest, native_logs=True)
    assert await mirror.sync()
    guest.put(relative, b"x" * (512 * 1024))
    guest.after_read = lambda: guest.put(relative, b"short replacement", replace=replace)

    assert not await mirror.sync()
    assert mirror.incomplete
    assert (tmp_path / "native" / relative).read_bytes() == b"original"
    assert not list(tmp_path.rglob("*.pull"))
    assert await mirror.sync()
    assert not mirror.incomplete
    assert (tmp_path / "native" / relative).read_bytes() == b"short replacement"


async def test_replaced_append_source_during_read_never_mixes_generations(tmp_path: Path) -> None:
    guest = MemoryFiles()
    relative = ".ale-harness/stdout.log"
    guest.put(relative, b"old\n")
    mirror = collector(tmp_path, guest)
    assert await mirror.sync()
    guest.put(relative, b"old\nappended\n")
    guest.after_read = lambda: guest.put(relative, b"a different session\n", replace=True)

    assert not await mirror.sync()
    assert (tmp_path / "stdout.log").read_bytes() == b"old\n"
    assert mirror.incomplete
    assert not list(tmp_path.rglob("*.pull"))
    assert await mirror.sync()
    assert not mirror.incomplete
    assert (tmp_path / "stdout.log").read_bytes() == b"a different session\n"


async def test_append_growth_during_copy_is_caught_up_on_next_tick(tmp_path: Path) -> None:
    guest = MemoryFiles()
    relative = ".ale-harness/stdout.log"
    guest.put(relative, b"first\n")
    guest.after_read = lambda: guest.put(relative, b"first\nsecond\n")
    mirror = collector(tmp_path, guest)

    assert await mirror.sync()
    assert (tmp_path / "stdout.log").read_bytes() == b"first\n"
    assert await mirror.sync(final=True)
    assert (tmp_path / "stdout.log").read_bytes() == b"first\nsecond\n"
    assert guest.transferred == len(b"first\nsecond\n")


async def test_missing_file_preserves_last_mirror_and_is_collected_when_it_reappears(
    tmp_path: Path,
) -> None:
    guest = MemoryFiles()
    path = guest.put(".ale-harness/stdout.log", b"preserved")
    mirror = collector(tmp_path, guest)
    assert await mirror.sync()
    del guest.files[path]

    assert not await mirror.sync()
    assert (tmp_path / "stdout.log").read_bytes() == b"preserved"
    guest.put(".ale-harness/stdout.log", b"replacement", replace=True)
    assert await mirror.sync()
    assert (tmp_path / "stdout.log").read_bytes() == b"replacement"


async def test_final_missing_stdout_marks_incomplete_and_preserves_observed_prefix(
    tmp_path: Path,
) -> None:
    guest = MemoryFiles()
    path = guest.put(".ale-harness/stdout.log", b"observed prefix\n")
    mirror = collector(tmp_path, guest)
    assert await mirror.sync()
    del guest.files[path]

    assert not await mirror.sync(final=True)
    assert mirror.incomplete
    assert (tmp_path / "stdout.log").read_bytes() == b"observed prefix\n"
    guest.put(".ale-harness/stdout.log", b"restored complete output\n", replace=True)
    assert await mirror.sync(final=True)
    assert not mirror.incomplete
    assert (tmp_path / "stdout.log").read_bytes() == b"restored complete output\n"


async def test_one_unreadable_file_does_not_block_other_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guest = MemoryFiles()
    failing = guest.put(".ale-harness/stderr.log", b"temporarily unreadable")
    guest.put(".ale-harness/stdout.log", b"observed output")
    original_read = guest.read_file

    async def fail_stderr(path: str, *, offset: int = 0, length: int | None = None) -> bytes:
        if path == failing:
            raise OSError("temporary file read failure")
        return await original_read(path, offset=offset, length=length)

    monkeypatch.setattr(guest, "read_file", fail_stderr)
    mirror = collector(tmp_path, guest)
    assert await mirror.sync()
    assert mirror.incomplete
    assert (tmp_path / "stdout.log").read_bytes() == b"observed output"
    assert not (tmp_path / "stderr.log").exists()
    assert not list(tmp_path.rglob("*.pull"))

    monkeypatch.setattr(guest, "read_file", original_read)
    assert await mirror.sync()
    assert not mirror.incomplete
    assert (tmp_path / "stderr.log").read_bytes() == b"temporarily unreadable"


async def test_cancelled_chunk_read_removes_staging_and_does_not_advance_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guest = MemoryFiles()
    relative = ".ale-harness/stdout.log"
    original = b"previous"
    guest.put(relative, original)
    mirror = collector(tmp_path, guest)
    assert await mirror.sync()
    updated = original + b"x" * (512 * 1024)
    guest.put(relative, updated)
    original_read = guest.read_file
    blocked = asyncio.Event()

    async def block_second_chunk(path: str, *, offset: int = 0, length: int | None = None) -> bytes:
        if offset > len(original):
            blocked.set()
            await asyncio.Future()
        return await original_read(path, offset=offset, length=length)

    monkeypatch.setattr(guest, "read_file", block_second_chunk)
    task = asyncio.create_task(mirror.sync())
    await asyncio.wait_for(blocked.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / "stdout.log").read_bytes() == original
    assert not list(tmp_path.rglob("*.pull"))
    monkeypatch.setattr(guest, "read_file", original_read)
    guest.reads.clear()

    assert await mirror.sync()
    assert guest.reads[0][1] == len(original)
    assert (tmp_path / "stdout.log").read_bytes() == updated


async def test_windows_paths_use_posix_host_layout_and_native_wildcard_snapshots(
    tmp_path: Path,
) -> None:
    guest = MemoryFiles(r"C:\Users\Agent User")
    relative = ".openclaw-ale/agents/main/sessions/root.jsonl"
    guest.put(".ale-harness/stdout.log", b"stdout")
    path = guest.put(relative, b"native\n")
    session = HarnessSession(
        episode_id="episode",
        gateway_url="http://gateway",
        token="token",
        model="model",
        home=guest.home,
        os=OperatingSystem.WINDOWS,
    )
    mirror = CliLogCollector(
        cast(Sandbox, guest), session, OpenClawCliHarness(), tmp_path, native_logs=True
    )
    assert await mirror.sync()
    assert (tmp_path / "stdout.log").read_bytes() == b"stdout"
    assert (tmp_path / "native" / relative).read_bytes() == b"native\n"
    guest.reads.clear()
    guest.put(relative, b"native\nappend\n")

    assert await mirror.sync()
    assert guest.reads == [(path, 0, len(b"native\nappend\n"))]


async def test_large_log_bounds_each_tick_then_final_drain_catches_up(tmp_path: Path) -> None:
    guest = MemoryFiles()
    content = b"x" * (18 * 1024 * 1024)
    guest.put(".ale-harness/stdout.log", content)
    mirror = collector(tmp_path, guest)

    assert await mirror.sync()
    assert guest.transferred == 8 * 1024 * 1024
    assert await mirror.sync()
    assert guest.transferred == 16 * 1024 * 1024
    assert await mirror.sync(final=True)
    assert guest.transferred == len(content)
    assert all(length is not None and length <= 256 * 1024 for _, _, length in guest.reads)
    assert (tmp_path / "stdout.log").read_bytes() == content


async def test_batch_budget_advances_across_large_files_and_wraps_the_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_budget = 512 * 1024
    monkeypatch.setattr(collection, "_POLL_FILE_BYTES", file_budget)
    monkeypatch.setattr(collection, "_POLL_SOURCE_BYTES", file_budget * 2)
    guest = MemoryFiles()
    relatives = [f".codex-ale/sessions/{name}.jsonl" for name in ("a", "b", "c")]
    payload = b"x" * (file_budget * 3)
    for relative in relatives:
        guest.put(relative, payload)
    mirror = collector(tmp_path, guest, native_logs=True)
    transferred = []
    for _ in range(5):
        guest.transferred = 0
        assert await mirror.sync()
        transferred.append(guest.transferred)
        assert guest.transferred <= file_budget * 2
        if len(transferred) == 2:
            assert (tmp_path / "native" / relatives[2]).stat().st_size == file_budget
    assert sum(transferred) == len(payload) * len(relatives)
    assert all((tmp_path / "native" / relative).read_bytes() == payload for relative in relatives)
    guest.reads.clear()
    assert not await mirror.sync()
    assert guest.reads == []


async def test_final_batch_cancellation_preserves_already_committed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(collection, "_POLL_SOURCE_BYTES", 512 * 1024)
    guest = MemoryFiles()
    first, second = ".codex-ale/sessions/a.jsonl", ".codex-ale/sessions/b.jsonl"
    payload = b"x" * (512 * 1024)
    guest.put(first, payload)
    guest.put(second, payload)
    mirror = collector(tmp_path, guest, native_logs=True)
    original_read = guest.read_file
    blocked = asyncio.Event()

    async def block_second_file(path: str, *, offset: int = 0, length: int | None = None) -> bytes:
        if path == guest.path(second):
            blocked.set()
            await asyncio.Future()
        return await original_read(path, offset=offset, length=length)

    monkeypatch.setattr(guest, "read_file", block_second_file)
    task = asyncio.create_task(mirror.sync(final=True))
    await asyncio.wait_for(blocked.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / "native" / first).read_bytes() == payload
    assert not (tmp_path / "native" / second).exists()
    assert not list(tmp_path.rglob("*.pull"))
    monkeypatch.setattr(guest, "read_file", original_read)
    guest.reads.clear()
    assert await mirror.sync(final=True)
    assert {path for path, _, _ in guest.reads} == {guest.path(second)}
    assert (tmp_path / "native" / second).read_bytes() == payload


async def test_large_log_append_transfer_and_allocation_measurements(tmp_path: Path) -> None:
    guest = MemoryFiles()
    initial = b"x" * (32 * 1024 * 1024)
    guest.put(".ale-harness/stdout.log", initial)
    mirror = collector(tmp_path, guest)
    assert await mirror.sync(final=True)
    suffix = b"y" * (2 * 1024 * 1024)
    guest.put(".ale-harness/stdout.log", initial + suffix)
    guest.reads.clear()
    guest.transferred = 0

    tracemalloc.start()
    started = time.perf_counter()
    try:
        assert await mirror.sync()
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(
        f"32 MiB history + 2 MiB append: {guest.transferred} bytes transferred, "
        f"{len(guest.reads)} reads, {peak} peak Python bytes, {elapsed:.4f}s"
    )
    assert guest.transferred == len(suffix)
    assert len(guest.reads) == 8
    assert all(length == 256 * 1024 for _, _, length in guest.reads)
    # A generous memory bound catches accidentally loading historical logs into RAM.
    assert peak < 4 * 1024 * 1024
    assert (tmp_path / "stdout.log").stat().st_size == len(initial) + len(suffix)


@pytest.mark.parametrize("harness", ["claude-code", "codex-cli", "grok-build", "openclaw-cli"])
def test_acceptance_native_lookup_requires_exact_root_session(tmp_path: Path, harness: str) -> None:
    paths = {
        "claude-code": ".claude-config/projects/project/{session}.jsonl",
        "codex-cli": ".codex-ale/sessions/date/{session}.jsonl",
        "grok-build": ".grok-ale/sessions/{session}/chat_history.jsonl",
        "openclaw-cli": ".openclaw-ale/agents/main/sessions/{session}.jsonl",
    }
    expected = None
    for session in ("root", "child"):
        # Codex filenames deliberately disagree with IDs: the native header decides.
        filename = "child" if harness == "codex-cli" and session == "root" else session
        if harness == "codex-cli" and session == "child":
            filename = "root"
        path = tmp_path / "native" / paths[harness].format(session=filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n")
        if session == "root":
            expected = path
    assert native_session_file(tmp_path, harness, "root") == expected
    with pytest.raises(AssertionError):
        native_session_file(tmp_path, harness, "missing")
