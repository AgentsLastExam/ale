from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.config import LLMJudgeConfig, VerificationConfig
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
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
    monkeypatch.setenv("JUDGE_KEY", secret)
    task_root = scaffold_task(tmp_path / "scope")
    (task_root / "setup").mkdir()
    (task_root / "instruction.md").write_text("${greeting}. Do nothing.\n")
    (task_root / "setup" / "run.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "mkdir -p /home/user/output\n"
        'test -z "${JUDGE_KEY-}"\n'
        "printf absent > /home/user/output/setup-key\n"
    )
    (task_root / "oracle" / "run.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    (task_root / "verify" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nexec python3 verify.py\n"
    )
    (task_root / "verify" / "verify.py").write_text(
        "import os, sys\n"
        "from ale_verify import CheckResult, Verification, checks\n"
        "v = Verification()\n"
        "v.check('setup_scope', checks.text_equals('/home/user/output/setup-key', 'absent'))\n"
        "v.check('verify_scope', CheckResult(float(bool(os.environ.get('JUDGE_KEY')))))\n"
        "v.check('no_host_callback', CheckResult(float(not any(name in os.environ for name in "
        "('ALE_VERIFICATION_URL', 'ALE_VERIFICATION_TOKEN')))))\n"
        "sys.stderr.write(os.environ['JUDGE_KEY'])\n"
        "v.aggregate('overall')\n"
        "v.write()\n"
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
                model="gpt-5-mini",
                reasoning_effort="medium",
                base_url="https://api.openai.com",
                api_key_env="JUDGE_KEY",
            )
        ),
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {
        "setup_scope": 1.0,
        "verify_scope": 1.0,
        "no_host_callback": 1.0,
        "overall": 1.0,
    }
    for path in result.run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_record_containing_a_verify_credential_is_not_collected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "record-leak-provider-secret"
    monkeypatch.setenv("JUDGE_KEY", secret)
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
        "from ale_verify import CheckResult, Verification\n"
        "v = Verification()\n"
        "v.check('score', CheckResult(1.0, raw_value=os.environ['JUDGE_KEY']))\n"
        "v.write()\n"
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
                model="gpt-5-mini",
                reasoning_effort="medium",
                base_url="https://api.openai.com",
                api_key_env="JUDGE_KEY",
            )
        ),
    )

    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.rewards is None
    assert not (result.run_dir / "verification.json").exists()
    for path in result.run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
