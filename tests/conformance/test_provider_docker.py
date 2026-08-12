"""The container backend against the shared provider contract."""

from __future__ import annotations

import pytest

from ale.core.config import RunConfig
from ale.core.sandbox import ImageRef, SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources
from ale.core.testkit import ProviderConformance
from ale.run.providers import ProviderRegistry
from ale.run.providers.docker import DockerProvider
from ale.run.scaffold import scaffold_task
from ale.run.task_images import prepare_task_image
from ale.run.tasksets.manifest import load_tasks

pytestmark = [pytest.mark.conformance, pytest.mark.needs_docker]


class TestDockerProvider(ProviderConformance):
    """Runs every assertion in the shared suite against docker.

    Our own base image, because the suite exercises the sandbox contract — and an image
    that does not satisfy the image contract cannot satisfy it.
    """

    provider = DockerProvider()
    image = ImageRef(
        kind="container",
        reference="ghcr.io/agentslastexam/sandbox-base-cli:latest",
    )
    gui_image = ImageRef(
        kind="container",
        reference="ghcr.io/agentslastexam/sandbox-base-gui:latest",
    )


@pytest.mark.asyncio
async def test_blocked_network_has_no_route_off_the_host() -> None:
    """Deny-all is topological: an internal bridge simply has nowhere to go."""
    from ale.core.sandbox import SandboxRequest
    from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources

    provider = DockerProvider()
    prepared = await provider.prepare_image(
        ImageRef(
            kind="container",
            reference="ghcr.io/agentslastexam/sandbox-base-cli:latest",
        )
    )
    request = SandboxRequest(
        episode_id="netprobe",
        prepared_image=prepared,
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


@pytest.mark.asyncio
async def test_proxy_only_sandbox_can_resolve_the_host_proxy() -> None:
    provider = DockerProvider()
    prepared = await provider.prepare_image(
        ImageRef(
            kind="container",
            reference="ghcr.io/agentslastexam/sandbox-base-cli:latest",
        )
    )
    request = SandboxRequest(
        episode_id="proxy-only",
        prepared_image=prepared,
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(mode="allowlist", allowed_hosts=("example.com",)),
        proxy_url="http://0.0.0.0:9443",
    )
    async with await provider.create(request) as sandbox:
        result = await sandbox.exec(
            ["python3", "-c", "import socket; print(socket.gethostbyname('ale-gateway.internal'))"],
            timeout_sec=30,
        )
    assert result.ok, result.stderr


@pytest.mark.asyncio
async def test_prepared_task_image_starts_without_remote_resolution(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = load_tasks(scaffold_task(tmp_path / "task"))[0]
    prepared = await prepare_task_image(task, ProviderRegistry(RunConfig()))

    async def remote_resolution_is_forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("prepared Task images must not use remote resolution")

    monkeypatch.setattr(
        "ale.run.providers.docker.resolve_container_image",
        remote_resolution_is_forbidden,
    )
    provider = DockerProvider()
    request = SandboxRequest(
        episode_id="prepared",
        prepared_image=prepared,
        resources=Resources(memory_mb=512),
        network=NetworkPolicy(),
    )
    async with await provider.create(request) as sandbox:
        assert sandbox.resolved_image.prepared_identity == prepared.prepared_identity
