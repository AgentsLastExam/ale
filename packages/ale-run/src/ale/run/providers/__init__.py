"""Sandbox providers: local container and virtual machine backends.

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
RETAINING_PROVIDERS = ("docker", "qemu")


class ProviderRegistry:
    """Lazily construct and reuse the configured Provider for each image kind."""

    def __init__(self, settings: RunConfig) -> None:
        self.settings = settings
        self._instances: dict[ImageKind, Provider] = {}

    def get(self, kind: ImageKind) -> Provider:
        if kind in self._instances:
            return self._instances[kind]
        if kind is ImageKind.CONTAINER:
            from ale.run.providers.docker import DockerProvider

            config = self.settings.container
            provider: Provider = DockerProvider(gpus=config.gpus)
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
    raise ProviderCapabilityError(f"unknown provider {name!r}")
