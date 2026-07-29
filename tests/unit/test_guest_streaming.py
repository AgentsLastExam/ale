"""Progressive guest output and bounded host previews."""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
from typing import Any

import pytest

from ale.core.errors import OutputStreamError
from ale.run.guestd.main import Handler
from ale.run.transport import GuestClient

pytestmark = pytest.mark.unit


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
