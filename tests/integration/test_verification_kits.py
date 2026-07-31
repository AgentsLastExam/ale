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
from ale.run.providers.qemu import QemuProvider
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import ManifestTaskset

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

TASKS = Path(__file__).resolve().parents[3] / "ale-tasks-base"
QEMU_IMAGE = Path(
    os.environ.get(
        "ALE_QEMU_IMAGE",
        Path.home() / ".cache/ale/images/ale-ubuntu-desktop.qcow2",
    )
)


def provenance(task_path: Path) -> ProvenanceInputs:
    settings = RunConfig()
    harness = OracleHarness()
    return ProvenanceInputs(
        source=TaskSource(kind="local", path=str(task_path)),
        agent=agent_provenance(
            harness,
            "",
            settings,
            EffectiveAgentResources(),
        ),
        gateway=gateway_provenance(settings),
        config_hash=settings.config_hash,
    )


@pytest.mark.asyncio
async def test_framework_kit_staging_record_and_metrics(tmp_path: Path) -> None:
    task_path = TASKS / "tasks" / "demo" / "verification_deterministic"
    task = next(iter(ManifestTaskset(task_path).load()))

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        DockerProvider(),
        run_dir=tmp_path,
        provenance=provenance(task_path),
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"format": 1.0, "value": 1.0, "overall": 1.0}
    assert result.verdict.metrics == {"checked_files": 1.0}
    record = json.loads((result.run_dir / "verification.json").read_text())
    assert record["status"] == "completed"
    assert result.lock is not None
    assert result.lock.ale_verify is not None
    assert result.lock.ale_verify.version == "0.1.0"
    assert result.lock.kits == ()


@pytest.mark.asyncio
async def test_domain_kit_and_framework_kit_have_separate_provenance(
    tmp_path: Path,
) -> None:
    task_path = TASKS / "tasks" / "demo" / "verification_domain_a"
    task = next(iter(ManifestTaskset(task_path).load()))

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        DockerProvider(),
        run_dir=tmp_path,
        provenance=provenance(task_path),
    )

    assert result.verdict.rewards == {"approved": 1.0, "overall": 1.0}
    assert result.lock is not None
    assert result.lock.ale_verify is not None
    assert [kit.name for kit in result.lock.kits] == ["verification_example"]
    verification = json.loads((result.run_dir / "verification.json").read_text())
    assert verification["criteria"][0]["source"] == "check"


@pytest.mark.asyncio
async def test_partial_record_survives_verifier_failure(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "tasks").mkdir(parents=True)
    (root / "domain.yaml").write_text("name: demo\nrequires_core: '>=0.1,<0.2'\n")
    task_path = scaffold_task(root / "tasks" / "partial")
    (task_path / "verify" / "check.py").write_text(
        "from ale_verify import CheckResult, Verification\n"
        "verification = Verification()\n"
        "verification.check('first', CheckResult(1.0))\n"
        "raise RuntimeError('verifier stopped')\n"
    )
    task = next(iter(ManifestTaskset(task_path).load()))

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        DockerProvider(),
        run_dir=tmp_path / "runs",
    )

    assert result.verdict.status is Status.TASK_ERROR
    record = json.loads((result.run_dir / "verification.json").read_text())
    assert record["status"] == "in_progress"
    assert record["criteria"][0]["name"] == "first"


@pytest.mark.asyncio
async def test_envelope_without_verification_record_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "tasks").mkdir(parents=True)
    (root / "domain.yaml").write_text("name: demo\nrequires_core: '>=0.1,<0.2'\n")
    task_path = scaffold_task(root / "tasks" / "legacy")
    (task_path / "verify" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'printf \'{"rewards":{"legacy":1},"metrics":{"files":1}}\' '
        '> "$ALE_VERDICT_PATH"\n'
    )
    (task_path / "verify" / "run.sh").chmod(0o755)
    task = next(iter(ManifestTaskset(task_path).load()))

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        DockerProvider(),
        run_dir=tmp_path / "runs",
    )

    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.rewards is None


@pytest.mark.asyncio
async def test_missing_judge_credential_is_an_environment_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ALE_MISSING_JUDGE_KEY", raising=False)
    task_path = TASKS / "tasks" / "demo" / "verification_llm_judge"
    task = next(iter(ManifestTaskset(task_path).load()))

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        DockerProvider(),
        run_dir=tmp_path,
        verification_config=VerificationConfig(
            llm=LLMJudgeConfig(
                model="gpt-5-mini",
                reasoning_effort="medium",
                base_url="https://api.openai.com",
                api_key_env="ALE_MISSING_JUDGE_KEY",
            )
        ),
    )

    assert result.verdict.status is Status.ENV_ERROR
    assert result.verdict.rewards is None
    record = json.loads((result.run_dir / "verification.json").read_text())
    assert record["status"] == "failed"


@pytest.mark.asyncio
async def test_task_cannot_silence_judge_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ALE_MISSING_JUDGE_KEY", raising=False)
    root = tmp_path / "repo"
    (root / "tasks").mkdir(parents=True)
    (root / "domain.yaml").write_text("name: demo\nrequires_core: '>=0.1,<0.2'\n")
    task_path = scaffold_task(root / "tasks" / "caught-judge-failure")
    (task_path / "verify" / "check.py").write_text(
        "from ale_verify import CheckResult, Verification\n"
        "verification = Verification()\n"
        "try:\n"
        "    verification.judge(\n"
        "        'llm', 'quality',\n"
        "        prompt='Judge.',\n"
        "        rubric={\n"
        "            'no': {'score': 0.0, 'description': 'Bad.'},\n"
        "            'yes': {'score': 1.0, 'description': 'Good.'},\n"
        "        },\n"
        "    )\n"
        "except Exception:\n"
        "    pass\n"
        "verification.check('fallback', CheckResult(1.0))\n"
        "verification.aggregate('overall')\n"
        "verification.write()\n"
    )
    task = next(iter(ManifestTaskset(task_path).load()))

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        DockerProvider(),
        run_dir=tmp_path / "runs",
        verification_config=VerificationConfig(
            llm=LLMJudgeConfig(
                model="gpt-5-mini",
                reasoning_effort="medium",
                base_url="https://api.openai.com",
                api_key_env="ALE_MISSING_JUDGE_KEY",
            )
        ),
    )

    assert result.verdict.status is Status.ENV_ERROR
    assert result.verdict.rewards is None


@pytest.mark.needs_kvm
@pytest.mark.skipif(not QEMU_IMAGE.is_file(), reason=f"no QEMU image at {QEMU_IMAGE}")
@pytest.mark.asyncio
async def test_framework_verification_has_qemu_parity(tmp_path: Path) -> None:
    task_path = TASKS / "tasks" / "demo" / "verification_deterministic"
    task = next(iter(ManifestTaskset(task_path).load()))

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        QemuProvider(image=QEMU_IMAGE),
        run_dir=tmp_path,
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"format": 1.0, "value": 1.0, "overall": 1.0}
    assert result.verdict.metrics == {"checked_files": 1.0}
