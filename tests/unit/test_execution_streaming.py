"""Stage failures retain progressive output and policy diagnostics."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orbit.core.episode import Phase
from orbit.core.errors import PhaseTimeoutError, TaskError, VerifierOutputError
from orbit.core.sandbox import ExecResult, SandboxFile
from orbit.core.task import ResolvedVariables, Stage
from orbit.core.taskspec import Healthcheck, NetworkMode, NetworkPolicy, OperatingSystem, VerifySpec
from orbit.core.trace import read_jsonl
from orbit.run.harnesses.builtin import NopHarness
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.recording import EpisodeRecording

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
        self.egress: list[str] = []

    async def write_file(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        return None

    async def exec(self, *args, output_sink=None, **kwargs):  # type: ignore[no-untyped-def]
        self.env = kwargs.get("env")
        self.argv, self.cwd = args[0], kwargs.get("cwd")
        for stream, data in self.chunks:
            if output_sink is not None:
                await output_sink(stream, data)
        return self.result

    async def close_egress(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.egress.append("close")

    async def open_egress(self) -> None:
        self.egress.append("open")

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
        spec=SimpleNamespace(
            network=NetworkPolicy(), os=OperatingSystem.LINUX, verify=VerifySpec()
        ),
        variables=ResolvedVariables(),
        secrets=(),
        verification_command_env={},
        verification_secrets=(),
        phase_timeout_sec=1.0,
        phase_deadline=None,
        run_dir=tmp_path,
        phases=[],
        open_egress_session=None,
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
            Path("/opt/orbit/setup"),
            Phase.SETUP,  # type: ignore[arg-type]
            stage=Stage.SETUP,
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
            Path("/opt/orbit/setup"),
            Phase.SETUP,  # type: ignore[arg-type]
            stage=Stage.SETUP,
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
    granted: list[NetworkPolicy] = []
    ctx.open_egress_session = granted.append
    with pytest.raises(RuntimeError, match="cannot seal"):
        await protocol._apply_network(
            ctx,  # type: ignore[arg-type]
            StageSandbox(
                ExecResult(exit_code=0),
                close_error=RuntimeError("cannot seal"),
            ),  # type: ignore[arg-type]
            NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("pypi.org",)),
            Phase.AGENT,
        )
    policy = read_jsonl(tmp_path / "trace.execution.jsonl").records[-1]
    assert policy["kind"] == "policy_applied"
    assert policy["succeeded"] is False
    # A policy that failed to apply gives the egress session no hosts.
    assert granted == []


@pytest.mark.asyncio
async def test_each_phase_applies_and_records_its_own_network(tmp_path: Path) -> None:
    """A solver that serves a shared verifier applies each phase's policy in turn, records
    the one it applied, and gives the egress session only that phase's policy."""
    ctx = context(tmp_path)
    granted: list[NetworkPolicy] = []
    ctx.open_egress_session = granted.append
    sandbox = StageSandbox(ExecResult(exit_code=0))
    protocol = EpisodeProtocol(NopHarness())
    pypi = NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("pypi.org",))
    for policy, phase in (
        (NetworkPolicy(mode=NetworkMode.OPEN), Phase.AGENT),
        (pypi, Phase.VALIDATE),
        (NetworkPolicy(), Phase.VERIFY),
    ):
        await protocol._apply_network(ctx, sandbox, policy, phase)  # type: ignore[arg-type]
    assert sandbox.egress == ["open", "close", "close"]
    assert granted == [NetworkPolicy(mode=NetworkMode.OPEN), pypi, NetworkPolicy()]
    applied = [
        (record["phase"], record["policy"], record["requested_mode"], record["succeeded"])
        for record in read_jsonl(tmp_path / "trace.execution.jsonl").records
        if record["kind"] == "policy_applied"
    ]
    assert applied == [
        ("agent", "open", "open", True),
        ("validate", "sealed", "allowlist", True),
        ("verify", "sealed", "block", True),
    ]


@pytest.mark.asyncio
async def test_verifier_learns_how_much_of_its_phase_is_left(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    sandbox = StageSandbox(ExecResult(exit_code=0))
    protocol = EpisodeProtocol(NopHarness())
    await protocol._run_stage(
        ctx, sandbox, Path("/opt/orbit/tests"), Phase.VERIFY, stage=Stage.TESTS
    )  # type: ignore[arg-type]
    assert "ORBIT_VERIFY_DEADLINE_SECONDS" not in sandbox.env

    ctx.phase_deadline = time.monotonic() + 100
    await protocol._run_stage(
        ctx, sandbox, Path("/opt/orbit/tests"), Phase.VERIFY, stage=Stage.TESTS
    )  # type: ignore[arg-type]
    assert 98 <= int(sandbox.env["ORBIT_VERIFY_DEADLINE_SECONDS"]) <= 100


@pytest.mark.asyncio
async def test_selected_verifier_python_is_only_exposed_during_verification(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    sandbox = StageSandbox(ExecResult(exit_code=0))
    protocol = EpisodeProtocol(NopHarness())
    await protocol._run_stage(
        ctx,
        sandbox,
        Path("/opt/orbit/setup"),
        Phase.SETUP,  # type: ignore[arg-type]
        stage=Stage.SETUP,
    )
    assert "ORBIT_VERIFY_PYTHON" not in sandbox.env
    await protocol._run_stage(
        ctx,
        sandbox,
        Path("/opt/orbit/tests"),
        Phase.VERIFY,
        stage=Stage.TESTS,
        verify_python="python3.12",  # type: ignore[arg-type]
    )
    assert sandbox.env["ORBIT_VERIFY_PYTHON"] == "python3.12"


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
            Path("/opt/orbit/setup"),
            Phase.SETUP,
            stage=Stage.SETUP,
        )

    await asyncio.gather(run("episode-a"), run("episode-b"))
    for name in ("episode-a", "episode-b"):
        records = read_jsonl(tmp_path / name / "trace.execution.jsonl").records
        output = records[-1]["stdout"]["inline"]
        assert output == name


@pytest.mark.asyncio
@pytest.mark.parametrize(("workdir", "cwd"), [("/app", "/app"), ("project", "/home/user/project")])
async def test_a_harbor_workdir_is_where_verification_runs(
    tmp_path: Path, workdir: str, cwd: str
) -> None:
    ctx = context(tmp_path)
    sandbox = StageSandbox(ExecResult(exit_code=0))
    protocol = EpisodeProtocol(NopHarness())
    await protocol._run_stage(
        ctx, sandbox, Path("/opt/orbit/tests"), Phase.VERIFY, stage=Stage.TESTS
    )  # type: ignore[arg-type]
    assert (sandbox.cwd, sandbox.argv) == ("/opt/orbit/tests", ["bash", "test.sh"])
    ctx.spec.verify = VerifySpec(workdir=workdir)
    await protocol._run_stage(
        ctx, sandbox, Path("/opt/orbit/tests"), Phase.VERIFY, stage=Stage.TESTS
    )  # type: ignore[arg-type]
    assert (sandbox.cwd, sandbox.argv) == (cwd, ["bash", "/opt/orbit/tests/test.sh"])
    assert sandbox.env["ORBIT_STAGE_DIR"] == "/opt/orbit/tests"
    # Setup keeps its own directory: Harbor's working directory is the agent's and the
    # verifier's.
    await protocol._run_stage(
        ctx, sandbox, Path("/opt/orbit/setup"), Phase.SETUP, stage=Stage.SETUP
    )  # type: ignore[arg-type]
    assert sandbox.cwd == "/opt/orbit/setup"


class CheckedSandbox:
    """Answers each healthcheck attempt with the next exit code."""

    def __init__(self, *codes: int) -> None:
        self.codes = list(codes)
        self.calls: list[tuple[list[str], str | None]] = []

    async def exec(self, argv, *, cwd=None, output_sink=None, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((argv, cwd))
        return ExecResult(exit_code=self.codes.pop(0))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("codes", "check", "attempts", "passes"),
    [
        ((0,), {}, 1, True),
        ((1, 1, 0), {}, 3, True),
        ((1, 1, 1), {}, 3, False),
        ((1, 0), {"retries": 1}, 1, False),
        # Failures during the start period count against nothing.
        ((1, 1, 1, 1, 0), {"retries": 1, "start_period_sec": 3600}, 5, True),
    ],
)
async def test_the_healthcheck_follows_harbor_s_docker_semantics(
    tmp_path: Path, monkeypatch, codes, check, attempts: int, passes: bool
) -> None:
    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr("orbit.run.protocol.episode_protocol.asyncio.sleep", no_wait)
    ctx = context(tmp_path)
    ctx.spec.healthcheck = Healthcheck(command="curl -sf localhost:8080", **check)
    ctx.spec.workdir = None
    sandbox = CheckedSandbox(*codes)
    protocol = EpisodeProtocol(NopHarness())
    if passes:
        await protocol._healthcheck(ctx, sandbox)  # type: ignore[arg-type]
    else:
        with pytest.raises(TaskError, match="healthcheck failed after"):
            await protocol._healthcheck(ctx, sandbox)  # type: ignore[arg-type]
    assert len(sandbox.calls) == attempts
    assert sandbox.calls[0] == (["bash", "-c", "curl -sf localhost:8080"], None)


@pytest.mark.asyncio
async def test_harbor_s_verifier_user_runs_verification_and_not_setup(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    ctx.spec.verify = VerifySpec(user="1000")
    sandbox = StageSandbox(ExecResult(exit_code=0))
    protocol = EpisodeProtocol(NopHarness())
    await protocol._run_stage(
        ctx, sandbox, Path("/opt/orbit/tests"), Phase.VERIFY, stage=Stage.TESTS
    )  # type: ignore[arg-type]
    python, flag, _drop, user, *command = sandbox.argv
    assert (python, flag, user, command) == (
        "/opt/orbit-python/bin/python3",
        "-c",
        "1000",
        ["bash", "test.sh"],
    )
    await protocol._run_stage(
        ctx, sandbox, Path("/opt/orbit/setup"), Phase.SETUP, stage=Stage.SETUP
    )  # type: ignore[arg-type]
    assert sandbox.argv == ["bash", "setup.sh"]
