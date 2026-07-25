"""Domain and asset manifests — a task repository's own declarations.

``domain.yaml`` says who a repository is and which engine versions it targets.
``assets.lock.yaml`` pins large data and, crucially, declares each component's
visibility.

Visibility travels with the data rather than with the task on purpose. A component
marked ``verify`` cannot reach the agent no matter which stage a task lists it under,
so answer material is protected centrally instead of by every author remembering the
rule. One mistake in a hundred and fifty tasks would otherwise be enough.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AssetComponent", "AssetsLock", "DomainManifest", "Visibility"]

_FROZEN = ConfigDict(frozen=True, extra="forbid")

Visibility = Literal["setup", "verify"]


class DomainManifest(BaseModel):
    """``domain.yaml`` at the root of a task repository."""

    model_config = _FROZEN

    name: str = Field(description="Namespace and registry key for this repository")
    requires_core: str | None = Field(
        default=None, description="Engine version specifier these tasks target"
    )
    requires_extensions: tuple[str, ...] = ()
    default_image: str | None = Field(
        default=None, description="Image for tasks that declare none of their own"
    )


class AssetComponent(BaseModel):
    """One bundle in the domain's asset lock."""

    model_config = _FROZEN

    path: str = Field(description="Path within the asset repository")
    visibility: Visibility = "setup"
    """``verify`` components are materialised only during scoring, never for the agent."""


class AssetsLock(BaseModel):
    """``assets.lock.yaml`` — large data pinned by revision."""

    model_config = _FROZEN

    repo: str = ""
    revision: str = ""
    components: dict[str, AssetComponent] = Field(default_factory=dict)

    def component(self, name: str) -> AssetComponent:
        if name not in self.components:
            known = ", ".join(sorted(self.components)) or "none"
            raise KeyError(f"unknown asset component {name!r}; declared: {known}")
        return self.components[name]

    def visibility_of(self, name: str) -> Visibility:
        return self.component(name).visibility
