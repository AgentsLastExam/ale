"""The container backend against the shared provider contract."""

from __future__ import annotations

import pytest

from ale.core.testkit import ProviderConformance
from ale.run.providers.docker import DockerProvider

pytestmark = [pytest.mark.conformance, pytest.mark.needs_docker]


class TestDockerProvider(ProviderConformance):
    """Runs every assertion in the shared suite against docker.

    Our own base image, because the suite exercises the sandbox contract — and an image
    that does not satisfy the image contract cannot satisfy it.
    """

    provider = DockerProvider()
    image_ref = "ghcr.io/agentslastexam/sandbox-base-cli:latest"


@pytest.mark.asyncio
async def test_blocked_network_has_no_route_off_the_host() -> None:
    """Deny-all is topological: an internal bridge simply has nowhere to go."""
    from ale.core.sandbox import SandboxRequest
    from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources

    provider = DockerProvider()
    request = SandboxRequest(
        episode_id="netprobe",
        image_ref="ghcr.io/agentslastexam/sandbox-base-cli:latest",
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
    )
    async with await provider.create(request) as sandbox:
        result = await sandbox.exec(
            [
                "python3",
                "-c",
                "import socket;socket.setdefaulttimeout(5);"
                "socket.create_connection(('1.1.1.1', 443))",
            ],
            timeout_sec=30,
        )
        assert result.exit_code != 0, "sandbox reached the internet on a blocked network"
