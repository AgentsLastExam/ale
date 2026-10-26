"""Opt-in Orchard qualification against the shared Sandbox contract.

Run only against a disposable worker/image with ALE_TEST_ORCHARD=1,
ALE_ORCHARD_TEST_CONFIG (an OrchardProviderConfig JSON object),
ALE_ORCHARD_TEST_IMAGE (digest-pinned OCI reference), and ALE_ORCHARD_TEST_USER.
The configured account must satisfy sudo=false. Credentials use the config's
explicit environment-variable names; this suite never loads a checkout config.
The suite uses one image. Configure driver_path and socket_path for that image
to include desktop capture/input checks; omit them for headless qualification.
Each test creates and destroys its own VM; none adopts an existing VM.
"""

from __future__ import annotations

import os

import pytest

from ale.core.config import OrchardProviderConfig
from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import ImageRef, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.core.testkit import ProviderConformance
from ale.run.providers.orchard import OrchardProvider

pytestmark = [
    pytest.mark.conformance,
    pytest.mark.needs_orchard,
    pytest.mark.skipif(
        os.environ.get("ALE_TEST_ORCHARD") != "1",
        reason="set ALE_TEST_ORCHARD=1 with explicit disposable Orchard test configuration",
    ),
]


class TestOrchardProvider(ProviderConformance):
    """Shared file, identity, lifecycle, resource and optional desktop guarantees.

    Orchard rejects sealed networks and retention at admission. Those two inherited
    cases check that rejection explicitly; all other shared assertions run unchanged.
    """

    @pytest.fixture(autouse=True)
    def configure(self) -> None:
        config = OrchardProviderConfig.model_validate_json(os.environ["ALE_ORCHARD_TEST_CONFIG"])
        self.provider = OrchardProvider(config)
        self.image = ImageRef(kind="vm", reference=os.environ["ALE_ORCHARD_TEST_IMAGE"])
        self.user = os.environ["ALE_ORCHARD_TEST_USER"]
        self.gui_image = self.image if config.driver_path else None

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

    @pytest.mark.asyncio
    async def test_a_retained_sandbox_can_be_listed_and_destroyed(self) -> None:
        request = await self.request(retention="keep")
        with pytest.raises(ProviderCapabilityError):
            await self.provider.create(request)
