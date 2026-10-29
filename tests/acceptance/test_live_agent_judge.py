from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from ale.core.config import AgentJudgeConfig, RunConfig, VerificationConfig
from ale.core.harness import EffectiveAgentResources
from ale.core.lock import TaskSource
from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.gateway.server import Gateway
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers.docker import DockerProvider
from ale.run.secrets import provider_endpoint
from ale.run.tasksets.manifest import load_tasks
from ale_verify import _agents
from tests.support import provider_registry, sibling_checkout

from .trajectory import MODELS

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_llm]

TASKS = sibling_checkout("ale-tasks-base")
CASES = (
    ("codex-cli", "ALE_LIVE_CODEX_CLI_VERSION", "openai"),
    ("claude-code", "ALE_LIVE_CLAUDE_CODE_VERSION", "anthropic"),
)


@pytest.mark.parametrize(
    ("adapter", "version_env", "provider"),
    CASES,
    ids=[case[0] for case in CASES],
)
@pytest.mark.asyncio
async def test_live_agent_judge_records_transcript_without_an_extra_trajectory(
    tmp_path: Path,
    adapter: str,
    version_env: str,
    provider: str,
) -> None:
    key_env, _ = provider_endpoint(provider)
    if not os.environ.get(key_env):
        pytest.skip(f"no {key_env}")
    version = os.environ.get(version_env)
    if not version:
        pytest.skip(f"no exact Agent Judge CLI version in {version_env}")
    repo = tmp_path / "repo"
    task_path = repo / "tasks" / "demo" / "verification_agent_judge"
    task_path.parent.mkdir(parents=True)
    shutil.copytree(TASKS / "tasks" / "demo" / "verification_agent_judge", task_path)
    task = load_tasks(task_path)[0]
    verification = VerificationConfig(
        agent=AgentJudgeConfig(
            provider=provider,  # type: ignore[arg-type]
            adapter=adapter,  # type: ignore[arg-type]
            version=version,
            model=MODELS[provider],
            reasoning_effort="high",
        )
    )
    settings = RunConfig(verification=verification)
    harness = OracleHarness()
    # A verifier reaches its Judge only through a Gateway session, which holds the key.
    gateway = Gateway()
    gateway_url = await gateway.start()
    try:
        result = await run_episode(
            task,
            EpisodeProtocol(harness),
            provider_registry(DockerProvider()),
            gateway=gateway,
            gateway_url=gateway_url,
            run_dir=tmp_path,
            provenance=ProvenanceInputs(
                source=TaskSource(kind="local", path=str(task_path)),
                agent=agent_provenance(harness, "", settings, EffectiveAgentResources()),
                gateway=gateway_provenance(settings),
                config_hash=settings.config_hash,
            ),
            verification_config=verification,
        )
    finally:
        await gateway.stop()
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"overall": 1.0}
    record = json.loads((result.run_dir / "verification.json").read_text())
    assert "agent_trajectory" not in record
    assert (
        next(
            json.loads(line)["invocation"]["adapter"]
            for line in (result.run_dir / "agent-judge.jsonl").read_text().splitlines()
            if json.loads(line)["event"] == "completed"
        )
        == adapter
    )
    agent_log = result.run_dir / "agent-judge.jsonl"
    assert agent_log.is_file()
    transcript = [
        event["stdout"]
        for line in agent_log.read_text().splitlines()
        if (event := json.loads(line))["event"] == "response"
    ][-1]
    _, final = _agents._final_response(adapter, transcript)
    assert final is not None
    assert json.loads(final)["choice"] == "yes"
    assert result.lock is not None
    assert len(result.lock.judges) == 1
    assert result.lock.judges[0].kind == "agent"
    assert result.lock.judges[0].adapter == adapter
    assert result.lock.judges[0].adapter_version
    assert result.lock.judges[0].reasoning_effort == "high"
    assert not (result.run_dir / "logs" / adapter).exists()
    secret = os.environ[key_env]
    for path in result.run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
