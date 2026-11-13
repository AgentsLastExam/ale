"""Sandbox providers: local container, microVM, virtual machine and cloud container backends.

A provider creates and destroys sandboxes, attaches a guest transport, applies the
network policy, and declares its capabilities. All in-sandbox operations go through
the guest service, never through provider-specific code paths.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from ale.core.config import RunConfig
from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import Provider
from ale.core.taskspec import ImageKind, OperatingSystem

if TYPE_CHECKING:
    from ale.run.providers.fleet import FleetClient

__all__ = ["RETAINING_PROVIDERS", "ProviderRegistry", "provider_named"]

#: Every Provider that can leave a sandbox behind. A retained sandbox outlives the run
#: configuration that made it, so listing and destroying consult all of them.
RETAINING_PROVIDERS = ("docker", "firecracker", "qemu", "daytona", "orchard", "fleet")


class ProviderRegistry:
    """Lazily construct and reuse the configured Provider for each image kind.

    macOS VMs run on the local Orchard, or on the Fleet when ``vm.provider`` is ``fleet``.
    Every kind the Fleet serves shares one connection to it.
    """

    def __init__(self, settings: RunConfig) -> None:
        self.settings = settings
        self._instances: dict[tuple[ImageKind, bool], Provider] = {}
        self._fleet: FleetClient | None = None

    @property
    def fleet(self) -> FleetClient | None:
        """The Run's connection to its Fleet, once a Provider uses it."""
        return self._fleet

    async def close(self) -> None:
        if self._fleet is not None:
            await self._fleet.close()

    def _on_fleet(self, kind: ImageKind) -> bool:
        if kind is ImageKind.CONTAINER:
            return self.settings.container.provider == "fleet"
        return self.settings.vm.provider == "fleet"

    def get(self, kind: ImageKind, operating_system: OperatingSystem) -> Provider:
        macos = kind is ImageKind.VM and operating_system is OperatingSystem.MACOS
        if (kind, macos) in self._instances:
            return self._instances[(kind, macos)]
        if self._on_fleet(kind):
            from ale.run.providers.fleet.provider import FleetProvider, fleet_client

            if self._fleet is None:
                config = self.settings.fleet
                self._fleet = fleet_client(config.url, config.token_env)
            provider: Provider = FleetProvider(self._fleet)
            self._instances[(kind, macos)] = provider
            return provider
        if macos:
            from ale.run.providers.orchard import OrchardProvider

            provider = OrchardProvider()
        elif kind is ImageKind.CONTAINER:
            config = self.settings.container
            if config.provider == "daytona":
                from ale.run.providers.daytona import DaytonaProvider

                provider = DaytonaProvider(config.daytona)
            elif config.provider == "firecracker":
                from ale.run.providers.firecracker import FirecrackerProvider

                provider = FirecrackerProvider()
            else:
                from ale.run.providers.docker import DockerProvider

                provider = DockerProvider(gpus=config.gpus, runtime=config.runtime)
        else:
            from ale.run.providers.qemu import QemuProvider

            provider = QemuProvider()
        self._instances[(kind, macos)] = provider
        return provider


def provider_named(name: str) -> Provider:
    """A default-configured Provider, for work on sandboxes it retained earlier.

    Retained handles and RunLocks name the Provider that made a sandbox; that name, not
    the current run configuration, decides who can attach to or destroy it. The Fleet is
    the one whose control plane ``$ALE_FLEET_URL`` names.
    """
    if name == "docker":
        from ale.run.providers.docker import DockerProvider

        return DockerProvider()
    if name == "firecracker":
        from ale.run.providers.firecracker import FirecrackerProvider

        return FirecrackerProvider()
    if name == "qemu":
        from ale.run.providers.qemu import QemuProvider

        return QemuProvider()
    if name == "daytona":
        from ale.run.providers.daytona import DaytonaProvider

        return DaytonaProvider()
    if name == "orchard":
        from ale.run.providers.orchard import OrchardProvider

        return OrchardProvider()
    if name == "fleet":
        from ale.run.providers.fleet.provider import FleetProvider, fleet_client

        url = os.environ.get("ALE_FLEET_URL", "")
        if not url:
            raise ProviderCapabilityError("set ALE_FLEET_URL to the Fleet's control plane")
        return FleetProvider(fleet_client(url, "ALE_FLEET_TOKEN"))
    raise ProviderCapabilityError(f"unknown provider {name!r}")
