"""An image says what it provides; the engine reads rather than infers.

Both inference failures this contract exists to prevent were silent. Substituting a
keep-alive command replaced a desktop image's whole graphical session, so the image
claimed a desktop and had none. Treating a sandbox as ready when the guest service
answered meant using a screen that did not exist yet.

So the checks here are about *refusing clearly and early*: an image that cannot serve as
a sandbox should say which requirement it misses, not fail later as a dead container or
a permission error with no apparent cause.
"""

from __future__ import annotations

import pytest

from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources
from ale.run.providers.docker import DEFAULT_AGENT_USER, DockerProvider

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

CONFORMING = "ghcr.io/agentslastexam/sandbox-base-cli:latest"

#: A build environment, not a sandbox: no unprivileged account, and a command that exits
#: at once. Referencing one directly is exactly what the contract rules out.
UPSTREAM = "docker.io/library/python:3.12-slim"


@pytest.mark.asyncio
async def test_our_base_image_conforms() -> None:
    assert await DockerProvider().check_image(CONFORMING) == []


@pytest.mark.asyncio
async def test_an_upstream_image_is_refused_by_name() -> None:
    """Named, so the answer is what to fix rather than that something went wrong."""
    problems = await DockerProvider().check_image(UPSTREAM)

    assert problems
    assert any("unprivileged" in problem for problem in problems)


@pytest.mark.asyncio
async def test_a_non_conforming_image_never_reaches_provisioning() -> None:
    """Refused before a container exists, not diagnosed from its corpse."""
    request = SandboxRequest(
        episode_id="contract",
        image_ref=UPSTREAM,
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(),
    )

    with pytest.raises(ProviderCapabilityError, match=r"sandbox-image\.md"):
        await DockerProvider().create(request)


@pytest.mark.asyncio
async def test_the_check_is_paid_for_once() -> None:
    """It starts a container, so a sweep must not pay per episode."""
    provider = DockerProvider()
    await provider.check_image(CONFORMING)
    assert CONFORMING in provider._checked


@pytest.mark.asyncio
async def test_the_agent_account_comes_from_the_image() -> None:
    """The image created it, so the image names it."""
    assert await DockerProvider()._agent_user(CONFORMING) == DEFAULT_AGENT_USER
