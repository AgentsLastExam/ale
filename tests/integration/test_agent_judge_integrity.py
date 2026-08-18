from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale.core.config import AgentJudgeConfig, VerificationConfig
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks
from ale_verify import CheckResult, JudgeAttempt, JudgeInvocation, Verification
from tests.support import provider_registry

pytestmark = pytest.mark.integration


def test_agent_judge_freezes_later_checks_and_does_not_touch_solver_trajectory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = tmp_path / "verification.json"
    verdict = tmp_path / "rewards.json"
    config = tmp_path / "config.json"
    trajectory = tmp_path / "trajectory.json"
    trajectory.write_text('{"schema_version":"ATIF-v1.7","steps":[]}')
    before = trajectory.read_bytes()
    config.write_text(
        '{"agent":{"adapter":"codex-cli","version":"1.0.0","model":"m",'
        '"reasoning_effort":"high",'
        '"base_url":"https://example.test","api_key_env":"KEY"}}'
    )
    for key, value in {
        "ALE_VERIFICATION_PATH": record,
        "ALE_VERDICT_PATH": verdict,
        "ALE_VERIFY_CONFIG_PATH": config,
        "ALE_TRAJECTORY_PATH": trajectory,
        "KEY": "secret",
    }.items():
        monkeypatch.setenv(key, str(value))

    def fake(**kwargs):  # type: ignore[no-untyped-def]
        return (
            "yes",
            "Works.",
            JudgeInvocation(
                id=kwargs["invocation_id"],
                kind="agent",
                criterion_name=kwargs["name"],
                adapter="codex-cli",
                adapter_version="1",
                status="completed",
                attempts=(
                    JudgeAttempt(
                        index=1,
                        mode="initial",
                        started_at="2026-07-31T00:00:00Z",
                        finished_at="2026-07-31T00:00:01Z",
                        outcome="completed",
                        model="m",
                        reasoning_effort="high",
                        endpoint_identity="https://example.test",
                        prompt_hash="sha256:" + "0" * 64,
                        rubric_hash="sha256:" + "1" * 64,
                    ),
                ),
            ),
        )

    monkeypatch.setattr("ale_verify._agents.run", fake)
    verification = Verification()
    verification.judge(
        "agent",
        "functional",
        prompt="Test.",
        rubric={
            "no": {"score": 0, "description": "Fails."},
            "yes": {"score": 1, "description": "Works."},
        },
    )
    with pytest.raises(RuntimeError, match="after an Agent Judge"):
        verification.check("late", CheckResult(1))
    verification.write()
    assert trajectory.read_bytes() == before


@pytest.mark.needs_docker
@pytest.mark.parametrize(
    ("adapter", "binary_name", "model", "base_url"),
    (
        ("codex-cli", "codex", "gpt-5", "https://example.test"),
        ("claude-code", "claude", "claude-sonnet-4", "https://example.test"),
    ),
)
@pytest.mark.asyncio
async def test_root_agent_judge_repairs_in_place_without_republishing_mutations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    adapter: str,
    binary_name: str,
    model: str,
    base_url: str,
) -> None:
    secret = "agent-direct-secret"
    monkeypatch.setenv("JUDGE_KEY", secret)
    task_root = scaffold_task(tmp_path / "agent-integrity")
    (task_root / "task.yaml").write_text(
        (task_root / "task.yaml")
        .read_text()
        .replace("memory_mb: 1024", "memory_mb: 512")
        .replace("verify: 120", "verify: 120")
    )
    (task_root / "instruction.md").write_text(
        "${greeting}. Write baseline to /home/user/output/result.txt\n"
    )
    (task_root / "image" / "fake-agent.py").write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print(Path(sys.argv[0]).name + ' fake 1.0.0')\n"
        "    raise SystemExit\n"
        "sys.stdin.read()\n"
        "name = Path(sys.argv[0]).name\n"
        "resumed = 'resume' in sys.argv or '--resume' in sys.argv\n"
        "if resumed:\n"
        "    verdict = {'choice':'yes','reasoning':'Observed baseline.'}\n"
        "else:\n"
        "    Path('/home/user/output/post-judge.txt').write_text('mutation')\n"
        "    verdict = {'choice':'invalid','reasoning':'repair me'}\n"
        "if name == 'codex':\n"
        "    session = 'thread-1'\n"
        "    print(json.dumps({'type':'thread.started','thread_id':session}))\n"
        "    print(json.dumps({'type':'item.completed','item':"
        "{'type':'agent_message','text':json.dumps(verdict)}}))\n"
        "else:\n"
        "    selector = '--resume' if resumed else '--session-id'\n"
        "    session = sys.argv[sys.argv.index(selector) + 1]\n"
        "    print(json.dumps({'type':'result','session_id':session,"
        "'result':json.dumps(verdict)}))\n"
    )
    (task_root / "image" / "Dockerfile").write_text(
        "FROM ghcr.io/agentslastexam/container-ubuntu22-base:latest\n"
        f"COPY fake-agent.py /usr/local/bin/{binary_name}\n"
        f"RUN chmod 755 /usr/local/bin/{binary_name} "
        "&& mkdir -p /home/user/output && chown -R user:user /home/user/output\n"
    )
    (task_root / "oracle" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf baseline > /home/user/output/result.txt\n"
    )
    (task_root / "verify" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nexec python3 verify.py\n"
    )
    (task_root / "verify" / "verify.py").write_text(
        "from ale_verify import Verification\n"
        "v = Verification()\n"
        "v.judge(\n"
        "    'agent', 'functional', prompt='Inspect result.txt.',\n"
        "    rubric={\n"
        "        'no': {'score': 0.0, 'description': 'Missing.'},\n"
        "        'yes': {'score': 1.0, 'description': 'Contains baseline.'},\n"
        "    },\n"
        ")\n"
        "v.aggregate('overall')\n"
        "v.write()\n"
    )
    task = load_tasks(task_root)[0]

    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
        verification_config=VerificationConfig(
            agent=AgentJudgeConfig(
                adapter=adapter,  # type: ignore[arg-type]
                version="1.0.0",
                model=model,
                reasoning_effort="high",
                base_url=base_url,
                api_key_env="JUDGE_KEY",
            )
        ),
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    record = json.loads((result.run_dir / "verification.json").read_text())
    invocation = record["judge_invocations"][0]
    assert [attempt["outcome"] for attempt in invocation["attempts"]] == [
        "invalid",
        "completed",
    ]
    assert invocation["adapter"] == adapter
    assert invocation["attempts"][0]["request_id"]
    assert invocation["attempts"][1]["request_id"] == invocation["attempts"][0]["request_id"]
    assert (result.run_dir / "logs" / "agent-judge.jsonl").is_file()
    assert not (result.run_dir / "artifacts" / "output" / "post-judge.txt").exists()
    trajectory = json.loads((result.run_dir / "trajectory.json").read_text())
    assert trajectory["agent"]["name"] == "oracle"
    assert "agent_trajectory" not in record
    for path in result.run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
