"""Progressive guest output and bounded host previews."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import sys
import threading
import time
from typing import Any

import pytest

from ale.core.errors import OutputStreamError
from ale.run.guestd.main import Handler, _Server, _TCPHandler
from ale.run.transport import GuestClient

pytestmark = pytest.mark.unit


def test_tcp_guest_replies_without_client_eof() -> None:
    with _Server(("127.0.0.1", 0), _TCPHandler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.settimeout(2)
                replies = client.makefile("rb")
                token = "t" * 43
                client.sendall(b'{"id":1,"op":"auth","params":{"token":"%s"}}\n' % token.encode())
                assert json.loads(replies.readline())["ok"] is True
                client.sendall(b'{"id":2,"op":"health","params":{}}\n')
                assert json.loads(replies.readline())["ok"] is True
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_guest_emits_output_before_the_process_finishes() -> None:
    events: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    handler = Handler(events.append)

    def run() -> None:
        result.update(
            handler.dispatch(
                1,
                "exec",
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        "import time; print('first', flush=True); time.sleep(.4); print('last')",
                    ]
                },
            )
        )

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1
    while not events and time.monotonic() < deadline:
        time.sleep(0.01)
    assert events and thread.is_alive()
    thread.join(timeout=2)
    assert result["data"]["exit_code"] == 0


def test_guest_timeout_drains_output_and_returns_a_typed_outcome() -> None:
    events: list[dict[str, Any]] = []
    result = Handler(events.append).dispatch(
        1,
        "exec",
        {
            "argv": [
                sys.executable,
                "-c",
                "import time; print('before timeout', flush=True); time.sleep(10)",
            ],
            "timeout_sec": 0.1,
        },
    )
    assert result["data"] == {"exit_code": None, "timed_out": True}
    text = b"".join(
        base64.b64decode(item["data"]["b64"]) for item in events if item["event"] == "stdout_chunk"
    )
    assert b"before timeout" in text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group cleanup")
def test_guest_timeout_reaps_children_after_launcher_exit() -> None:
    started = time.monotonic()
    result = Handler(lambda event: None).dispatch(
        1,
        "exec",
        {
            "argv": [
                sys.executable,
                "-c",
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])",
            ],
            "timeout_sec": 0.2,
        },
    )
    assert result["data"] == {"exit_code": None, "timed_out": True}
    assert time.monotonic() - started < 2


class FakeTransport:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = list(messages)
        self.read = 0

    async def send(self, line: str) -> None:
        return None

    async def recv(self) -> str:
        self.read += 1
        return json.dumps(self.messages.pop(0))

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actions", "expected_timeout"),
    [
        ([{"type": "click"}], 120),
        ([{"type": "wait", "duration_ms": 0}], 120),
        ([{"type": "wait", "duration_ms": None}], 120.5),
        ([{"type": "wait", "duration_ms": 150_000}], 270),
        ([{"type": "wait", "duration_ms": 300_000}], 420),
        (
            [{"type": "wait", "duration_ms": 150_000}, {"type": "wait", "duration_ms": 300_000}],
            570,
        ),
    ],
)
async def test_input_timeout_allows_requested_waits(monkeypatch, actions, expected_timeout):
    timeouts = []
    wait_for = asyncio.wait_for

    async def observe_timeout(awaitable, *, timeout):
        timeouts.append(timeout)
        return await wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", observe_timeout)
    transport = FakeTransport([{"id": 1, "ok": True, "data": {"applied": len(actions)}}])
    assert await GuestClient(transport).inject_input(actions) == len(actions)
    assert timeouts == [expected_timeout]


@pytest.mark.asyncio
async def test_long_input_wait_remains_cancellable():
    receiving = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingTransport(FakeTransport):
        async def recv(self) -> str:
            receiving.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("unreachable")

    task = asyncio.create_task(
        GuestClient(WaitingTransport([])).inject_input([{"type": "wait", "duration_ms": 300_000}])
    )
    await asyncio.wait_for(receiving.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_host_streams_immediately_and_keeps_a_bounded_preview() -> None:
    block = b"x" * (40 * 1024)
    messages = [
        {
            "id": 1,
            "event": "stdout_chunk",
            "data": {"b64": base64.b64encode(block).decode()},
        },
        {
            "id": 1,
            "event": "stdout_chunk",
            "data": {"b64": base64.b64encode(block).decode()},
        },
        {"id": 1, "ok": True, "data": {"exit_code": 0, "timed_out": False}},
    ]
    seen = bytearray()

    async def sink(stream: str, data: bytes) -> None:
        assert stream == "stdout"
        seen.extend(data)

    code, stdout, stderr, timed_out = await GuestClient(FakeTransport(messages)).exec(
        ["demo"], output_sink=sink
    )
    assert code == 0 and not timed_out and not stderr
    assert len(seen) == 80 * 1024
    assert len(stdout.encode()) == 64 * 1024

    broken = FakeTransport([*messages[:1], messages[-1]])

    async def fail(stream: str, data: bytes) -> None:
        raise RuntimeError("disk full")

    with pytest.raises(OutputStreamError, match="disk full"):
        await GuestClient(broken).exec(["demo"], output_sink=fail)
    assert broken.read == 2


@pytest.mark.asyncio
async def test_cancellation_during_output_delivery_reaches_the_provider() -> None:
    transport = FakeTransport(
        [
            {"id": 1, "event": "stdout_chunk", "data": {"b64": "eA=="}},
            {"id": 1, "ok": True, "data": {"exit_code": 0}},
        ]
    )
    delivering = asyncio.Event()

    async def sink(stream: str, data: bytes) -> None:
        delivering.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(GuestClient(transport).exec(["demo"], output_sink=sink))
    await asyncio.wait_for(delivering.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_acknowledged_exec_waits_for_the_sink_and_drains_after_sink_error() -> None:
    sent = []
    delivered = asyncio.Event()
    release = asyncio.Event()

    class RecordingTransport(FakeTransport):
        async def send(self, line: bytes) -> None:
            sent.append(json.loads(line))

    transport = RecordingTransport(
        [
            {"id": 1, "event": "stdout_chunk", "data": {"b64": "eA==", "seq": 1}},
            {"id": 1, "event": "stderr_chunk", "data": {"b64": "eQ==", "seq": 2}},
            {"id": 1, "ok": True, "data": {"exit_code": 0, "timed_out": False}},
        ]
    )

    async def sink(stream: str, data: bytes) -> None:
        delivered.set()
        await release.wait()
        raise RuntimeError("disk full")

    client = GuestClient(transport, acknowledge_exec_output=True)
    task = asyncio.create_task(client.exec(["demo"], output_sink=sink))
    await asyncio.wait_for(delivered.wait(), 1)
    assert sent == [{"id": 1, "op": "exec", "params": {"argv": ["demo"], "output_ack": True}}]
    release.set()
    with pytest.raises(OutputStreamError, match="disk full"):
        await task
    assert sent[1:] == [
        {"id": 1, "op": "exec_output_ack", "seq": 1},
        {"id": 1, "op": "exec_output_ack", "seq": 2},
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Orchard output flow control uses POSIX pipes")
async def test_acknowledged_guest_streams_large_stdout_and_stderr() -> None:
    from pathlib import Path

    from ale.run.guestd import main as guest_main
    from ale.run.transport import StdioTransport

    transport = StdioTransport([sys.executable, str(Path(guest_main.__file__)), "--stdio"])
    await transport.start()
    client = GuestClient(transport, acknowledge_exec_output=True)
    seen = {"stdout": bytearray(), "stderr": bytearray()}

    async def sink(stream: str, block: bytes) -> None:
        assert len(block) <= 64 * 1024
        seen[stream].extend(block)

    try:
        result = await asyncio.wait_for(
            client.exec(
                [
                    sys.executable,
                    "-c",
                    "import os; os.write(1,b'x'*1048576); os.write(2,b'y'*131072)",
                ],
                timeout_sec=5,
                output_sink=sink,
            ),
            8,
        )
        assert result[0] == 0 and result[3] is False
        assert seen == {"stdout": b"x" * 1048576, "stderr": b"y" * 131072}
        assert len(result[1]) == len(result[2]) == 64 * 1024
        assert (await client.health())["proto"] >= 2
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Orchard output flow control uses POSIX pipes")
async def test_guest_timeout_interrupts_ack_wait_and_accepts_the_late_ack(tmp_path) -> None:
    import os
    from pathlib import Path

    from ale.run.guestd import main as guest_main
    from ale.run.transport import StdioTransport

    transport = StdioTransport([sys.executable, str(Path(guest_main.__file__)), "--stdio"])
    await transport.start()
    pidfile = tmp_path / "child.pid"
    script = (
        f"import os,time,pathlib; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
        "print('ready',flush=True); time.sleep(20)"
    )
    try:
        await transport.send(
            json.dumps(
                {
                    "id": 1,
                    "op": "exec",
                    "params": {
                        "argv": [sys.executable, "-c", script],
                        "output_ack": True,
                        "timeout_sec": 0.2,
                    },
                }
            ).encode()
            + b"\n"
        )
        first = json.loads(await asyncio.wait_for(transport.recv(), 2))
        assert first["event"] == "stdout_chunk" and first["data"]["seq"] == 1
        final = json.loads(await asyncio.wait_for(transport.recv(), 2))
        assert final["data"] == {"exit_code": None, "timed_out": True}
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
        await transport.send(b'{"id":1,"op":"exec_output_ack","seq":1}\n')
        await transport.send(b'{"id":2,"op":"health","params":{}}\n')
        healthy = json.loads(await asyncio.wait_for(transport.recv(), 2))
        assert healthy["id"] == 2 and healthy["ok"] is True
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Orchard output flow control uses POSIX pipes")
@pytest.mark.parametrize("disconnect", [False, True])
async def test_ack_failure_terminates_the_child_without_an_orphan(tmp_path, disconnect) -> None:
    import os
    from pathlib import Path

    from ale.core.errors import GuestUnreachableError
    from ale.run.guestd import main as guest_main
    from ale.run.transport import StdioTransport

    transport = StdioTransport([sys.executable, str(Path(guest_main.__file__)), "--stdio"])
    await transport.start()
    pidfile = tmp_path / "child.pid"
    script = (
        f"import os,time,pathlib; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
        "print('ready',flush=True); time.sleep(20)"
    )
    try:
        await transport.send(
            json.dumps(
                {
                    "id": 1,
                    "op": "exec",
                    "params": {
                        "argv": [sys.executable, "-c", script],
                        "output_ack": True,
                    },
                }
            ).encode()
            + b"\n"
        )
        first = json.loads(await asyncio.wait_for(transport.recv(), 2))
        assert first["data"]["seq"] == 1
        if disconnect:
            await transport.close()
        else:
            await transport.send(b'{"id":1,"op":"exec_output_ack","seq":99}\n')
            error = json.loads(await asyncio.wait_for(transport.recv(), 2))
            assert error["ok"] is False
            with pytest.raises(GuestUnreachableError, match="session closed"):
                await asyncio.wait_for(transport.recv(), 2)
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_acknowledged_output_cancellation_does_not_ack_an_unconsumed_block() -> None:
    sent = []
    delivering = asyncio.Event()

    class RecordingTransport(FakeTransport):
        async def send(self, line: bytes) -> None:
            sent.append(json.loads(line))

    transport = RecordingTransport(
        [
            {"id": 1, "event": "stdout_chunk", "data": {"b64": "eA==", "seq": 1}},
        ]
    )

    async def sink(stream: str, data: bytes) -> None:
        delivering.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        GuestClient(transport, acknowledge_exec_output=True).exec(
            ["demo"],
            output_sink=sink,
        )
    )
    await asyncio.wait_for(delivering.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sequence", [None, 0, 2, True])
async def test_acknowledged_output_rejects_missing_or_wrong_sequence(sequence) -> None:
    from ale.core.errors import GuestUnreachableError

    transport = FakeTransport(
        [
            {"id": 1, "event": "stdout_chunk", "data": {"b64": "eA==", "seq": sequence}},
        ]
    )
    with pytest.raises(GuestUnreachableError, match="acknowledgement sequence"):
        await GuestClient(transport, acknowledge_exec_output=True).exec(["demo"])
