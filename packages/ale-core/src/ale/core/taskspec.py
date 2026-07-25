"""The task specification: the wire atom of ALE.

A ``TaskSpec`` is frozen, fully serialisable, and self-contained: everything needed to
provision a sandbox, brief an agent and score the result. Its hash identifies the task
instance in provenance records and in resume decisions, so it covers the *rendered*
instruction — what the agent actually saw — not a template.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ale.core.ids import TaskId, content_hash

__all__ = [
    "AssetRef",
    "AssetsStep",
    "HarnessFamily",
    "ImageRef",
    "KitRef",
    "KitStep",
    "NetworkMode",
    "NetworkPolicy",
    "PhaseTimeouts",
    "Resources",
    "ScriptStep",
    "SetupStep",
    "TaskSpec",
    "ToolProvision",
    "ValidateSpec",
    "Workspace",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Workspace(StrEnum):
    """The fixed in-sandbox layout. Instructions reference these paths literally."""

    INPUT = "/ale/input"
    SOFTWARE = "/ale/software"
    OUTPUT = "/ale/output"
    WORK = "/ale/work"
    REFERENCE = "/ale/reference"
    """Verification only: never present while the agent is running."""


class HarnessFamily(StrEnum):
    """Which side owns the interaction loop."""

    AUTONOMOUS = "autonomous"
    POLICY = "policy"


class ImageRef(BaseModel):
    """A sandbox image by name and tag.

    The digest is deliberately absent: it is resolved at run time and recorded in the
    provenance record, so a moved tag is detectable instead of silently comparable.
    """

    model_config = _FROZEN

    name: str = Field(description="Short name (resolved against the default registry) or full ref")
    tag: str = "latest"

    def __str__(self) -> str:
        return f"{self.name}:{self.tag}"


class AssetRef(BaseModel):
    """A component of a domain's pinned asset bundle."""

    model_config = _FROZEN

    component: str
    stage: Literal["setup", "verify"] = "setup"
    """``setup`` lands in the workspace before the agent; ``verify`` only at scoring."""

    lock: str = "assets.lock.yaml"


class KitRef(BaseModel):
    """A versioned code package injected into the sandbox."""

    model_config = _FROZEN

    name: str
    version: str


class ScriptStep(BaseModel):
    """Run a script from the task folder inside the sandbox."""

    model_config = _FROZEN

    kind: Literal["script"] = "script"
    script: str = Field(description="Path relative to the task folder, e.g. setup/prepare.sh")
    timeout_sec: float | None = Field(default=None, gt=0)


class AssetsStep(BaseModel):
    """Materialise an asset component into the workspace."""

    model_config = _FROZEN

    kind: Literal["assets"] = "assets"
    assets: AssetRef


class KitStep(BaseModel):
    """Materialise a kit into the sandbox."""

    model_config = _FROZEN

    kind: Literal["kit"] = "kit"
    kit: KitRef


SetupStep = Annotated[ScriptStep | AssetsStep | KitStep, Field(discriminator="kind")]


class Resources(BaseModel):
    """What the sandbox needs.

    Single scalars: how a backend turns a request into a reservation or a hard limit is
    a runtime policy, not a property of the task.
    """

    model_config = _FROZEN

    cpus: int = Field(default=1, ge=1)
    memory_mb: int = Field(default=1024, ge=128)
    storage_mb: int | None = Field(default=None, ge=256)
    gpus: int = Field(default=0, ge=0)
    gpu_vram_gb: int | None = Field(default=None, ge=1)


class NetworkMode(StrEnum):
    BLOCK = "block"
    ALLOWLIST = "allowlist"
    OPEN = "open"


class NetworkPolicy(BaseModel):
    """Egress policy. The gateway is reachable in every mode, and only the gateway
    holds real credentials."""

    model_config = _FROZEN

    mode: NetworkMode = NetworkMode.BLOCK
    allowed_hosts: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_hosts(self) -> Self:
        if self.mode is NetworkMode.ALLOWLIST and not self.allowed_hosts:
            raise ValueError("allowlist mode requires at least one allowed host")
        if self.mode is not NetworkMode.ALLOWLIST and self.allowed_hosts:
            raise ValueError(f"allowed_hosts is meaningless in {self.mode} mode")
        return self


class PhaseTimeouts(BaseModel):
    """Per-phase deadlines in seconds, enforced by the engine rather than the agent."""

    model_config = _FROZEN

    setup: float = Field(default=120, gt=0)
    agent: float = Field(default=900, gt=0)
    verify: float = Field(default=300, gt=0)

    @property
    def total(self) -> float:
        return self.setup + self.agent + self.verify


class ToolProvision(BaseModel):
    """Extra capabilities handed to the agent for this task."""

    model_config = _FROZEN

    skills: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    """Names of MCP servers the engine wires up, e.g. the desktop bridge."""


class ValidateSpec(BaseModel):
    """The admission gate: an oracle solution must reach ``min_reward``."""

    model_config = _FROZEN

    mode: Literal["oracle", "manual"] = "oracle"
    min_reward: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: str | None = Field(default=None, description="Required when mode is manual")

    @model_validator(mode="after")
    def _check_reason(self) -> Self:
        if self.mode == "manual" and not self.reason:
            raise ValueError("manual validation requires a reason")
        return self


class TaskSpec(BaseModel):
    """One task instance, fully specified."""

    model_config = _FROZEN

    id: TaskId
    spec_type: str = "core/v1"
    environment: str = "core/standard"
    harness_family: HarnessFamily = HarnessFamily.AUTONOMOUS

    instruction: str = Field(
        description="The rendered prompt: substitution already applied, paths literal"
    )
    image: ImageRef
    resources: Resources = Resources()
    network: NetworkPolicy = NetworkPolicy()
    timeouts: PhaseTimeouts = PhaseTimeouts()
    setup: tuple[SetupStep, ...] = ()
    tools: ToolProvision = ToolProvision()
    params: dict[str, Any] = Field(
        default_factory=dict, description="Values substituted into the instruction"
    )
    validate_: ValidateSpec = Field(default=ValidateSpec(), alias="validate")
    metadata: dict[str, Any] = Field(default_factory=dict)
    extras: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Namespaced experiments; promoted into the schema once shared",
    )

    @property
    def family(self) -> str:
        """The identifier without its variant suffix."""
        return self.id.family

    @property
    def variant(self) -> str | None:
        return self.id.variant

    @property
    def spec_hash(self) -> str:
        """``sha256:<hex>`` over the canonical form. The task instance's identity."""
        return content_hash(self.model_dump(mode="json", by_alias=True))

    def asset_refs(self, stage: Literal["setup", "verify"] | None = None) -> tuple[AssetRef, ...]:
        """Asset components declared by this task, optionally filtered by stage."""
        refs = tuple(s.assets for s in self.setup if isinstance(s, AssetsStep))
        if stage is None:
            return refs
        return tuple(ref for ref in refs if ref.stage == stage)

    def kit_refs(self) -> tuple[KitRef, ...]:
        return tuple(s.kit for s in self.setup if isinstance(s, KitStep))
