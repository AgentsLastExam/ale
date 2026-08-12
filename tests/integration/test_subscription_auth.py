"""Provider-independent subscription profile lifecycle inside a real Task Sandbox."""

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
from ale.run.subscription import ProfileLease, resolve_authentication
from ale.run.tasksets.manifest import load_tasks
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
proxy = __import__('os').environ['HTTPS_PROXY']
assert proxy.startswith('http://') and ':@' in proxy
assert 'ale-gateway.internal' not in proxy
auth = home / '.codex-ale/auth.json'
payload = json.loads(auth.read_text())
assert payload['tokens']['refresh_token'] == 'old'
payload['tokens']['refresh_token'] = 'new'
auth.write_text(json.dumps(payload))
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
async def test_isolated_profile_round_trips_through_task_sandbox(
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
        gateway=gateway_provenance(settings, observable=False),
        config_hash=settings.config_hash,
    )

    async with ProfileLease(authentication) as lease:
        result = await run_episode(
            task,
            StandardEnvironment(harness),
            provider_registry(DockerProvider()),
            run_dir=tmp_path / "runs",
            model=settings.agent.model,
            provenance=provenance,
            authentication="subscription",
            profile_slot_id=authentication.profile_slot_id,
            subscription_credential=lease.credential,
            subscription_lease=lease,
            proxy_url="http://0.0.0.0:9443",
            session_registry=SessionRegistry(),
            allowed_hosts=frozenset({"chatgpt.com"}),
        )

    assert result.verdict.status is expected_status, result.verdict.failure
    assert json.loads(profile.read_text())["tokens"]["refresh_token"] == "new"
    assert json.loads(ordinary.read_text()) == {"ordinary": True}
    assert not (result.run_dir / "trace.transport.jsonl").exists()
    assert result.lock is not None
    assert result.lock.agent.authentication.effective == "subscription"
    assert result.lock.agent.authentication.credential_exposed_to_agent is True
    assert result.lock.gateway.observability == "unavailable"
    assert result.lock.gateway.limits == {}
    assert "refresh_token" not in result.lock.model_dump_json()
