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
* Data placement is the task's decision. A task says where each asset lands and which
  paths to collect afterwards; the framework guarantees *when* things appear, not where.
  Fixing a global layout here would force every domain into one shape and buy nothing —
  answers stay away from an agent because verify-stage assets are materialised during
  scoring, whatever path they use.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ale.core.ids import TaskId, content_hash

__all__ = [
    "AssetMount",
    "ImageRef",
    "McpServer",
    "McpSource",
    "NetworkMode",
    "NetworkPolicy",
    "PhaseTimeouts",
    "Resources",
    "SetupStage",
    "SkillSource",
    "StageSpec",
    "StdioMcpServer",
    "StreamableHttpMcpServer",
    "TaskMcpSource",
    "TaskSpec",
    "ToolProvision",
    "VerifyStage",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


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


class AssetMount(BaseModel):
    """A directory of published data, and where this task wants it.

    Everything needed to find the data is here, so a task is readable on its own and no
    lookup table has to be kept in step with it. The revision is a commit: two runs that
    name the same one read the same bytes.

    Which stage lists a mount is what decides when it appears — gold answers listed under
    ``verify`` are simply not in the sandbox while the agent works.
    """

    model_config = _FROZEN

    repo: str = Field(description="Dataset repository, e.g. agents-last-exam/ale-tasks-assets")
    revision: str = Field(description="Commit to read; a branch name would not be reproducible")
    path: str = Field(description="Directory within the repository")
    dest: str = Field(description="Absolute path in the sandbox where it lands")


class StageSpec(BaseModel):
    """What a stage needs beyond its own folder."""

    model_config = _FROZEN

    assets: tuple[AssetMount, ...] = ()
    kits: tuple[str, ...] = ()


class SetupStage(StageSpec):
    """Preparation that runs before the agent."""


class VerifyStage(StageSpec):
    """Scoring that runs after the agent, in a workspace the agent never saw."""


class Resources(BaseModel):
    """How the sandbox must be provisioned.

    Single scalars: how a backend turns a request into a reservation or a hard limit is
    a runtime policy, not a property of the task.
    """

    model_config = _FROZEN

    cpus: int = Field(default=1, ge=1)
    memory_mb: int = Field(default=1024, ge=128)
    storage_mb: int | None = Field(default=None, ge=256)
    gpus: int = Field(default=0, ge=0)
    gpu_vram_gb: int | None = Field(default=None, ge=1)

    sudo: bool = Field(
        default=False,
        description=(
            "Whether the agent may elevate. Some tasks genuinely require installing "
            "software or changing system configuration, and the alternative to saying so "
            "is either giving every agent root or making those tasks impossible. Recorded "
            "in provenance, because an episode run this way was less isolated than one "
            "without — and a backend that cannot grant it refuses rather than running "
            "with less than was declared. Stated as a need, so one declaration serves any "
            "operating system."
        ),
    )


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


class SkillSource(BaseModel):
    """One local Skill or immediate collection of Skills."""

    model_config = _FROZEN

    path: str = Field(min_length=1)
    origin: Literal["task", "preset", "run", "cli"] | None = Field(default=None, exclude=True)
    declared: str | None = Field(default=None, exclude=True)


class McpSource(BaseModel):
    """A Run-level local descriptor or framework-owned built-in MCP server."""

    model_config = _FROZEN

    path: str | None = Field(default=None, min_length=1)
    builtin: str | None = Field(default=None, min_length=1)
    origin: Literal["task", "preset", "run", "cli"] | None = Field(default=None, exclude=True)
    declared: str | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if (self.path is None) == (self.builtin is None):
            raise ValueError("an MCP source requires exactly one of path or builtin")
        return self


class TaskMcpSource(BaseModel):
    """One task-owned MCP descriptor, relative to the task folder."""

    model_config = _FROZEN

    path: str = Field(min_length=1)


class StdioMcpServer(BaseModel):
    """A server the agent's MCP client starts inside the sandbox."""

    model_config = _FROZEN

    schema_version: Literal[1] = 1
    name: str = Field(min_length=1)
    transport: Literal["stdio"]
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    cwd: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _absolute_cwd(self) -> Self:
        if self.cwd is not None and not self.cwd.startswith("/"):
            raise ValueError("stdio MCP cwd must be an absolute sandbox path")
        return self


class StreamableHttpMcpServer(BaseModel):
    """An unauthenticated remote MCP endpoint."""

    model_config = _FROZEN

    schema_version: Literal[1] = 1
    name: str = Field(min_length=1)
    transport: Literal["streamable-http"]
    url: str = Field(pattern=r"^https?://")


McpServer = Annotated[
    StdioMcpServer | StreamableHttpMcpServer,
    Field(discriminator="transport"),
]


class ToolProvision(BaseModel):
    """Explicit agent resources required by this task."""

    model_config = _FROZEN

    skills: tuple[SkillSource, ...] = ()
    mcp_servers: tuple[TaskMcpSource, ...] = ()


class TaskSpec(BaseModel):
    """One task instance, fully specified."""

    model_config = _FROZEN

    id: TaskId
    domain: str = Field(description="Namespace that maps to a task repository")
    variant: str | None = Field(default=None, description="Named parameterisation, if any")

    spec_type: str = "core/v1"
    environment: str = "core/standard"

    instruction: str = Field(
        description="The rendered prompt: substitution already applied, paths literal"
    )
    image: ImageRef
    resources: Resources = Resources()
    network: NetworkPolicy = NetworkPolicy()
    timeouts: PhaseTimeouts = PhaseTimeouts()
    setup: SetupStage = SetupStage()
    verify: VerifyStage = VerifyStage()
    artifacts: tuple[str, ...] = Field(
        default=(),
        description="Absolute sandbox paths holding this task's output. What happens to "
        "them — kept on the host, discarded, uploaded — is a run-level decision, so it "
        "is not written here.",
    )
    tools: ToolProvision = ToolProvision()
    params: dict[str, Any] = Field(
        default_factory=dict, description="Values substituted into the instruction"
    )
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
