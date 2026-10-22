from __future__ import annotations

import json
import textwrap
from dataclasses import asdict
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
    JudgeAttempt,
    JudgeInvocation,
)
from tests.support import CONTAINER_BASE, provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

IMAGE = CONTAINER_BASE
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
@pytest.mark.parametrize("mode", ["direct", "crash", "malformed", "stale"])
@pytest.mark.parametrize("environment_mode", ["shared", "separate"])
async def test_plain_result_is_accepted_only_after_normal_exit(
    tmp_path: Path, mode: str, environment_mode: str
) -> None:
    task_root = repository(tmp_path / "repo", "plain-result")
    if environment_mode == "separate":
        manifest = task_root / "task.yaml"
        manifest.write_text(
            manifest.read_text()
            + "verify:\n  environment_mode: separate\n"
            + "  resources: {cpus: 1, memory_mb: 512, storage_mb: null, gpus: 0}\n"
        )
    script = (
        "import json, os\nfrom pathlib import Path\n"
        "Path(os.environ['ALE_VERDICT_PATH']).write_text(json.dumps({"
        "'overall':0.999,'details':{'count':10,'raw':0.999}}))\n"
    )
    if mode == "crash":
        script += "raise RuntimeError('after writing result')\n"
    elif mode == "malformed":
        script += "Path(os.environ['ALE_VERDICT_PATH']).write_text('partial JSON')\n"
    elif mode == "stale":
        (task_root / "verify/result.json").write_text('{"overall":1}')
        script = "pass\n"
    (task_root / "verify/verify.py").write_text(script)
    result = await run_episode(
        load_tasks(task_root)[0],
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
    )
    if mode == "direct":
        assert result.verdict.status is Status.COMPLETED, result.verdict.failure
        assert result.verdict.rewards == {"overall": 1.0}
        assert json.loads((result.run_dir / "verification.json").read_text()) == {
            "overall": 1.0,
            "details": {"count": 10, "raw": 0.999},
        }
    else:
        assert result.verdict.status is Status.TASK_ERROR
        assert result.verdict.rewards is None


@pytest.mark.asyncio
async def test_agent_transcript_and_judge_provenance_are_collected_after_exit(
    tmp_path: Path,
) -> None:
    task_root = repository(tmp_path / "repo", "recording")
    invocation = JudgeInvocation(
        id="judge-1",
        kind="agent",
        name="functional",
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
                schema_hash=HASH1,
                request_id="native-session",
            ),
        ),
    )
    script = textwrap.dedent(
        f"""
        import json, os
        from pathlib import Path
        record = {{"event":"completed", "invocation":{asdict(invocation)!r}}}
        transcript = {{"event":"response", "attempt":1,"stdout":"sanitized","stderr":""}}
        Path(os.environ["ALE_AGENT_JUDGE_LOG_PATH"]).write_text(
            json.dumps(transcript)+"\\n"+json.dumps(record)+"\\n"
        )
        Path(os.environ["ALE_VERDICT_PATH"]).write_text(
            json.dumps({{"overall":1.0, "details":{{"functional":1.0}}}})
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
    assert (result.run_dir / "agent-judge.jsonl").is_file()
    assert not (result.run_dir / "llm-judge.jsonl").exists()
    assert not list((result.run_dir / "logs").rglob("*judge.jsonl"))
    assert result.lock is not None
    assert result.lock.ale_verify is not None
    assert result.lock.judges[0].adapter == "codex-cli"
    assert result.lock.judges[0].attempts == 1
