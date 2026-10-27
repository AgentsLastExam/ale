from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.config import LLMJudgeConfig, VerificationConfig
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.gateway.server import Gateway
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.recording import Redactor
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from ale_verify._io import EvidenceError, read_text_evidence
from tests.support import provider_registry

pytestmark = pytest.mark.integration


def test_credentials_and_unsafe_evidence_are_rejected_or_redacted(tmp_path: Path) -> None:
    secret = "sk-provider-secret"
    assert secret not in Redactor((secret,))(f"Authorization: Bearer {secret}")
    regular = tmp_path / "answer.txt"
    regular.write_text("answer")
    symlink = tmp_path / "answer-link"
    symlink.symlink_to(regular)
    with pytest.raises(EvidenceError):
        read_text_evidence(symlink)
    oversized = tmp_path / "large.txt"
    oversized.write_bytes(b"x" * 10)
    with pytest.raises(EvidenceError, match="exceeds"):
        read_text_evidence(oversized, max_bytes=5)


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_credential_exists_only_during_verify_and_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "verify-only-provider-secret"
    monkeypatch.setenv("ALE_OPENAI_API_KEY", secret)
    task_root = scaffold_task(tmp_path / "scope")
    (task_root / "setup").mkdir()
    (task_root / "instruction.md").write_text("${greeting}. Do nothing.\n")
    (task_root / "setup" / "run.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "mkdir -p /home/user/output\n"
        'test -z "${ALE_OPENAI_API_KEY-}${ALE_LLM_JUDGE_TOKEN-}"\n'
        "printf absent > /home/user/output/setup-key\n"
    )
    (task_root / "oracle" / "run.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    (task_root / "verify" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nexec python3 verify.py\n"
    )
    (task_root / "verify" / "verify.py").write_text(
        "import os, sys\n"
        "from ale_verify import checks, write\n"
        "v = {}\n"
        "v['setup_scope'] = checks.text_equals('/home/user/output/setup-key', 'absent')['score']\n"
        "v['verify_scope'] = float(bool(os.environ.get('ALE_LLM_JUDGE_TOKE"
        "N')) and 'ALE_OPENAI_API_KEY' not in os.environ)\n"
        "v['no_host_callback'] = float(not any((name in os.environ for nam"
        "e in ('ALE_VERIFICATION_URL', 'ALE_VERIFICATION_TOKEN'))))\n"
        "sys.stderr.write(os.environ['ALE_LLM_JUDGE_TOKEN'])\n"
        "v['overall'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    (task_root / "setup" / "run.sh").chmod(0o755)

    task = load_tasks(task_root)[0]
    gateway = Gateway()
    gateway_url = await gateway.start()
    try:
        result = await run_episode(
            task,
            StandardEnvironment(OracleHarness()),
            provider_registry(DockerProvider()),
            run_dir=tmp_path / "runs",
            gateway=gateway,
            gateway_url=gateway_url,
            verification_config=VerificationConfig(
                llm=LLMJudgeConfig(
                    provider="openai",
                    dialect="openai-responses",
                    model="gpt-5-mini",
                    reasoning_effort="medium",
                )
            ),
        )
    finally:
        await gateway.stop()

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"overall": 1.0}
    for path in result.run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_record_containing_a_verify_credential_is_not_collected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "record-leak-provider-secret"
    monkeypatch.setenv("ALE_OPENAI_API_KEY", secret)
    task_root = scaffold_task(tmp_path / "leak")
    (task_root / "setup").mkdir()
    (task_root / "instruction.md").write_text("${greeting}. Do nothing.\n")
    (task_root / "setup" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /home/user/output\n"
    )
    (task_root / "oracle" / "run.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    (task_root / "verify" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nexec python3 verify.py\n"
    )
    (task_root / "verify" / "verify.py").write_text(
        "import os\n"
        "from ale_verify import write\n"
        "v = {}\n"
        "v['score'] = {'score': 1.0, 'raw_value': os.environ['ALE_OPENAI_API_KEY']}['score']\n"
        "v['overall'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    (task_root / "setup" / "run.sh").chmod(0o755)
    task = load_tasks(task_root)[0]

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
        verification_config=VerificationConfig(
            llm=LLMJudgeConfig(
                provider="openai",
                dialect="openai-responses",
                model="gpt-5-mini",
                reasoning_effort="medium",
            )
        ),
    )

    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.rewards is None
    assert not (result.run_dir / "verification.json").exists()
    for path in result.run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
