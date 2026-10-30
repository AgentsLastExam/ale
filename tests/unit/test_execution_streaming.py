"""Stage failures retain progressive output and policy diagnostics."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.episode import Phase
from ale.core.errors import PhaseTimeoutError, TaskError, VerifierOutputError
from ale.core.sandbox import ExecResult, SandboxFile
from ale.core.taskspec import NetworkPolicy, OperatingSystem
from ale.core.trace import read_jsonl
from ale.run.harnesses.builtin import NopHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.recording import EpisodeRecording

pytestmark = pytest.mark.unit


class StageSandbox:
    def __init__(
        self,
        result: ExecResult,
        *,
        chunks: tuple[tuple[str, bytes], ...] = (),
        close_error: Exception | None = None,
        rewards: bytes | None = None,
    ) -> None:
        self.result = result
        self.chunks = chunks
        self.close_error = close_error
        self.rewards = rewards

    async def write_file(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        return None

    async def exec(self, *args, output_sink=None, **kwargs):  # type: ignore[no-untyped-def]
        self.env = kwargs.get("env")
        for stream, data in self.chunks:
            if output_sink is not None:
                await output_sink(stream, data)
        return self.result

    async def close_egress(self) -> None:
        if self.close_error is not None:
            raise self.close_error

    async def list_files(self, path: str) -> tuple[SandboxFile, ...]:
        if self.rewards is None:
            return ()
        return (SandboxFile(path=f"{path}/reward.json", size=len(self.rewards)),)

    async def read_file(self, _path) -> bytes:  # type: ignore[no-untyped-def]
        assert self.rewards is not None
        return self.rewards


def context(tmp_path: Path):
    recording = EpisodeRecording(tmp_path)
    return SimpleNamespace(
        execution=recording.execution,
        blobs=recording.blobs,
        episode_id=tmp_path.name,
        home="/home/user",
        session=SimpleNamespace(token=""),
        spec=SimpleNamespace(network=NetworkPolicy(), os=OperatingSystem.LINUX),
        secrets=(),
        verification_command_env={},
        verification_secrets=(),
        phase_timeout_sec=1.0,
        phase_deadline=None,
        run_dir=tmp_path,
        phases=[],
    )


@pytest.mark.asyncio
async def test_setup_nonzero_keeps_received_stdout(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    sandbox = StageSandbox(
        ExecResult(exit_code=17, stderr="failed"),
        chunks=(("stdout", b"before failure\n"),),
    )
    with pytest.raises(TaskError, match="exit code 17"):
        await EpisodeProtocol(NopHarness())._run_stage(
            ctx,
            sandbox,
            Path("/opt/ale/setup"),
            Phase.SETUP,  # type: ignore[arg-type]
        )
    records = read_jsonl(tmp_path / "trace.execution.jsonl").records
    finished = next(record for record in records if record["kind"] == "command_finished")
    assert finished["outcome"] == "failed"
    assert finished["stdout"]["inline"] == "before failure\n"


@pytest.mark.asyncio
async def test_setup_timeout_keeps_received_output(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    sandbox = StageSandbox(
        ExecResult(exit_code=None, timed_out=True, duration_ms=1000),
        chunks=(("stderr", b"last diagnostic\n"),),
    )
    with pytest.raises(PhaseTimeoutError):
        await EpisodeProtocol(NopHarness())._run_stage(
            ctx,
            sandbox,
            Path("/opt/ale/setup"),
            Phase.SETUP,  # type: ignore[arg-type]
        )
    finished = read_jsonl(tmp_path / "trace.execution.jsonl").records[-1]
    assert finished["outcome"] == "timed_out"
    assert finished["stderr"]["inline"] == "last diagnostic\n"
    assert finished["stderr"]["complete"] is False


@pytest.mark.asyncio
async def test_malformed_verifier_and_network_failure_are_explicit(
    tmp_path: Path,
) -> None:
    protocol = EpisodeProtocol(NopHarness())
    with pytest.raises(VerifierOutputError):
        await protocol._read_result(
            context(tmp_path),
            StageSandbox(ExecResult(exit_code=0), rewards=b'{"rewards": {}}'),  # type: ignore[arg-type]
        )

    ctx = context(tmp_path)
    with pytest.raises(RuntimeError, match="cannot seal"):
        await protocol._seal(
            ctx,  # type: ignore[arg-type]
            StageSandbox(
                ExecResult(exit_code=0),
                close_error=RuntimeError("cannot seal"),
            ),  # type: ignore[arg-type]
        )
    policy = read_jsonl(tmp_path / "trace.execution.jsonl").records[-1]
    assert policy["kind"] == "policy_applied"
    assert policy["succeeded"] is False


@pytest.mark.asyncio
async def test_verifier_learns_how_much_of_its_phase_is_left(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    sandbox = StageSandbox(ExecResult(exit_code=0))
    protocol = EpisodeProtocol(NopHarness())
    await protocol._run_stage(ctx, sandbox, Path("/opt/ale/tests"), Phase.VERIFY)  # type: ignore[arg-type]
    assert "ALE_VERIFY_DEADLINE_SECONDS" not in sandbox.env

    ctx.phase_deadline = time.monotonic() + 100
    await protocol._run_stage(ctx, sandbox, Path("/opt/ale/tests"), Phase.VERIFY)  # type: ignore[arg-type]
    assert 98 <= int(sandbox.env["ALE_VERIFY_DEADLINE_SECONDS"]) <= 100


@pytest.mark.asyncio
async def test_selected_verifier_python_is_only_exposed_during_verification(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    sandbox = StageSandbox(ExecResult(exit_code=0))
    protocol = EpisodeProtocol(NopHarness())
    await protocol._run_stage(
        ctx,
        sandbox,
        Path("/opt/ale/setup"),
        Phase.SETUP,  # type: ignore[arg-type]
    )
    assert "ALE_VERIFY_PYTHON" not in sandbox.env
    await protocol._run_stage(
        ctx,
        sandbox,
        Path("/opt/ale/tests"),
        Phase.VERIFY,
        verify_python="python3.12",  # type: ignore[arg-type]
    )
    assert sandbox.env["ALE_VERIFY_PYTHON"] == "python3.12"


@pytest.mark.asyncio
async def test_concurrent_episode_output_never_crosses_files(tmp_path: Path) -> None:
    async def run(name: str) -> None:
        root = tmp_path / name
        ctx = context(root)
        await EpisodeProtocol(NopHarness())._run_stage(
            ctx,  # type: ignore[arg-type]
            StageSandbox(
                ExecResult(exit_code=0),
                chunks=(("stdout", name.encode()),),
            ),  # type: ignore[arg-type]
            Path("/opt/ale/setup"),
            Phase.SETUP,
        )

    await asyncio.gather(run("episode-a"), run("episode-b"))
    for name in ("episode-a", "episode-b"):
        records = read_jsonl(tmp_path / name / "trace.execution.jsonl").records
        output = records[-1]["stdout"]["inline"]
        assert output == name
