from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from ale.core.config import LLMJudgeConfig, VerificationConfig
from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.gateway.server import Gateway
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers.docker import DockerProvider
from ale.run.recording import Redactor
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from ale_verify._io import EvidenceError, read_text_evidence
from tests.support import edit_task_config, provider_registry

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
    (task_root / "environment" / "setup").mkdir()
    (task_root / "instruction.md").write_text("Do nothing.\n")
    (task_root / "environment" / "setup" / "setup.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "mkdir -p /home/user/output\n"
        'test -z "${ALE_OPENAI_API_KEY-}${ALE_LLM_JUDGE_TOKEN-}"\n'
        "printf absent > /home/user/output/setup-key\n"
    )
    (task_root / "solution" / "solve.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    (task_root / "tests" / "test.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nexec python3 verify.py\n"
    )
    (task_root / "tests" / "verify.py").write_text(
        "import os, sys\n"
        "from ale_verify import checks, write\n"
        "v = {}\n"
        "v['setup_scope'] = checks.text_equals('/home/user/output/setup-key', 'absent')['score']\n"
        "v['verify_scope'] = float(bool(os.environ.get('ALE_LLM_JUDGE_TOKE"
        "N')) and 'ALE_OPENAI_API_KEY' not in os.environ)\n"
        "v['no_host_callback'] = float(not any((name in os.environ for nam"
        "e in ('ALE_VERIFICATION_URL', 'ALE_VERIFICATION_TOKEN'))))\n"
        "sys.stderr.write(os.environ['ALE_LLM_JUDGE_TOKEN'])\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    (task_root / "environment" / "setup" / "setup.sh").chmod(0o755)

    task = load_tasks(task_root)[0]
    gateway = Gateway()
    gateway_url = await gateway.start()
    try:
        result = await run_episode(
            task,
            EpisodeProtocol(OracleHarness()),
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
    assert result.verdict.rewards == {"reward": 1.0}
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
    (task_root / "environment" / "setup").mkdir()
    (task_root / "instruction.md").write_text("Do nothing.\n")
    (task_root / "environment" / "setup" / "setup.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /home/user/output\n"
    )
    (task_root / "solution" / "solve.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    (task_root / "tests" / "test.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nexec python3 verify.py\n"
    )
    (task_root / "tests" / "verify.py").write_text(
        "import os\n"
        "from ale_verify import write\n"
        "v = {}\n"
        "v['score'] = {'score': 1.0, 'raw_value': os.environ['ALE_OPENAI_API_KEY']}['score']\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    (task_root / "environment" / "setup" / "setup.sh").chmod(0o755)
    task = load_tasks(task_root)[0]

    result = await run_episode(
        task,
        EpisodeProtocol(OracleHarness()),
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


#: What an agent with sudo leaves at the verifier directory before verification.
PLANTED = {
    "files": """
        sudo mkdir -p /logs/verifier
        echo '{"reward": 1}' | sudo tee /logs/verifier/reward.json /logs/verifier/verification.json
        sudo chmod -R a+rwx /logs
        """,
    "parent link": """
        mkdir -p /home/user/trap/verifier
        echo '{"reward": 1}' > /home/user/trap/verifier/reward.json
        sudo ln -s /home/user/trap /logs
        """,
    "directory link": """
        sudo mkdir /logs
        sudo chown user /logs
        mkdir -p /home/user/trap
        echo '{"reward": 1}' > /home/user/trap/reward.json
        ln -s /home/user/trap /logs/verifier
        """,
}

#: Records what verification finds at the verifier directory and scores zero.
OBSERVING_VERIFIER = """
import os, stat
from pathlib import Path
from ale_verify import write
parent, verifier = os.lstat('/logs'), os.lstat('/logs/verifier')
trap = Path('/home/user/trap')
write({
    'reward': 0,
    'parent': stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0
    and not parent.st_mode & 0o022,
    'verifier': stat.S_ISDIR(verifier.st_mode) and verifier.st_uid == 0
    and stat.S_IMODE(verifier.st_mode) == 0o700,
    'empty': os.listdir('/logs/verifier') == [],
    'trap': {str(p.relative_to(trap)): p.read_text() for p in trap.rglob('*') if p.is_file()},
})
"""


def _planting_task(root: Path, plant: str, verifier: str) -> Path:
    task_root = scaffold_task(root)
    edit_task_config(task_root, {"ale": {"sudo": True}})
    (task_root / "solution" / "solve.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n" + textwrap.dedent(PLANTED[plant])
    )
    (task_root / "tests" / "verify.py").write_text(verifier)
    return task_root


@pytest.mark.needs_docker
@pytest.mark.asyncio
@pytest.mark.parametrize("plant", sorted(PLANTED))
async def test_an_agent_cannot_plant_or_redirect_a_result(tmp_path: Path, plant: str) -> None:
    """Verification finds the verifier directory empty and the framework's, links removed."""
    task = load_tasks(_planting_task(tmp_path / "planted", plant, OBSERVING_VERIFIER))[0]
    result = await run_episode(
        task,
        EpisodeProtocol(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 0.0}
    record = json.loads((result.run_dir / "verification.json").read_text())
    assert (record["parent"], record["verifier"], record["empty"]) == (True, True, True)
    # The framework neither wrote through a link nor removed what it pointed at.
    planted = {"parent link": "verifier/reward.json", "directory link": "reward.json"}
    expected = {planted[plant]: '{"reward": 1}\n'} if plant in planted else {}
    assert record["trap"] == expected


@pytest.mark.needs_docker
@pytest.mark.asyncio
async def test_a_planted_result_is_not_a_result(tmp_path: Path) -> None:
    task = load_tasks(_planting_task(tmp_path / "silent", "files", "pass\n"))[0]
    result = await run_episode(
        task,
        EpisodeProtocol(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
    )

    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.failure is not None
    assert result.verdict.failure.error_class == "VerifierOutputError"
    assert not (result.run_dir / "verification.json").exists()
