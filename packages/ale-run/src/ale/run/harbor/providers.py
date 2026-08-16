"""Provider selection for Harbor Tasks."""

from ale.core.sandbox import Provider
from ale.core.taskspec import ImageKind
from ale.run.harbor.docker import HarborDockerProvider
from ale.run.providers import ProviderRegistry

__all__ = ["HarborProviderRegistry"]


class HarborProviderRegistry(ProviderRegistry):
    def get(self, kind: ImageKind) -> Provider:
        if kind is not ImageKind.CONTAINER:
            raise ValueError("HarborEnvironment supports only container images")
        if kind not in self._instances:
            self._instances[kind] = HarborDockerProvider(gpus=self.settings.container.gpus)
        return self._instances[kind]
