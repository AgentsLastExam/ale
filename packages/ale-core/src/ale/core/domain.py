"""The domain manifest.

A task repository says two things about itself: what namespace its tasks belong to, and
which engine versions they were written against. Everything else — where data comes
from, which image to use, what to collect — belongs to individual tasks, because that is
where the knowledge actually lives.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["DomainManifest"]


class DomainManifest(BaseModel):
    """``domain.yaml`` at the root of a task repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Namespace and registry key for this repository")
    requires_core: str | None = Field(
        default=None, description="Engine version specifier these tasks target"
    )
