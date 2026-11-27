from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from orbit.core.errors import ProviderStartError
from orbit.run import process as process_module
from orbit.run.process import end, run

pytestmark = pytest.mark.unit


def _sleeper(pid_file: Path) -> tuple[str, ...]:
    """A program that records its process ID at once and then sleeps."""
    return ("sh", "-c", f"echo $$ > '{pid_file}'; exec sleep 60")


async def test_a_program_is_run_with_its_input_and_environment() -> None:
    code, stdout, stderr = await run(
        sys.executable,
        "-c",
        "import os, sys; print(sys.stdin.read() + os.environ['GREETING']); sys.exit(3)",
        stdin=b"hello ",
        env={"GREETING": "world"},
        timeout=30,
    )
    assert (code, stdout, stderr) == (3, "hello world\n", "")


async def test_a_hung_program_is_the_environment_s_failure_and_is_reaped(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    with pytest.raises(ProviderStartError, match=r"timed out after 0\.5s"):
        await run(*_sleeper(pid_file), timeout=0.5)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_a_cancelled_caller_leaves_no_program_behind(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    task = asyncio.create_task(run(*_sleeper(pid_file), timeout=60))
    while not pid_file.exists() or not pid_file.read_text():
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_a_session_ended_by_its_input_exits_as_its_program_does() -> None:
    process = await asyncio.create_subprocess_exec(
        "sh", "-c", "cat > /dev/null; exit 7", stdin=asyncio.subprocess.PIPE
    )
    await end(process)
    assert process.returncode == 7


async def test_a_session_that_outlives_its_input_is_killed_unwaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even one whose output nobody reads any more, which would hold a wait for it."""
    monkeypatch.setattr(process_module, "END_TIMEOUT_SEC", 0.2)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys, time; sys.stdout.write('x' * 600_000); sys.stdout.flush(); time.sleep(60)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        limit=1024,
    )
    await asyncio.wait_for(end(process), 5)
    await process.communicate()
    assert process.returncode == -9


async def test_a_cancelled_end_leaves_no_session_behind(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    process = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        f"echo $$ > '{pid_file}'; trap '' HUP; exec sleep 60",
        stdin=asyncio.subprocess.PIPE,
    )
    while not pid_file.exists() or not pid_file.read_text():
        await asyncio.sleep(0.05)
    task = asyncio.create_task(end(process))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await process.wait()
    assert process.returncode == -9
