"""Subscription credentials stay host-side while the agent runs in a real sandbox."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.config import AgentConfig, RunConfig
from ale.core.harness import AgentRun, AutonomousHarness, EffectiveAgentResources, HarnessSession
from ale.core.lock import TaskSource
from ale.core.sandbox import Identity, Sandbox
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.gateway.session import SessionRegistry
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers.docker import DockerProvider
from ale.run.subscription import resolve_authentication
from ale.run.tasksets import load_tasks
from tests.support import provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


class SubscriptionProbeHarness(AutonomousHarness):
    name = "subscription-probe"

    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code

    def version(self) -> str:
        return "1"

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        script = f"""
import json
from pathlib import Path

home = Path(__import__('os').environ['ALE_HOME'])
for name in ('OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'XAI_API_KEY'):
    assert name not in __import__('os').environ
auth = home / '.codex-ale/auth.json'
assert not auth.exists()
(home / 'output/result.txt').write_text('hello world')
raise SystemExit({self.exit_code})
"""
        result = await sandbox.exec(
            ["python3", "-c", script],
            env={"ALE_HOME": session.home},
            identity=Identity.AGENT,
            timeout_sec=timeout_sec,
        )
        return AgentRun(exit_code=result.exit_code, final_message=result.stderr or None)


@pytest.mark.parametrize(
    ("exit_code", "expected_status"),
    [(0, Status.COMPLETED), (17, Status.AGENT_ERROR)],
)
@pytest.mark.asyncio
async def test_subscription_profile_stays_host_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_repo: Callable[..., Path],
    exit_code: int,
    expected_status: Status,
) -> None:
    environment = tmp_path / ".env"
    environment.write_text("")
    monkeypatch.setenv("ALE_ENV_FILE", str(environment))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ordinary = tmp_path / "home" / ".codex" / "auth.json"
    ordinary.parent.mkdir(parents=True)
    ordinary.write_text('{"ordinary": true}')
    profile = tmp_path / ".ale" / "auth" / "codex-cli" / "auth.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "old"}}))
    profile.chmod(0o600)
    authentication = resolve_authentication(
        AgentConfig(name="codex-cli", authentication="subscription")
    )
    task = load_tasks(write_repo(tmp_path / "repo"))[0]
    harness = SubscriptionProbeHarness(exit_code)
    settings = RunConfig(
        agent=AgentConfig(
            name="subscription-probe",
            model="test-model",
            authentication="subscription",
        )
    )
    resources = EffectiveAgentResources()
    provenance = ProvenanceInputs(
        source=TaskSource(kind="local", path=str(task.folder.root)),
        agent=agent_provenance(
            harness,
            settings.agent.model,
            settings,
            resources,
            authentication=authentication.provenance(source="cli", cli_version="1"),
        ),
        gateway=gateway_provenance(settings),
        config_hash=settings.config_hash,
    )

    result = await run_episode(
        task,
        StandardEnvironment(harness),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
        model=settings.agent.model,
        provenance=provenance,
        authentication="subscription",
        profile_slot_id=authentication.profile_slot_id,
        proxy_url="http://0.0.0.0:9443",
        session_registry=SessionRegistry(),
    )

    assert result.verdict.status is expected_status, result.verdict.failure
    assert json.loads(profile.read_text())["tokens"]["refresh_token"] == "old"
    assert json.loads(ordinary.read_text()) == {"ordinary": True}
    assert (result.run_dir / "trace.transport.jsonl").exists()
    assert result.lock is not None
    assert result.lock.agent.authentication.effective == "subscription"
    assert result.lock.agent.authentication.credential_exposed_to_agent is False
    assert result.lock.gateway.observability == "available"
    assert "refresh_token" not in result.lock.model_dump_json()
