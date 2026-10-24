"""Concurrent native continuations remain bound to their own Claude session."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import PurePosixPath

import pytest

from ale.core.harness import HarnessSession
from ale.core.sandbox import ExecResult, SandboxFile
from ale.core.testkit import AutonomousHarnessConformance
from ale.run.harnesses.claude_code import ClaudeCodeHarness

pytestmark = pytest.mark.integration


class FakeClaudeSandbox:
    def __init__(self, index: int) -> None:
        self.sandbox_id = f"sandbox-{index}"
        self.prompts: list[str] = []
        self.selectors: list[tuple[str, str]] = []
        self.transcript = b""
        self.files: dict[str, bytes] = {}

    async def exec(self, argv, *, env=None, **kwargs):  # type: ignore[no-untyped-def]
        command = " ".join(str(part) for part in argv)
        if "find " in command or "claude " not in command:
            return ExecResult(exit_code=0)
        prompt = next(
            data.decode("utf-8")
            for path, data in self.files.items()
            if path.endswith("/instruction.md")
        )
        self.prompts.append(prompt)
        match = re.search(r"--(session-id|resume) ([^ ]+)", command)
        assert match
        selector, native_id = match.groups()
        native_id = native_id.strip("'")
        self.selectors.append((selector, native_id))
        line = (
            json.dumps({"type": "result", "session_id": native_id, "result": prompt}).encode()
            + b"\n"
        )
        self.transcript = self.transcript + line if " >> " in command else line
        path = next(path for path in self.files if path.endswith("/stdout.log"))
        self.files[path] = self.transcript
        return ExecResult(exit_code=0)

    async def write_file(self, path, data, **kwargs):  # type: ignore[no-untyped-def]
        self.files[str(path)] = data

    async def read_file(self, path, *, offset=0, length=None):  # type: ignore[no-untyped-def]
        data = self.files[str(path)]
        return data[offset:] if length is None else data[offset : offset + length]

    async def list_files(self, path):  # type: ignore[no-untyped-def]
        return tuple(
            SandboxFile(path=name, size=len(data), mtime_ns=1, file_id=name)
            for name, data in self.files.items()
            if PurePosixPath(name).is_relative_to(path)
        )


@pytest.mark.asyncio
async def test_twenty_three_segment_native_sessions_are_isolated() -> None:
    harness = ClaudeCodeHarness()

    async def run(index: int):  # type: ignore[no-untyped-def]
        sandbox = FakeClaudeSandbox(index)
        session = HarnessSession(
            episode_id=f"episode-{index}",
            gateway_url="http://gateway",
            token=f"token-{index}",
            model="claude-opus-4-8",
            sandbox_id=sandbox.sandbox_id,
            resources_digest="sha256:" + f"{index:064x}",
            home=f"/home/agent-{index}",
        )
        current = await harness.launch(
            f"{index}:one",
            sandbox,  # type: ignore[arg-type]
            session,
            timeout_sec=10,
        )
        for suffix in ("two", "three"):
            assert current.continuation is not None
            current = await harness.resume(
                f"{index}:{suffix}",
                current.continuation,
                sandbox,  # type: ignore[arg-type]
                session,
                timeout_sec=10,
            )
        assert current.continuation is not None
        AutonomousHarnessConformance.check_native_continuation(
            harness,
            session,
            current.continuation,
        )
        return sandbox, current.continuation.native_session_id

    results = await asyncio.gather(*(run(index) for index in range(20)))

    assert len({native_id for _, native_id in results}) == 20
    for index, (sandbox, native_id) in enumerate(results):
        assert sandbox.prompts == [f"{index}:one", f"{index}:two", f"{index}:three"]
        assert sandbox.selectors == [
            ("session-id", native_id),
            ("resume", native_id),
            ("resume", native_id),
        ]
