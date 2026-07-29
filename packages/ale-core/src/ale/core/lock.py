"""Provenance.

A result is only meaningful if you can say exactly what produced it. ``RunLock``
records that, and the engine refuses to report a verdict whose lock is incomplete —
an unattributable number is worse than no number.

Two fields deserve emphasis because they are where other frameworks leak:

* ``image.digest`` is the *resolved* digest, not the tag. A moving ``:latest`` would
  otherwise make two runs look comparable when they ran on different software.
* ``agent.integrity`` pins the agent binary itself, not just a version string.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ale.core.errors import ProvenanceIncompleteError
from ale.core.ids import TaskId
from ale.core.store import AssetOrigin

__all__ = [
    "AgentProvenance",
    "AgentResourceProvenance",
    "AssetProvenance",
    "FrameworkProvenance",
    "GatewayProvenance",
    "HarnessPresetProvenance",
    "ImageProvenance",
    "JudgeProvenance",
    "KitProvenance",
    "LimitTermination",
    "RunLock",
    "SandboxProvenance",
    "TaskProvenance",
    "TaskSource",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")

SCHEMA_VERSION = 1


class TaskSource(BaseModel):
    """Where the task content came from."""

    model_config = _FROZEN

    kind: Literal["registry", "local"]
    repo: str | None = Field(default=None, description="Repository URL for registry sources")
    commit: str | None = Field(default=None, description="Resolved commit, never a branch name")
    path: str = Field(description="Task folder path within the repository, or an absolute path")

    @property
    def is_reproducible(self) -> bool:
        """Local checkouts are fine for authoring, but they cannot be re-fetched."""
        return self.kind == "registry"

    # --- validation ---

    @model_validator(mode="after")
    def _check_kind(self) -> Self:
        if self.kind == "registry" and not (self.repo and self.commit):
            raise ValueError("a registry source must record both repo and resolved commit")
        if self.kind == "local" and (self.repo or self.commit):
            raise ValueError("a local source has neither repo nor commit")
        return self


class TaskProvenance(BaseModel):
    model_config = _FROZEN

    id: TaskId
    domain: str
    variant: str | None = None
    spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source: TaskSource
    requires_core: str | None = None


class ImageProvenance(BaseModel):
    model_config = _FROZEN

    ref: str = Field(description="name:tag as declared by the task")
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", description="Resolved at run time")


class AgentProvenance(BaseModel):
    model_config = _FROZEN

    harness: str
    family: Literal["autonomous", "policy"]
    version: str = Field(description="Agent or CLI version actually executed")
    integrity: str = Field(
        description="What pins the binary: an image digest, a package hash, or a commit"
    )
    model: str
    preset: HarnessPresetProvenance | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    native_limits: dict[str, int | float | str] = Field(default_factory=dict)
    resources: tuple[AgentResourceProvenance, ...] = ()
    resources_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class HarnessPresetProvenance(BaseModel):
    model_config = _FROZEN

    name: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AgentResourceProvenance(BaseModel):
    model_config = _FROZEN

    kind: Literal["skill", "mcp"]
    name: str
    source_layers: tuple[Literal["task", "preset", "run", "cli"], ...]
    resolved_source: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_version: str | None = None
    reportable: bool
    reportability_reason: str | None = None


class JudgeProvenance(BaseModel):
    """Recorded when scoring used a model judge: a judge change moves scores."""

    model_config = _FROZEN

    model: str
    prompt_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AssetProvenance(BaseModel):
    """One asset component as it was actually materialised.

    ``origin`` matters: a run served from a pre-baked image must stay as explainable as
    one that downloaded everything, and ``data_key`` is what makes the two comparable.
    """

    model_config = _FROZEN

    component: str
    repo: str
    revision: str
    data_key: str
    origin: AssetOrigin


class KitProvenance(BaseModel):
    model_config = _FROZEN

    name: str
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SandboxProvenance(BaseModel):
    """The isolation an episode actually ran under.

    Two results obtained at different isolation levels are not comparable, and nothing
    else in the record would reveal the difference: the task, the image and the agent can
    all be identical while one run's agent could elevate and the other's could not.
    """

    model_config = _FROZEN

    user: str = Field(description="The unprivileged account the agent ran as")
    sudo: bool = Field(default=False, description="Whether that account could elevate")


class GatewayProvenance(BaseModel):
    model_config = _FROZEN

    dialect: str
    limits: dict[str, int | float | str] = Field(default_factory=dict)


class LimitTermination(BaseModel):
    model_config = _FROZEN

    layer: Literal["gateway", "harness"]
    name: str
    configured_value: int | float | Literal["unlimited"]
    observed_value: int | float | None = None
    reason: str


class FrameworkProvenance(BaseModel):
    model_config = _FROZEN

    version: str
    commit: str = Field(description="Engine git commit; 'unknown' is not acceptable in a report")
    schema_version: int = SCHEMA_VERSION


class RunLock(BaseModel):
    """The complete provenance of one episode."""

    model_config = _FROZEN

    task: TaskProvenance
    image: ImageProvenance
    agent: AgentProvenance
    framework: FrameworkProvenance
    gateway: GatewayProvenance
    trajectory_schema: Literal["ATIF-v1.7"] = "ATIF-v1.7"
    result_schema_version: Literal[1] = 1
    transport_schema_version: Literal[1] = 1
    execution_schema_version: Literal[1] = 1
    sandbox: SandboxProvenance | None = None
    config_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    seed: int
    judge: JudgeProvenance | None = None
    assets: tuple[AssetProvenance, ...] = ()
    kits: tuple[KitProvenance, ...] = ()
    termination: LimitTermination | None = None

    def missing_for_report(self) -> list[str]:
        """Reasons this lock cannot back a reported result.

        Structural completeness is already guaranteed by validation; what is checked
        here is whether the recorded values are *usable* for reproduction.
        """
        problems: list[str] = []
        if not self.task.source.is_reproducible:
            problems.append("task came from a local path and cannot be re-fetched")
        if self.framework.commit in {"", "unknown"}:
            problems.append("framework commit was not resolved")
        for resource in self.agent.resources:
            if not resource.reportable:
                problems.append(
                    f"{resource.kind} {resource.name!r} is not reportable: "
                    f"{resource.reportability_reason or 'source is not immutable'}"
                )
        return problems

    def require_reportable(self) -> None:
        """Raise unless this lock is good enough to publish a result against."""
        if problems := self.missing_for_report():
            raise ProvenanceIncompleteError("; ".join(problems))
