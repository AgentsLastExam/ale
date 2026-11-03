"""Ceilings and deadlines end episodes; they do not hang them.

The failure this guards against is not "the limit was ignored" — it is an episode that
burns its whole budget and then stalls, holding a container and telling nobody why. So
each case asserts a *typed status*, promptly, with the partial evidence preserved. An agent
stopped early still has what it left verified, as in Harbor.
"""

from __future__ import annotations

import asyncio
import textwrap
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from aiohttp import web

from ale.core.config import RunTimeouts, SandboxRetentionConfig
from ale.core.errors import AgentError, AgentRefusalError, HarnessLimitError, PhaseTimeoutError
from ale.core.harness import AgentRun, AutonomousHarness, HarnessSession
from ale.core.sandbox import Identity, Sandbox
from ale.core.trace import read_jsonl
from ale.core.trajectory import AtifTrajectory
from ale.core.verdict import Status
from ale.run.episode import run_episode, run_episode_with_retries
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import Limits
from ale.run.harnesses.builtin import NopHarness, OracleHarness
from ale.run.protocol import episode_protocol
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import load_tasks
from tests.support import edit_task_config, provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

#: Well under any phase deadline these tests set, so a pass cannot come from slowness.
GRACE_SEC = 60


async def run_one(task_root: Path, run_dir: Path, harness: object):  # type: ignore[no-untyped-def]
    task = load_tasks(task_root)[0]
    return await run_episode(
        task,
        EpisodeProtocol(harness),  # type: ignore[arg-type]
        provider_registry(DockerProvider()),
        run_dir=run_dir,
    )


def set_timeout(task_root: Path, phase: str, seconds: int) -> None:
    table = "agent" if phase == "agent" else "verifier"
    edit_task_config(task_root, {table: {"timeout_sec": seconds}})


@pytest.mark.asyncio
async def test_a_hanging_setup_times_out(
    tmp_path: Path, write_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task that never finishes preparing is a task defect, reported as a timeout."""
    task_root = write_repo(tmp_path / "repo")
    (task_root / "environment" / "setup").mkdir()
    (task_root / "environment" / "setup" / "setup.sh").write_text(
        "#!/usr/bin/env bash\nsleep 600\n"
    )
    monkeypatch.setattr(episode_protocol, "SETUP_TIMEOUT_SEC", 5)

    started = time.monotonic()
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())
    elapsed = time.monotonic() - started

    assert result.verdict.status is Status.TIMEOUT
    assert result.verdict.rewards is None  # a timeout is not a zero
    assert elapsed < GRACE_SEC, "the deadline fired, but the episode did not end promptly"


@pytest.mark.asyncio
async def test_a_hanging_agent_times_out_and_keeps_its_trace(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """Partial evidence survives: an episode that ran out of time still explains itself."""
    task_root = write_repo(tmp_path / "repo")
    (task_root / "solution" / "solve.sh").write_text("#!/usr/bin/env bash\nsleep 600\n")
    set_timeout(task_root, "agent", 5)

    started = time.monotonic()
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())
    elapsed = time.monotonic() - started

    assert result.verdict.status is Status.TIMEOUT
    assert result.verdict.rewards == {"reward": 0.0}  # verified: it left nothing behind
    assert elapsed < GRACE_SEC

    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    assert trajectory.steps[0].source == "user"
    assert trajectory.extra["ale"]["incomplete"] is True
    assert any(phase.phase == "agent" for phase in result.record.phases)


@pytest.mark.asyncio
@pytest.mark.parametrize("environment_mode", ["shared", "separate"])
async def test_work_left_by_a_timed_out_agent_is_scored(
    tmp_path: Path, write_repo: Callable[..., Path], environment_mode: str
) -> None:
    task_root = write_repo(
        tmp_path / "repo", config={"verifier": {"environment_mode": environment_mode}}
    )
    solve = task_root / "solution" / "solve.sh"
    solve.write_text(solve.read_text() + "sleep 600\n")
    set_timeout(task_root, "agent", 5)

    result = await run_one(task_root, tmp_path / "runs", OracleHarness())

    assert result.verdict.status is Status.TIMEOUT
    assert result.verdict.failure.phase == "agent"
    assert result.verdict.rewards == {"reward": 1.0}
    assert result.record.status is Status.TIMEOUT
    assert result.record.rewards == {"reward": 1.0}


@pytest.mark.asyncio
async def test_a_failed_verification_leaves_the_agents_stop_as_the_outcome(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(tmp_path / "repo", test_body="#!/usr/bin/env bash\nexit 1\n")
    (task_root / "solution" / "solve.sh").write_text("#!/usr/bin/env bash\nsleep 600\n")
    set_timeout(task_root, "agent", 5)

    result = await run_one(task_root, tmp_path / "runs", OracleHarness())

    assert result.verdict.status is Status.TIMEOUT
    assert result.verdict.failure.phase == "agent"
    assert result.verdict.rewards is None


@pytest.mark.asyncio
async def test_runtime_agent_budget_excludes_install_and_verification(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(tmp_path / "repo")
    verifier = task_root / "tests/test.sh"
    verifier.write_text("sleep 3\n" + verifier.read_text())
    set_timeout(task_root, "verify", 1)

    class SlowInstallOracle(OracleHarness):
        async def install(self, sandbox: Sandbox) -> str:
            await asyncio.sleep(3)
            return await super().install(sandbox)

    result = await run_episode(
        load_tasks(task_root)[0],
        EpisodeProtocol(SlowInstallOracle(), timeouts=RunTimeouts(agent=2, verify="unlimited")),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
    )
    assert result.record.status is Status.COMPLETED
    durations = {phase.phase: phase.duration_ms for phase in result.record.phases}
    assert durations["agent"] < 2000
    assert durations["verify"] >= 3000


@pytest.mark.asyncio
async def test_runtime_agent_timeout_destroys_descendants_and_does_not_retry(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(tmp_path / "repo")
    (task_root / "solution/solve.sh").write_text("#!/usr/bin/env bash\nsleep 600 &\nwait\n")
    calls = 0

    async def execute():
        nonlocal calls
        calls += 1
        return await run_episode(
            load_tasks(task_root)[0],
            EpisodeProtocol(OracleHarness(), timeouts=RunTimeouts(agent=2, verify="unlimited")),
            provider_registry(DockerProvider()),
            run_dir=tmp_path / "runs",
            sandbox_retention=SandboxRetentionConfig(solver="keep"),
        )

    started = time.monotonic()
    result = await run_episode_with_retries(execute, retries=3)
    assert calls == 1
    assert result.record.status is Status.TIMEOUT
    assert result.record.failure.phase == "agent"
    assert "2" in result.record.failure.message
    assert time.monotonic() - started < GRACE_SEC
    assert result.record.sandboxes
    assert all(outcome.outcome == "destroyed" for outcome in result.record.sandboxes)
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "ps",
        "--filter",
        f"label=ale.episode={result.episode_id}",
        "--format",
        "{{.Names}}",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    assert not stdout.strip()


@pytest.mark.asyncio
async def test_a_hanging_verifier_times_out(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(tmp_path / "repo")
    (task_root / "tests" / "test.sh").write_text("#!/usr/bin/env bash\nsleep 600\n")
    set_timeout(task_root, "verify", 5)

    started = time.monotonic()
    result = await run_one(task_root, tmp_path / "runs", OracleHarness())

    assert result.verdict.status is Status.TIMEOUT
    assert time.monotonic() - started < GRACE_SEC


@pytest.mark.asyncio
async def test_a_timed_out_episode_leaves_no_container_behind(
    tmp_path: Path, write_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown is shielded, so the deadline reclaims the sandbox rather than leaking it."""
    task_root = write_repo(tmp_path / "repo")
    (task_root / "environment" / "setup").mkdir()
    (task_root / "environment" / "setup" / "setup.sh").write_text(
        "#!/usr/bin/env bash\nsleep 600\n"
    )
    monkeypatch.setattr(episode_protocol, "SETUP_TIMEOUT_SEC", 5)

    result = await run_one(task_root, tmp_path / "runs", OracleHarness())
    assert result.verdict.status is Status.TIMEOUT

    proc = await asyncio.create_subprocess_exec(
        "docker",
        "ps",
        "--filter",
        f"label=ale.episode={result.episode_id}",
        "--format",
        "{{.Names}}",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    assert not stdout.decode().strip(), "a container outlived its episode"


@pytest.mark.asyncio
async def test_a_crashing_setup_is_a_task_error_not_a_zero(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    """Distinguishing a broken task from a hard one is the point of the taxonomy."""
    task_root = write_repo(tmp_path / "repo")
    (task_root / "environment" / "setup").mkdir()
    (task_root / "environment" / "setup" / "setup.sh").write_text(
        textwrap.dedent("""
            #!/usr/bin/env bash
            set -euo pipefail
            echo "the data this task needs is not where it expected" >&2
            exit 3
        """).strip()
    )

    result = await run_one(task_root, tmp_path / "runs", NopHarness())

    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.rewards is None


class StoppedHarness(AutonomousHarness):
    """Leaves the right answer, then ends the way ``stop`` says."""

    name = "stopped"

    def __init__(self, stop: Exception) -> None:
        self.stop = stop

    def version(self) -> str:
        return "1"

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        await sandbox.exec(
            ["sh", "-c", "printf 'hello world' > /home/user/output/result.txt"],
            identity=Identity.AGENT,
        )
        raise self.stop


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "status"),
    [
        (PhaseTimeoutError("agent", 1), Status.TIMEOUT),
        (HarnessLimitError("max_turns", 1, 1), Status.BUDGET_EXCEEDED),
        (AgentError("crashed"), Status.AGENT_ERROR),
        (AgentRefusalError("declined"), Status.REFUSED),
    ],
)
async def test_every_early_stop_keeps_its_status_and_gets_a_score(
    tmp_path: Path, write_repo: Callable[..., Path], stop: Exception, status: Status
) -> None:
    result = await run_one(write_repo(tmp_path / "repo"), tmp_path / "runs", StoppedHarness(stop))

    assert result.verdict.status is status
    assert result.verdict.failure.phase == "agent"
    assert result.verdict.rewards == {"reward": 1.0}


class NativeLimitedHarness(AutonomousHarness):
    name = "native-limited"

    def version(self) -> str:
        return "1"

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        raise HarnessLimitError("max_turns", 1, 1)


class GatewayLimitedHarness(NativeLimitedHarness):
    name = "gateway-limited"

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        script = (
            "import json,sys,urllib.error,urllib.request\n"
            "url,token,text=sys.argv[1:]\n"
            "request=urllib.request.Request(url+'/v1/messages',"
            "data=json.dumps({'messages':[{'role':'user','content':text}],"
            "'max_tokens':10}).encode(),headers={'authorization':'Bearer '+token,"
            "'content-type':'application/json'})\n"
            "try:\n"
            " print(urllib.request.urlopen(request).read().decode())\n"
            "except urllib.error.HTTPError as error:\n"
            " print(error.read().decode());sys.exit(42)\n"
        )
        for text in ("first", "second"):
            result = await sandbox.exec(
                ["python3", "-c", script, session.gateway_url, session.token, text],
                identity=Identity.AGENT,
                timeout_sec=timeout_sec,
            )
            if result.exit_code == 42:
                # Like a real agent program: it reports a failed API call, not a limit.
                raise AgentError(f"API Error: 429 {result.stdout.strip()}")
        return AgentRun(exit_code=0)


async def _upstream() -> tuple[web.AppRunner, str]:
    async def messages(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "type": "message",
                "content": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_native_limit_records_layer_and_preserves_partial_evidence(
    tmp_path: Path,
    write_repo: Callable[..., Path],
) -> None:
    task_root = write_repo(tmp_path / "repo")
    result = await run_one(task_root, tmp_path / "runs", NativeLimitedHarness())
    assert result.verdict.status is Status.BUDGET_EXCEEDED
    trajectory = AtifTrajectory.model_validate_json(
        (result.run_dir / "trajectory.json").read_text()
    )
    expected = load_tasks(task_root)[0].spec.instruction
    assert trajectory.steps[0].message == expected
    assert result.verdict.failure is not None
    assert "max_turns" in result.verdict.failure.message


@pytest.mark.asyncio
async def test_gateway_limit_records_gateway_layer(
    tmp_path: Path,
    write_repo: Callable[..., Path],
) -> None:
    task_root = write_repo(tmp_path / "repo")
    task = load_tasks(task_root)[0]
    runner, upstream = await _upstream()
    gateway = Gateway(api_key="host-secret", upstream=upstream, host="0.0.0.0")
    gateway_url = await gateway.start()
    try:
        result = await run_episode(
            task,
            EpisodeProtocol(GatewayLimitedHarness()),
            provider_registry(DockerProvider()),
            run_dir=tmp_path / "runs",
            gateway=gateway,
            gateway_url=gateway_url,
            model="claude-opus-4-8",
            limits=Limits(max_model_calls=1),
        )
    finally:
        await gateway.stop()
        await runner.cleanup()

    assert result.verdict.status is Status.BUDGET_EXCEEDED
    transport = read_jsonl(result.run_dir / "trace.transport.jsonl").records
    refusal = next(record for record in transport if record["disposition"] == "refused")
    assert refusal["refusal_limit"] == "max_model_calls"
    assert result.verdict.failure is not None
    assert "max_model_calls" in result.verdict.failure.message
