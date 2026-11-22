"""A real agent CLI through the real Gateway while the provider fails its calls.

The provider is the scripted model, failing every request for a while as an overloaded or
rate-limiting provider does. A failure shorter than the Gateway's retries never reaches
the CLI, Codex's included, which does not retry a 429 at all. A longer one reaches it, the
CLI gives up after its own retries, and the Gateway's session says how the provider was
failing, which the episode protocol turns into the environment's error.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ale.core.errors import AgentError, ModelProviderError
from ale.core.harness import EffectiveAgentResources, HarnessSession
from ale.core.sandbox import Identity
from ale.core.taskspec import OperatingSystem
from ale.core.trace import read_jsonl
from ale.run.episode import _model_provider_failure
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.harnesses.codex_cli import CodexCliHarness
from ale.run.protocol import episode_protocol
from ale.run.recording import EpisodeRecording
from tests.integration.scripted_model import ScriptedModel
from tests.support import cli_os, cli_sandbox

pytestmark = [
    pytest.mark.integration,
    pytest.mark.needs_cli_install,
    pytest.mark.needs_orchard if cli_os() is OperatingSystem.MACOS else pytest.mark.needs_docker,
]

#: Each CLI, its dialect, its shell tool, and how its provider says it is busy.
CASES: dict[str, tuple[Any, str, str, tuple[int, dict[str, Any]], tuple[int, dict[str, Any]]]] = {
    "codex-cli": (
        CodexCliHarness,
        "openai-responses",
        r"^(shell|exec_command)$",
        (429, {"error": {"type": "rate_limit_exceeded", "code": "rate_limit_exceeded"}}),
        (503, {"error": {"type": "server_error", "code": "server_is_overloaded"}}),
    ),
    "claude-code": (
        ClaudeCodeHarness,
        "anthropic",
        r"^Bash$",
        (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}),
        (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}),
    ),
}


async def _run(
    tmp_path: Path, name: str, model: ScriptedModel, *, outage_retry_sec: float
) -> tuple[Exception | None, GatewaySession, list[dict[str, Any]], bool, list[int]]:
    """Launch the CLI once on a scripted task; return what it raised, the Gateway session,
    the calls the trace recorded, whether the task's file was written, and the status of
    every answer the CLI itself received."""
    factory, dialect, _, _, _ = CASES[name]
    harness = factory()
    recording = EpisodeRecording(tmp_path)
    gateway = Gateway(
        api_key="host-only",
        upstream=await model.start(),
        dialect=dialect,
        host="0.0.0.0",
        outage_retry_sec=outage_retry_sec,
    )
    await gateway.start()
    received: list[int] = []
    call = gateway._call

    async def answered(*args: Any, **options: Any) -> Any:
        response = await call(*args, **options)
        received.append(response.status)
        return response

    gateway._call = answered  # type: ignore[method-assign]
    live = gateway.open_session(
        GatewaySession(episode_id=f"failing-{name}", model="scripted-model"), recording.transport
    )
    raised: Exception | None = None
    try:
        async with cli_sandbox(f"failing-{name}", gateway.base_url, tmp_path) as (sandbox, home):
            await sandbox.open_egress()
            await harness.install(sandbox)
            session = HarnessSession(
                episode_id=f"failing-{name}",
                gateway_url=sandbox.gateway_url or gateway.base_url,
                token=live.token,
                model="scripted-model",
                sandbox_id=sandbox.sandbox_id,
                home=home,
                os=cli_os(),
            )
            await harness.install_resources(sandbox, session, EffectiveAgentResources())
            await sandbox.close_egress()
            proof = f"{home}/scripted-proof.txt"
            model.command = f"echo scripted > {proof} && cat {proof}"
            try:
                await harness.launch("Write the proof file.", sandbox, session, timeout_sec=900)
            except AgentError as error:
                raised = error
            written = await sandbox.exec(["cat", proof], identity=Identity.AGENT)
    finally:
        gateway.close_session(live)
        await gateway.stop()
        await model.stop()
    calls = [
        r for r in read_jsonl(tmp_path / "trace.transport.jsonl").records if r["kind"] == "call"
    ]
    return raised, live, calls, written.ok and written.stdout.strip() == "scripted", received


@pytest.mark.parametrize("name", sorted(CASES))
async def test_a_failure_shorter_than_the_gateways_retries_never_reaches_the_cli(
    tmp_path: Path, name: str
) -> None:
    _, _, shell, limited, _ = CASES[name]
    model = ScriptedModel(tool_pattern=shell)
    began: list[float] = []

    def fail() -> tuple[int, dict[str, Any]] | None:
        # Every request fails for the first eight seconds from the CLI's first.
        now = time.monotonic()
        began[:] = began or [now]
        return limited if now < began[0] + 8.0 else None

    model.fail = fail

    raised, live, calls, written, received = await _run(
        tmp_path, name, model, outage_retry_sec=60.0
    )

    assert raised is None
    assert written
    assert model.failed > 0
    # The CLI itself never received a failure: the Gateway rode out every one.
    assert received and all(200 <= status < 300 for status in received), received
    tried = [call for call in calls if call["disposition"] == "failed"]
    assert len(tried) == model.failed
    assert {(call["upstream_status"], call["upstream_error"]) for call in tried} == {
        (limited[0], "rate_limited")
    }
    assert live.failure is None
    assert live.usage.model_calls == len(calls) - len(tried)


@pytest.mark.parametrize("name", sorted(CASES))
async def test_a_longer_failure_stops_the_cli_and_the_gateway_says_why(
    tmp_path: Path, name: str
) -> None:
    _, _, shell, _, busy = CASES[name]
    model = ScriptedModel(tool_pattern=shell)
    failed_at: list[float] = []

    def fail() -> tuple[int, dict[str, Any]]:
        failed_at.append(time.monotonic())
        return busy

    model.fail = fail

    raised, live, calls, written, received = await _run(tmp_path, name, model, outage_retry_sec=5.0)

    assert isinstance(raised, AgentError), raised
    assert not written
    assert live.failure is not None and live.failure.kind == "overloaded"
    assert {(call["disposition"], call["upstream_error"]) for call in calls} == {
        ("failed", "overloaded")
    }
    assert live.usage.model_calls == 0
    # The Gateway rode out its five seconds, sending one call at 0, 1, 3 and 5 s, then gave
    # the failure to the CLI, whose own retries sent more.
    assert failed_at[-1] - failed_at[0] >= 5.0
    assert model.failed > 4 and any(status >= 500 for status in received)
    # What the episode protocol makes of it: the provider stopped the agent.
    ctx: Any = SimpleNamespace(
        limit_refusal=None, model_provider_failure=lambda: _model_provider_failure(live)
    )
    provider = pytest.raises(ModelProviderError, match=r"overloaded \(HTTP \d+\)")
    with provider, episode_protocol._attributed(ctx):
        raise raised
