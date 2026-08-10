from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.harness import EffectiveAgentResources
from ale.core.lock import TaskSource
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers.docker import DockerProvider
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from ale_verify import (
    CriterionResult,
    JudgeAttempt,
    JudgeInvocation,
    VerificationRecord,
)
from tests.support import provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"
HASH0 = "sha256:" + "0" * 64
HASH1 = "sha256:" + "1" * 64


def repository(root: Path, name: str) -> Path:
    return scaffold_task(root / "tasks" / name)


def provenance(task_path: Path) -> ProvenanceInputs:
    settings = RunConfig()
    harness = OracleHarness()
    return ProvenanceInputs(
        source=TaskSource(kind="local", path=str(task_path)),
        agent=agent_provenance(harness, "", settings, EffectiveAgentResources()),
        gateway=gateway_provenance(settings),
        config_hash=settings.config_hash,
    )


@pytest.mark.asyncio
async def test_record_and_envelope_mismatch_is_rejected_but_record_is_retained(
    tmp_path: Path,
) -> None:
    task_root = repository(tmp_path / "repo", "mismatch")
    (task_root / "verify" / "verify.py").write_text(
        "import json, os\n"
        "from ale_verify import CheckResult, Verification\n"
        "v = Verification()\n"
        "v.check('score', CheckResult(1.0))\n"
        "v.write()\n"
        "with open(os.environ['ALE_VERDICT_PATH'], 'w') as handle:\n"
        "    json.dump({'rewards': {'score': 0.0}, 'metrics': {}}, handle)\n"
    )
    task = load_tasks(task_root)[0]

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
    )

    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.rewards is None
    record = json.loads((result.run_dir / "verification.json").read_text())
    assert record["criteria"][0]["name"] == "score"
    assert record["criteria"][0]["score"] == 1.0


@pytest.mark.asyncio
async def test_agent_transcript_and_judge_provenance_are_collected_after_exit(
    tmp_path: Path,
) -> None:
    task_root = repository(tmp_path / "repo", "recording")
    invocation = JudgeInvocation(
        id="judge-1",
        kind="agent",
        criterion_name="functional",
        adapter="codex-cli",
        adapter_version="codex 1",
        status="completed",
        attempts=(
            JudgeAttempt(
                index=1,
                mode="initial",
                started_at="2026-07-31T00:00:00Z",
                finished_at="2026-07-31T00:00:01Z",
                outcome="completed",
                model="judge-model",
                reasoning_effort="high",
                endpoint_identity="https://example.test",
                prompt_hash=HASH0,
                rubric_hash=HASH1,
                request_id="native-session",
            ),
        ),
    )
    record = VerificationRecord(
        status="completed",
        criteria=(
            CriterionResult(
                name="functional",
                source="agent_judge",
                score=1.0,
                reasoning="Works.",
                raw_value="yes",
                judge_invocation_id="judge-1",
            ),
        ),
        judge_invocations=(invocation,),
    )
    script = textwrap.dedent(
        f"""
        import json, os
        from pathlib import Path
        record = {record.to_dict()!r}
        Path(os.environ["ALE_VERIFICATION_PATH"]).write_text(json.dumps(record))
        Path(os.environ["ALE_VERDICT_PATH"]).write_text(
            json.dumps({{"rewards": {{"functional": 1.0}}, "metrics": {{}}}})
        )
        Path(os.environ["ALE_AGENT_JUDGE_LOG_PATH"]).write_text(
            '{{"attempt":1,"stdout":"sanitized","stderr":""}}\\n'
        )
        """
    ).strip()
    (task_root / "verify" / "verify.py").write_text(script + "\n")
    task = load_tasks(task_root)[0]

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
        provenance=provenance(task_root),
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert (result.run_dir / "logs" / "agent-judge.jsonl").is_file()
    assert not (result.run_dir / "logs" / "oracle" / "agent-judge.jsonl").exists()
    assert result.lock is not None
    assert result.lock.ale_verify is not None
    assert result.lock.judges[0].adapter == "codex-cli"
    assert result.lock.judges[0].attempts == 1
