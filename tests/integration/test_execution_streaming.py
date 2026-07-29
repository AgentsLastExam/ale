"""Stage failures retain progressive output and policy diagnostics."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.environment import Phase
from ale.core.errors import PhaseTimeoutError, TaskError, VerifierOutputError
from ale.core.sandbox import ExecResult
from ale.core.taskspec import NetworkPolicy
from ale.core.trace import read_jsonl
from ale.run.environments.standard import StandardEnvironment
from ale.run.harnesses.builtin import NopHarness
from ale.run.recording import EpisodeRecording

pytestmark = pytest.mark.integration


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
        for stream, data in self.chunks:
            if output_sink is not None:
                await output_sink(stream, data)
        return self.result

    async def close_egress(self) -> None:
        if self.close_error is not None:
            raise self.close_error

    async def read_file(self, _path) -> bytes:  # type: ignore[no-untyped-def]
        if self.rewards is None:
            raise FileNotFoundError
        return self.rewards


def context(tmp_path: Path):
    recording = EpisodeRecording(tmp_path)
    return SimpleNamespace(
        execution=recording.execution,
        blobs=recording.blobs,
        episode_id=tmp_path.name,
        home="/home/user",
        session=SimpleNamespace(token=""),
        spec=SimpleNamespace(params={}, network=NetworkPolicy()),
        extras={"phase_timeout_sec": 1.0},
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
        await StandardEnvironment(NopHarness())._run_stage(
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
        await StandardEnvironment(NopHarness())._run_stage(
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
    environment = StandardEnvironment(NopHarness())
    with pytest.raises(VerifierOutputError):
        await environment._read_rewards(
            StageSandbox(ExecResult(exit_code=0), rewards=b'{"rewards": {}}')  # type: ignore[arg-type]
        )

    ctx = context(tmp_path)
    with pytest.raises(RuntimeError, match="cannot seal"):
        await environment._seal(
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
async def test_concurrent_episode_output_never_crosses_files(tmp_path: Path) -> None:
    async def run(name: str) -> None:
        root = tmp_path / name
        ctx = context(root)
        await StandardEnvironment(NopHarness())._run_stage(
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
