"""The task specification: the wire atom of ALE.

A ``TaskSpec`` is frozen, fully serialisable, and self-contained: everything needed to
provision a sandbox, brief an agent and score the result. Its hash identifies the task
instance in provenance records and in resume decisions, so it covers the *rendered*
instruction — what the agent actually saw — not a template.

Three shapes here are worth reading twice:

* ``id`` is opaque. The domain and the variant are their own fields, and nothing in the
  framework parses the identifier.
* ``setup`` and ``verify`` are the same shape. Scripts are not declared at all: a
  stage's folder is copied in and its entry point runs, so the task's layout on disk is
  its execution semantics.
* ``Workspace`` paths are fixed and identical for every task. Data reaches them from the
  store (see :mod:`ale.core.store`), which is what lets one image hold many tasks' data.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ale.core.ids import TaskId, content_hash

__all__ = [
    "HarnessFamily",
    "ImageRef",
    "NetworkMode",
    "NetworkPolicy",
    "PhaseTimeouts",
    "Resources",
    "SetupStage",
    "StageSpec",
    "TaskSpec",
    "ToolProvision",
    "ValidateSpec",
    "VerifyStage",
    "Workspace",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Workspace(StrEnum):
    """The fixed in-sandbox layout. Instructions reference these paths literally.

    Identical for every task, so no prompt and no script ever depends on a task's name,
    its domain, or where its folder happens to sit.
    """

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


class StageSpec(BaseModel):
    """What a stage needs beyond its own folder.

    Assets are named; their visibility (setup or verify) lives in the domain's asset
    lock, so answer data cannot be staged early by a task that asks for it in the wrong
    place. Kits are named too; their versions come from the kit lock.
    """

    model_config = _FROZEN

    assets: tuple[str, ...] = ()
    kits: tuple[str, ...] = ()


class SetupStage(StageSpec):
    """Preparation that runs before the agent."""

    prebakeable: bool = Field(
        default=False,
        description=(
            "True when this setup is deterministic and may be baked into an image "
            "ahead of time. A setup that mints a per-episode secret is not."
        ),
    )


class VerifyStage(StageSpec):
    """Scoring that runs after the agent, in a workspace the agent never saw."""


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
    """Egress policy.

    The gateway is reachable in every mode, and only the gateway holds real credentials.
    """

    model_config = _FROZEN

    mode: NetworkMode = NetworkMode.BLOCK
    allowed_hosts: tuple[str, ...] = ()

    # --- validation ---

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

    # --- validation ---

    @model_validator(mode="after")
    def _check_reason(self) -> Self:
        if self.mode == "manual" and not self.reason:
            raise ValueError("manual validation requires a reason")
        return self


class TaskSpec(BaseModel):
    """One task instance, fully specified."""

    model_config = _FROZEN

    id: TaskId
    domain: str = Field(description="Namespace that maps to a task repository")
    variant: str | None = Field(default=None, description="Named parameterisation, if any")

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
    setup: SetupStage = SetupStage()
    verify: VerifyStage = VerifyStage()
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
    def spec_hash(self) -> str:
        """``sha256:<hex>`` over the canonical form. The task instance's identity."""
        return content_hash(self.model_dump(mode="json", by_alias=True))

    @property
    def label(self) -> str:
        """Human-facing name: ``demo-hello`` or ``demo-hello@hard``.

        Display only. Nothing parses it back.
        """
        return f"{self.id}@{self.variant}" if self.variant else str(self.id)

    def needs_gui(self) -> bool:
        """Whether this task requires a desktop-capable sandbox."""
        return self.harness_family is HarnessFamily.POLICY or "desktop" in self.tools.mcp_servers
