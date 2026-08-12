from __future__ import annotations

from pathlib import Path

from ale.core.config import RunConfig
from ale.core.sandbox import ImageRef, PreparedTaskImage, Provider
from ale.core.taskspec import ImageKind
from ale.run.providers import ProviderRegistry

ENGINE_ROOT = Path(__file__).resolve().parents[1]


def sibling_checkout(name: str) -> Path:
    """Locate a sibling repository from main or any linked worktree."""
    return next(
        (parent / name for parent in ENGINE_ROOT.parents if (parent / name).is_dir()),
        ENGINE_ROOT.parent / name,
    )


def provider_registry(
    provider: Provider | None = None,
    *,
    kind: ImageKind = ImageKind.CONTAINER,
) -> ProviderRegistry:
    registry = ProviderRegistry(RunConfig())
    if provider is not None:
        registry._instances[kind] = provider
    return registry


async def prepare_reference(
    provider: Provider,
    reference: str,
    *,
    kind: ImageKind = ImageKind.CONTAINER,
) -> PreparedTaskImage:
    return await provider.prepare_image(ImageRef(kind=kind, reference=reference))
