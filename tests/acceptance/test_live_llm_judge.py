from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ale.core.config import LLMJudgeConfig, RunConfig, VerificationConfig
from ale.core.harness import EffectiveAgentResources
from ale.core.lock import TaskSource
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import ManifestTaskset

pytestmark = [
    pytest.mark.needs_docker,
    pytest.mark.needs_llm,
    pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="no OPENAI_API_KEY"),
]

TASKS = Path(__file__).resolve().parents[3] / "ale-tasks-base"


@pytest.mark.asyncio
async def test_live_llm_judge_record_and_transport(tmp_path: Path) -> None:
    task_path = TASKS / "tasks" / "demo" / "verification_llm_judge"
    task = next(iter(ManifestTaskset(task_path).load()))
    verification = VerificationConfig(
        llm=LLMJudgeConfig(
            model=os.environ.get("ALE_LIVE_OPENAI_MODEL", "gpt-5.4-mini"),
            reasoning_effort="medium",
            base_url="https://api.openai.com",
            api_key_env="OPENAI_API_KEY",
        )
    )
    settings = RunConfig(verification=verification)
    harness = OracleHarness()
    result = await run_episode(
        task,
        StandardEnvironment(harness),
        DockerProvider(),
        run_dir=tmp_path,
        provenance=ProvenanceInputs(
            source=TaskSource(kind="local", path=str(task_path)),
            agent=agent_provenance(harness, "", settings, EffectiveAgentResources()),
            gateway=gateway_provenance(settings),
            config_hash=settings.config_hash,
        ),
        verification_config=verification,
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"correctness": 1.0, "overall": 1.0}
    assert (result.run_dir / "result.json").is_file()
    assert (result.run_dir / "verification.json").is_file()
    record = json.loads((result.run_dir / "verification.json").read_text())
    invocation = record["judge_invocations"][0]
    assert invocation["kind"] == "llm"
    assert invocation["status"] == "completed"
    transport = (result.run_dir / "trace.transport.jsonl").read_text()
    assert invocation["id"] not in transport
    assert result.lock is not None
    assert len(result.lock.judges) == 1
    assert result.lock.judges[0].kind == "llm"
    assert result.lock.judges[0].endpoint_identity == "https://api.openai.com"
    secret = os.environ["OPENAI_API_KEY"]
    for path in result.run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
