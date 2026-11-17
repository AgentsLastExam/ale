"""What tells an OOM kill in a Sandbox from inside it, the counts its cgroup keeps from its
start, and the end of a guest connection that says so."""

from __future__ import annotations

import pytest

from ale.core.errors import GuestUnreachableError, ProviderStartError
from ale.run.providers import memory
from ale.run.transport import StdioTransport

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
        ["sh", "-c", f"echo {counts} oom_group_kill 0; sleep 0.2"]
    )
    try:
        await watcher.settled(5)
        assert watcher.oom_killed() == (
            seen and f"its cgroup counted {seen} its memory reached its limit"
        )
    finally:
        await watcher.close()


@pytest.mark.asyncio
async def test_a_guest_watcher_that_never_writes_fails_the_start() -> None:
    with pytest.raises(ProviderStartError, match="could not watch the guest's memory: absent"):
        await memory.GuestEvents.start(["sh", "-c", "echo absent >&2"])


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
