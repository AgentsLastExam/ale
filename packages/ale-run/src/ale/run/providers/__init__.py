"""Sandbox providers: local container, virtual machine and cloud container backends.

A provider creates and destroys sandboxes, attaches a guest transport, applies the
network policy, and declares its capabilities. All in-sandbox operations go through
the guest service, never through provider-specific code paths.
"""

from __future__ import annotations

from ale.core.config import RunConfig
from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import Provider
from ale.core.taskspec import ImageKind

__all__ = ["RETAINING_PROVIDERS", "ProviderRegistry", "provider_named"]

#: Every Provider that can leave a sandbox behind. A retained sandbox outlives the run
#: configuration that made it, so listing and destroying consult all of them.
RETAINING_PROVIDERS = ("docker", "qemu", "daytona")


class ProviderRegistry:
    """Lazily construct and reuse the configured Provider for each image kind."""

    def __init__(self, settings: RunConfig) -> None:
        self.settings = settings
        self._instances: dict[ImageKind, Provider] = {}

    def get(self, kind: ImageKind) -> Provider:
        if kind in self._instances:
            return self._instances[kind]
        if kind is ImageKind.CONTAINER:
            config = self.settings.container
            if config.provider == "daytona":
                from ale.run.providers.daytona import DaytonaProvider

                provider: Provider = DaytonaProvider(config.daytona)
            else:
                from ale.run.providers.docker import DockerProvider

                provider = DockerProvider(gpus=config.gpus)
        elif self.settings.vm.provider == "orchard":
            from ale.run.providers.orchard import OrchardProvider

            orchard_config = self.settings.vm.orchard
            assert orchard_config is not None
            provider = OrchardProvider(orchard_config)
        else:
            from ale.run.providers.qemu import QemuProvider

            provider = QemuProvider()
        self._instances[kind] = provider
        return provider


def provider_named(name: str) -> Provider:
    """A default-configured Provider, for work on sandboxes it retained earlier.

    Retained handles and RunLocks name the Provider that made a sandbox; that name, not
    the current run configuration, decides who can attach to or destroy it.
    """
    if name == "docker":
        from ale.run.providers.docker import DockerProvider

        return DockerProvider()
    if name == "qemu":
        from ale.run.providers.qemu import QemuProvider

        return QemuProvider()
    if name == "daytona":
        from ale.run.providers.daytona import DaytonaProvider

        return DaytonaProvider()
    raise ProviderCapabilityError(f"unknown provider {name!r}")
