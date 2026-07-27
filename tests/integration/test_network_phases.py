"""When the declared network policy applies, and when it does not.

A task's network policy describes what binds **the agent** — that is the thing being
measured, and the only one whose reach is a result rather than a detail. Setup and verify
are the task's own trusted code and the harness's preparation is ours; holding all three
to the agent's limits bought nothing and cost the ability to install an agent into an
image that had not pre-baked one.

What must remain exactly true is the part that is a claim: during the agent's phase, a
sandbox declaring `block` reaches nothing but the gateway. These tests assert both halves,
because a change that opened the first without keeping the second would be invisible in
every score and false in every lock file.
"""

from __future__ import annotations

import pytest

from ale.core.sandbox import Identity, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.run.providers.docker import DockerProvider

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"
PROBE = ["timeout", "10", "curl", "-sS", "-o", "/dev/null", "https://registry.npmjs.org/"]


async def _sandbox(mode: NetworkMode, *, gateway: str = ""):  # type: ignore[no-untyped-def]
    return await DockerProvider().create(
        SandboxRequest(
            episode_id="net-phases",
            image_ref=IMAGE,
            resources=Resources(cpus=1, memory_mb=1024),
            network=NetworkPolicy(mode=mode),
            gateway_url=gateway,
        )
    )


@pytest.mark.asyncio
async def test_a_blocked_task_still_reaches_the_network_before_the_agent_runs() -> None:
    """Otherwise an image without a pre-baked agent could never obtain one."""
    sandbox = await _sandbox(NetworkMode.BLOCK)
    try:
        await sandbox.open_egress()
        result = await sandbox.exec(PROBE, timeout_sec=30)
        assert result.exit_code == 0, result.stderr
    finally:
        await sandbox.destroy()


@pytest.mark.asyncio
async def test_and_reaches_nothing_once_it_is_sealed() -> None:
    """The half that is a claim. Asserted after opening, so it is a transition."""
    sandbox = await _sandbox(NetworkMode.BLOCK)
    try:
        await sandbox.open_egress()
        assert (await sandbox.exec(PROBE, timeout_sec=30)).exit_code == 0

        await sandbox.close_egress()
        # As the agent, since the agent is who the policy is about.
        result = await sandbox.exec(PROBE, timeout_sec=30, identity=Identity.AGENT)
        assert result.exit_code != 0, "the sandbox reached the network after being sealed"
    finally:
        await sandbox.destroy()


@pytest.mark.asyncio
async def test_the_gateway_survives_both_states() -> None:
    """The agent is handed one gateway address at the start and must keep it.

    Opening egress attaches a second network and sealing detaches it; if that moved the
    address the sandbox reaches the gateway on, every episode would break halfway through
    for a reason with nothing to do with the task.
    """
    # A gateway URL is what puts the alias in the sandbox's hosts file; without one there
    # is no gateway to keep reachable and nothing here to test.
    sandbox = await _sandbox(NetworkMode.BLOCK, gateway="http://127.0.0.1:9999")
    try:
        before = sandbox.gateway_url
        await sandbox.open_egress()
        await sandbox.close_egress()
        assert sandbox.gateway_url == before

        # Resolvable, which is the part an attached-and-detached bridge could break.
        result = await sandbox.exec(["getent", "hosts", "ale-gateway.internal"], timeout_sec=30)
        assert result.exit_code == 0, result.stderr
    finally:
        await sandbox.destroy()


@pytest.mark.asyncio
async def test_a_task_that_declared_open_is_never_sealed() -> None:
    sandbox = await _sandbox(NetworkMode.OPEN)
    try:
        await sandbox.close_egress()
        result = await sandbox.exec(PROBE, timeout_sec=30, identity=Identity.AGENT)
        assert result.exit_code == 0, result.stderr
    finally:
        await sandbox.destroy()
