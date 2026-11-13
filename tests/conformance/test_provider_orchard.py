"""Opt-in Orchard qualification against the shared Sandbox contract.

Run on a Mac whose local Orchard ``scripts/setup_macos.py`` keeps running, with
ALE_TEST_ORCHARD=1. The suite builds its image on that Mac once, as ``ale prepare`` would,
from ``tests/fixtures/tasks/macos-unprivileged``: it signs in ``tester``, an account other
than ``admin``, so the sudo=false cases apply. Every macOS sandbox has that account's
desktop, so the desktop capture and input checks always run. Each test destroys the VMs and
snapshots it creates; none adopts a VM it did not create.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import PreparedTaskImage, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.core.testkit import ProviderConformance
from ale.run.providers import ProviderRegistry
from ale.run.providers.orchard import OrchardProvider
from ale.run.task_images import prepare_task_image
from ale.run.tasksets.manifest import load_tasks

pytestmark = [
    pytest.mark.conformance,
    pytest.mark.needs_orchard,
    pytest.mark.skipif(
        os.environ.get("ALE_TEST_ORCHARD") != "1",
        reason="set ALE_TEST_ORCHARD=1 on a Mac whose local Orchard setup keeps running",
    ),
]

TASK = Path(__file__).resolve().parents[1] / "fixtures/tasks/macos-unprivileged"


@pytest.fixture(scope="module")
def unprivileged() -> tuple[PreparedTaskImage, str]:
    """The fixture Task's image, built on its base the first time, and its account."""
    task = load_tasks(TASK)[0]
    image = asyncio.run(prepare_task_image(task, ProviderRegistry(RunConfig())))
    return image, task.spec.image.user


class TestOrchardProvider(ProviderConformance):
    """Shared file, identity, lifecycle, retention, resource and desktop guarantees.

    Orchard rejects sealed networks at admission. The inherited case that asks for them
    checks that rejection explicitly; all other shared assertions run unchanged.
    """

    @pytest.fixture(autouse=True)
    def configure(self, unprivileged: tuple[PreparedTaskImage, str]) -> None:
        self.provider = OrchardProvider()
        self.image, self.user = unprivileged
        self.gui_image = self.image

    async def request(self, **overrides: object) -> SandboxRequest:
        resources = overrides.get("resources", Resources())
        assert isinstance(resources, Resources)
        overrides["resources"] = resources.model_copy(
            update={"cpus": max(2, resources.cpus), "memory_mb": max(4096, resources.memory_mb)}
        )
        overrides.setdefault("network", NetworkPolicy(mode=NetworkMode.OPEN))
        overrides.setdefault("os", OperatingSystem.MACOS)
        overrides.setdefault("user", self.user)
        return await super().request(**overrides)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [NetworkMode.BLOCK, NetworkMode.ALLOWLIST])
    async def test_a_sealed_sandbox_reaches_its_gateway_and_nothing_else(
        self, mode: NetworkMode
    ) -> None:
        request = await self.request(
            network=NetworkPolicy(
                mode=mode, allowed_hosts=("example.com",) if mode is NetworkMode.ALLOWLIST else ()
            )
        )
        with pytest.raises(ProviderCapabilityError):
            await self.provider.create(request)
