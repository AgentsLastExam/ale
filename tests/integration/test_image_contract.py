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

import subprocess

import pytest

from ale.core.errors import ProviderCapabilityError, TaskDefinitionError
from ale.core.sandbox import ImageRef
from ale.run.framework import with_framework
from ale.run.images import resolve_container_reference
from ale.run.providers.docker import DockerProvider
from tests.support import CONTAINER_BASE

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

CONFORMING = CONTAINER_BASE

#: A build environment, not a sandbox: no unprivileged account, and a command that exits
#: at once. Referencing one directly is exactly what the contract rules out.
UPSTREAM = "docker.io/library/python:3.12-slim"
OLD_PYTHON = "docker.io/library/python:3.10-slim"


async def _pulled(reference: str) -> str:
    """The check inspects a local image, so each test makes sure its image is there.

    A local copy is used as it is: CI builds this engine version's base before publishing.
    """
    present = subprocess.run(["docker", "image", "inspect", reference], capture_output=True)
    if present.returncode != 0:
        await resolve_container_reference(reference)
    return reference


@pytest.mark.asyncio
async def test_our_base_image_conforms() -> None:
    assert await DockerProvider().check_image(await _pulled(CONFORMING)) == []


@pytest.mark.asyncio
async def test_an_image_without_the_tasks_account_is_refused_by_name() -> None:
    """Any image serves once it has Python and the account; what is missing is named."""
    prepared = await DockerProvider().prepare_image(ImageRef(kind="container", reference=UPSTREAM))
    with pytest.raises(TaskDefinitionError, match=r"\[agent\]\.user"):
        await with_framework(prepared.runtime_ref, user="user", sudo=False)


@pytest.mark.asyncio
async def test_an_old_python_image_is_refused_by_name() -> None:
    problems = await DockerProvider().check_image(await _pulled(OLD_PYTHON))

    assert any("older than 3.12" in problem for problem in problems)


@pytest.mark.asyncio
async def test_a_non_conforming_image_never_reaches_provisioning() -> None:
    """Refused before a container exists, not diagnosed from its corpse."""
    with pytest.raises(ProviderCapabilityError, match=r"sandbox-image\.md"):
        await DockerProvider().prepare_image(ImageRef(kind="container", reference=OLD_PYTHON))


@pytest.mark.asyncio
async def test_the_check_is_paid_for_once() -> None:
    """It starts a container, so a sweep must not pay per episode."""
    provider = DockerProvider()
    await provider.check_image(CONFORMING)
    assert CONFORMING in provider._checked
