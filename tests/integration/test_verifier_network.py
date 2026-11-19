"""The verifier's own network, end to end in a shared Docker Sandbox: the agent and the
verifier run in one Sandbox, each with its own policy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.session import SessionRegistry
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers.docker import DockerProvider
from ale.run.tasksets.manifest import load_tasks
from tests.support import CONTAINER_BASE, provider_registry, write_task

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

DECLARED, OTHER = "https://example.com/", "https://www.iana.org/"
#: What reaching ``$1`` gave: the HTTP status and, through a proxy, the CONNECT status;
#: ``000 000`` when nothing answered.
PROBE = (
    "probe() { curl -sS -o /dev/null -m 20 -w '%{http_code} %{http_connect}' \"$1\" "
    "2>/dev/null || true; }\n"
)
SOLVE = f"#!/usr/bin/env bash\n{PROBE}probe {DECLARED} > /home/user/agent.txt\n"
TEST = f"""#!/usr/bin/env bash
set -eu
{PROBE}echo "agent=$(cat /home/user/agent.txt)"
echo "declared=$(probe {DECLARED})"
echo "other=$(probe {OTHER})"
echo 1 > /logs/verifier/reward.txt
"""


async def _probes(tmp_path: Path, config: dict[str, Any], **episode: Any) -> dict[str, str]:
    """What the oracle, standing in for the agent, and then ``tests/`` reached."""
    task = write_task(
        tmp_path / "task",
        config={"environment": {"image": CONTAINER_BASE}, **config},
        dockerfile=None,
        test=TEST,
        solve=SOLVE,
    )
    result = await run_episode(
        load_tasks(task)[0],
        EpisodeProtocol(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=tmp_path / "runs",
        **episode,
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    records = [
        json.loads(line)
        for line in (result.run_dir / "trace.execution.jsonl").read_text().splitlines()
    ]
    (stage,) = [
        record
        for record in records
        if record["kind"] == "command_finished" and record["phase"] == "verify"
        if record["component"] == "task-stage"
    ]
    applied = [
        (record["phase"], record["policy"])
        for record in records
        if record["kind"] == "policy_applied"
    ]
    lines = stage["stdout"]["inline"].splitlines()
    return dict(line.split("=", 1) for line in lines) | {"applied": str(applied)}


@pytest.mark.asyncio
async def test_a_no_network_verifier_reaches_nothing_its_public_agent_reached(
    tmp_path: Path,
) -> None:
    probes = await _probes(
        tmp_path,
        {"agent": {"network_mode": "public"}, "verifier": {"network_mode": "no-network"}},
    )
    assert probes["agent"].split()[0] == "200", probes
    assert probes["declared"] == "000 000", probes
    assert probes["other"] == "000 000", probes
    assert probes["applied"] == str([("agent", "open"), ("verify", "sealed")])


@pytest.mark.asyncio
async def test_a_verifier_allowlist_reaches_its_declared_host_only(tmp_path: Path) -> None:
    registry = SessionRegistry()
    proxy = EgressProxy(registry, host="0.0.0.0")
    await proxy.start()
    try:
        probes = await _probes(
            tmp_path,
            {
                "agent": {"network_mode": "no-network"},
                "verifier": {"network_mode": "allowlist", "allowed_hosts": ["example.com"]},
            },
            proxy_url=proxy.url,
            session_registry=registry,
        )
    finally:
        await proxy.stop()
    # The relay was there while the agent ran, and the proxy refused it: its egress session
    # held no hosts then.
    assert probes["agent"] == "000 407", probes
    assert probes["declared"] == "200 200", probes
    assert probes["other"] == "000 403", probes
    assert probes["applied"] == str([("agent", "sealed"), ("verify", "sealed")])
