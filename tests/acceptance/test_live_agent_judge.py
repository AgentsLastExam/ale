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
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import ManifestTaskset
from ale_verify import _agents

pytestmark = [pytest.mark.needs_docker, pytest.mark.needs_llm]

TASKS = Path(__file__).resolve().parents[3] / "ale-tasks-base"
IMAGE = os.environ.get("ALE_LIVE_AGENT_JUDGE_IMAGE")

CASES = (
    (
        "codex-cli",
        "OPENAI_API_KEY",
        "https://api.openai.com",
        "ALE_LIVE_OPENAI_MODEL",
        "gpt-5.4",
    ),
    (
        "claude-code",
        "ANTHROPIC_API_KEY",
        "https://api.anthropic.com",
        "ALE_LIVE_ANTHROPIC_MODEL",
        "claude-sonnet-5",
    ),
)


@pytest.mark.parametrize(
    ("adapter", "key_env", "base_url", "model_env", "default_model"),
    CASES,
)
@pytest.mark.asyncio
async def test_live_agent_judge_records_transcript_without_an_extra_trajectory(
    tmp_path: Path,
    adapter: str,
    key_env: str,
    base_url: str,
    model_env: str,
    default_model: str,
) -> None:
    if not os.environ.get(key_env):
        pytest.skip(f"no {key_env}")
    if not IMAGE:
        pytest.skip("ALE_LIVE_AGENT_JUDGE_IMAGE is not set")
    repo = tmp_path / "repo"
    task_path = repo / "tasks" / "demo" / "verification_agent_judge"
    task_path.parent.mkdir(parents=True)
    shutil.copytree(TASKS / "tasks" / "demo" / "verification_agent_judge", task_path)
    shutil.copy2(TASKS / "domain.yaml", repo / "domain.yaml")
    manifest = task_path / "task.yaml"
    manifest.write_text(manifest.read_text().replace("image: sandbox-base-cli", f"image: {IMAGE}"))
    task = next(iter(ManifestTaskset(task_path).load()))
    verification = VerificationConfig(
        agent=AgentJudgeConfig(
            adapter=adapter,  # type: ignore[arg-type]
            model=os.environ.get(model_env, default_model),
            reasoning_effort="high",
            base_url=base_url,
            api_key_env=key_env,
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
    assert result.verdict.rewards == {"functional": 1.0, "overall": 1.0}
    record = json.loads((result.run_dir / "verification.json").read_text())
    assert "agent_trajectory" not in record
    assert record["judge_invocations"][0]["adapter"] == adapter
    agent_log = result.run_dir / "logs" / "agent-judge.jsonl"
    assert agent_log.is_file()
    transcript = json.loads(agent_log.read_text())["stdout"]
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
