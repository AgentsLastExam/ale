"""What tells an OOM kill in a Sandbox from inside it, the counts its cgroup, or gVisor's
Sentry, keeps from its start, and the end of a guest connection that says so."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from orbit.core.errors import GuestUnreachableError, ProviderStartError
from orbit.run.providers import memory
from orbit.run.transport import StdioTransport

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("counts", "seen"),
    [
        # Under Kata the microVM runs out before the container's cgroup reaches its limit.
        ("oom 0 oom_kill 5", "5 OOM kills and 0 times"),
        ("oom 2 oom_kill 0", "0 OOM kills and 2 times"),
        ("oom 0 oom_kill 0", None),
    ],
)
async def test_a_guest_watcher_counts_every_oom_kill_since_its_sandbox_started(
    counts: str, seen: str | None
) -> None:
    """The watcher's session is read until it ends with its Sandbox; what it wrote when it
    started counts as well, since the cgroup counts from the Sandbox's start: here the only
    line it writes."""
    watcher = await memory.GuestEvents.start(
        ["sh", "-c", f"echo {counts} oom_group_kill 0; sleep 0.2"], counter="its cgroup"
    )
    try:
        await watcher.settled(5)
        assert watcher.oom_killed() == (
            seen and f"its cgroup counted {seen} its memory reached its limit"
        )
    finally:
        await watcher.close()


@pytest.mark.asyncio
async def test_a_guest_watcher_reads_again_a_file_whose_changes_nothing_announces(
    tmp_path: Path,
) -> None:
    """The Sentry's ``memory.oom_control`` announces no change, as a regular file does not:
    the watcher reads it again every second and writes each change once. The Sentry counts
    no ``oom``."""
    counts = tmp_path / "memory.oom_control"
    counts.write_text("oom_kill_disable 0\nunder_oom 0\noom_kill 0\n")
    watcher = await memory.GuestEvents.start(
        [sys.executable, "-I", "-c", memory.WATCHER, str(counts)], counter="the Sentry"
    )
    try:
        assert watcher.oom_killed() is None
        counts.write_text("oom_kill_disable 0\nunder_oom 0\noom_kill 2\n")
        deadline = asyncio.get_running_loop().time() + 5
        while watcher.oom_killed() is None:
            assert asyncio.get_running_loop().time() < deadline, "the count never changed"
            await asyncio.sleep(0.05)
        assert watcher.oom_killed() == "the Sentry counted 2 OOM kills"
    finally:
        await watcher.close()


@pytest.mark.asyncio
async def test_a_guest_watcher_that_never_writes_fails_the_start() -> None:
    with pytest.raises(ProviderStartError, match="could not watch the guest's memory: absent"):
        await memory.GuestEvents.start(["sh", "-c", "echo absent >&2"], counter="its cgroup")


@pytest.mark.asyncio
async def test_a_reply_cut_short_ends_the_stream() -> None:
    """A guest killed between two writes of one line leaves part of it: the session ended,
    which the transport explains, rather than a reply that cannot be read."""
    transport = StdioTransport(["sh", "-c", 'printf \'{"id": 1, "ok"\''])
    await transport.start()
    try:
        with pytest.raises(GuestUnreachableError, match="guest session closed"):
            await transport.recv()
    finally:
        await transport.close()
