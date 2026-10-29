from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ale.core.config import LLMJudgeConfig, RunConfig, VerificationConfig
from ale.core.harness import EffectiveAgentResources
from ale.core.lock import TaskSource
from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.gateway.server import REQUEST_PATHS, Gateway
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers.docker import DockerProvider
from ale.run.secrets import provider_endpoint
from ale.run.tasksets.manifest import load_tasks
from tests.support import provider_registry, sibling_checkout

from .trajectory import MODELS

pytestmark = [
    pytest.mark.needs_docker,
    pytest.mark.needs_llm,
]

TASKS = sibling_checkout("ale-tasks-base")

CASES = (
    ("openai", "openai-responses"),
    ("openai", "openai-chat-completions"),
    ("anthropic", "anthropic"),
)


@pytest.mark.parametrize(
    ("provider", "dialect"),
    CASES,
    ids=["responses", "chat-completions", "messages"],
)
@pytest.mark.asyncio
async def test_live_llm_judge_record_and_transport(
    tmp_path: Path,
    provider: str,
    dialect: str,
) -> None:
    key_env, base_url = provider_endpoint(provider)
    if not os.environ.get(key_env):
        pytest.skip(f"no {key_env}")
    task_path = TASKS / "tasks" / "demo" / "verification_llm_judge"
    task = load_tasks(task_path)[0]
    verification = VerificationConfig(
        llm=LLMJudgeConfig(
            provider=provider,  # type: ignore[arg-type]
            dialect=dialect,  # type: ignore[arg-type]
            model=MODELS[provider],
            reasoning_effort="medium",
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
    assert (result.run_dir / "result.json").is_file()
    assert (result.run_dir / "verification.json").is_file()
    json.loads((result.run_dir / "verification.json").read_text())
    invocation = next(
        json.loads(line)["invocation"]
        for line in (result.run_dir / "llm-judge.jsonl").read_text().splitlines()
        if json.loads(line)["event"] == "completed"
    )
    assert invocation["kind"] == "llm"
    assert invocation["status"] == "completed"
    transport = (result.run_dir / "trace.transport.jsonl").read_text()
    assert invocation["id"] not in transport
    assert result.lock is not None
    assert len(result.lock.judges) == 1
    assert result.lock.judges[0].kind == "llm"
    assert result.lock.judges[0].endpoint_identity == base_url + REQUEST_PATHS[dialect]
    secret = os.environ[key_env]
    for path in result.run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
