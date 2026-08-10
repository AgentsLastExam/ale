"""Sandbox providers: local container and virtual machine backends.

A provider creates and destroys sandboxes, attaches a guest transport, applies the
network policy, and declares its capabilities. All in-sandbox operations go through
the guest service, never through provider-specific code paths.
"""

from __future__ import annotations

from ale.core.config import RunConfig
from ale.core.sandbox import Provider
from ale.core.taskspec import ImageKind

__all__ = ["ProviderRegistry"]


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
