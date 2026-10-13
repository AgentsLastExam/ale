"""Processes the agent leaves behind do not survive into evidence capture and verification.

The script tests point it at a private proc root containing only processes the test
spawned, so a terminate run can never reach anything else on the machine.
"""

from __future__ import annotations

import getpass
import json
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.errors import EnvironmentError_
from ale.core.sandbox import ExecResult
from ale.core.taskspec import NetworkPolicy, OperatingSystem
from ale.run.environments.standard import _AGENT_PROCESSES, StandardEnvironment
from ale.run.episode import _Artifacts
from ale.run.harnesses.builtin import NopHarness
from ale.run.recording import EpisodeRecording

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs Linux /proc"),
]


def script(mode: str, baseline: Path, proc_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _AGENT_PROCESSES,
            mode,
            getpass.getuser(),
            str(baseline),
            str(proc_root),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_only_processes_started_after_the_snapshot_are_killed(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    baseline = tmp_path / "agent-processes.json"
    desktop = subprocess.Popen(["sleep", "60"])
    leftover: subprocess.Popen[bytes] | None = None
    try:
        (proc_root / str(desktop.pid)).symlink_to(f"/proc/{desktop.pid}")
        assert script("snapshot", baseline, proc_root).returncode == 0
        assert list(json.loads(baseline.read_text())) == [str(desktop.pid)]
        assert stat.S_IMODE(baseline.stat().st_mode) == 0o600

        leftover = subprocess.Popen(["sleep", "60"], start_new_session=True)
        (proc_root / str(leftover.pid)).symlink_to(f"/proc/{leftover.pid}")
        result = script("terminate", baseline, proc_root)

        assert result.returncode == 0, result.stderr
        assert leftover.wait(timeout=5) == -9
        assert desktop.poll() is None
    finally:
        for process in (desktop, leftover):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()


def test_without_an_agent_phase_there_is_nothing_to_terminate(tmp_path: Path) -> None:
    result = script("terminate", tmp_path / "missing.json", tmp_path)
    assert result.returncode == 0


class CaptureSandbox:
    def __init__(self, terminate_exit: int) -> None:
        self.request = SimpleNamespace(os=OperatingSystem.LINUX)
        self.agent_user = "user"
        self.terminate_exit = terminate_exit
        self.calls: list[str] = []

    async def exec(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        command = [str(part) for part in argv]
        if command[:2] == ["python3", "-c"] and command[2] == _AGENT_PROCESSES:
            self.calls.append(f"agent-processes:{command[3]}")
            return ExecResult(exit_code=self.terminate_exit, stderr="agent processes survived: 7")
        self.calls.append("artifact-probe")
        return ExecResult(exit_code=0, stdout=json.dumps({"kind": "file", "mode": 0o644}))

    async def read_file(self, path):  # type: ignore[no-untyped-def]
        self.calls.append("artifact-read")
        return b"output"


def capture_context(tmp_path: Path, operating_system: OperatingSystem) -> SimpleNamespace:
    recording = EpisodeRecording(tmp_path)
    return SimpleNamespace(
        solver_evidence_captured=False,
        execution=recording.execution,
        blobs=recording.blobs,
        episode_id="episode",
        session=SimpleNamespace(token=""),
        spec=SimpleNamespace(
            os=operating_system,
            artifacts=("/home/user/output.txt",),
            network=NetworkPolicy(),
        ),
        home="/home/user",
        agent_started=False,
        trajectory_written=False,
        harness_execution_counter=0,
        artifacts=_Artifacts(tmp_path),
    )


@pytest.mark.asyncio
async def test_leftovers_are_terminated_before_any_artifact_is_read(tmp_path: Path) -> None:
    sandbox = CaptureSandbox(terminate_exit=0)
    environment = StandardEnvironment(NopHarness(), agent_enabled=False)
    await environment._capture_solver_evidence(
        capture_context(tmp_path, OperatingSystem.LINUX),  # type: ignore[arg-type]
        sandbox,  # type: ignore[arg-type]
    )
    assert sandbox.calls[0] == "agent-processes:terminate"
    assert sandbox.calls[1:] == ["artifact-probe", "artifact-read"]


@pytest.mark.asyncio
async def test_surviving_leftovers_fail_closed_before_evidence_capture(tmp_path: Path) -> None:
    sandbox = CaptureSandbox(terminate_exit=1)
    environment = StandardEnvironment(NopHarness(), agent_enabled=False)
    with pytest.raises(EnvironmentError_, match="survived"):
        await environment._capture_solver_evidence(
            capture_context(tmp_path, OperatingSystem.LINUX),  # type: ignore[arg-type]
            sandbox,  # type: ignore[arg-type]
        )
    assert sandbox.calls == ["agent-processes:terminate"]


@pytest.mark.asyncio
async def test_windows_sandboxes_are_not_scanned(tmp_path: Path) -> None:
    sandbox = CaptureSandbox(terminate_exit=1)
    sandbox.request = SimpleNamespace(os=OperatingSystem.WINDOWS)
    context = capture_context(tmp_path, OperatingSystem.WINDOWS)
    context.spec.artifacts = ()
    await StandardEnvironment(NopHarness(), agent_enabled=False)._capture_solver_evidence(
        context,  # type: ignore[arg-type]
        sandbox,  # type: ignore[arg-type]
    )
    assert sandbox.calls == []
