"""Strict public contracts for one self-contained ALE Task."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ale.core.ids import TaskId, content_hash

__all__ = [
    "ImageKind",
    "ImageSpec",
    "McpServer",
    "McpSource",
    "NetworkMode",
    "NetworkPolicy",
    "PhaseTimeoutOverride",
    "PhaseTimeouts",
    "ResourceOverride",
    "Resources",
    "SkillSource",
    "StdioMcpServer",
    "StreamableHttpMcpServer",
    "TaskManifestV1",
    "TaskMcpSource",
    "TaskSpec",
    "ToolProvision",
    "VariantOverride",
    "VerificationMode",
    "VerifierResources",
    "VerifySpec",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ImageKind(StrEnum):
    CONTAINER = "container"
    VM = "vm"


class ImageSpec(BaseModel):
    """Authored solver or dedicated-verifier image declaration."""

    model_config = _FROZEN

    kind: ImageKind
    ref: str | None = Field(default=None, min_length=1)

    @field_validator("ref")
    @classmethod
    def _nonblank_ref(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("image ref must not be blank")
        return value


class Resources(BaseModel):
    model_config = _FROZEN

    cpus: int = Field(default=1, ge=1)
    memory_mb: int = Field(default=1024, ge=128)
    storage_mb: int | None = Field(default=None, ge=256)
    gpus: int = Field(default=0, ge=0)
    sudo: bool = False


class ResourceOverride(BaseModel):
    model_config = _FROZEN

    cpus: int | None = Field(default=None, ge=1)
    memory_mb: int | None = Field(default=None, ge=128)
    storage_mb: int | None = Field(default=None, ge=256)
    gpus: int | None = Field(default=None, ge=0)
    sudo: bool | None = None


class NetworkMode(StrEnum):
    BLOCK = "block"
    ALLOWLIST = "allowlist"
    OPEN = "open"


class NetworkPolicy(BaseModel):
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
    model_config = _FROZEN

    setup: float = Field(default=120, gt=0)
    agent: float = Field(default=900, gt=0)
    verify: float = Field(default=300, gt=0)

    @property
    def total(self) -> float:
        return self.setup + self.agent + self.verify


class PhaseTimeoutOverride(BaseModel):
    model_config = _FROZEN

    setup: float | None = Field(default=None, gt=0)
    agent: float | None = Field(default=None, gt=0)
    verify: float | None = Field(default=None, gt=0)


class SkillSource(BaseModel):
    model_config = _FROZEN

    path: str = Field(min_length=1)
    origin: Literal["task", "preset", "run", "cli"] | None = Field(default=None, exclude=True)
    declared: str | None = Field(default=None, exclude=True)


class McpSource(BaseModel):
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
    model_config = _FROZEN

    path: str = Field(min_length=1)


class StdioMcpServer(BaseModel):
    model_config = _FROZEN

    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    transport: Literal["stdio"]
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    cwd: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _absolute_cwd(self) -> Self:
        if self.cwd is not None and not (
            self.cwd.startswith("/") or self.cwd == "{mcp}" or self.cwd.startswith("{mcp}/")
        ):
            raise ValueError("stdio MCP cwd must be an absolute sandbox path")
        return self


class StreamableHttpMcpServer(BaseModel):
    model_config = _FROZEN

    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    transport: Literal["streamable-http"]
    url: str = Field(pattern=r"^https?://")


McpServer = Annotated[
    StdioMcpServer | StreamableHttpMcpServer,
    Field(discriminator="transport"),
]


class ToolProvision(BaseModel):
    model_config = _FROZEN

    skills: tuple[SkillSource, ...] = ()
    mcp_servers: tuple[TaskMcpSource, ...] = ()


class VerificationMode(StrEnum):
    SHARED = "shared"
    SEPARATE = "separate"


class VerifierResources(BaseModel):
    """Explicit resources for a physically separate verifier sandbox."""

    model_config = _FROZEN

    cpus: int = Field(ge=1)
    memory_mb: int = Field(ge=128)
    storage_mb: int | None = Field(ge=256)
    gpus: int = Field(ge=0)

    def as_resources(self) -> Resources:
        return Resources(
            cpus=self.cpus,
            memory_mb=self.memory_mb,
            storage_mb=self.storage_mb,
            gpus=self.gpus,
            sudo=False,
        )


class VerifySpec(BaseModel):
    model_config = _FROZEN

    environment_mode: VerificationMode = VerificationMode.SHARED
    image: ImageSpec | None = None
    resources: VerifierResources | None = None

    @model_validator(mode="after")
    def _valid_topology(self) -> Self:
        if self.environment_mode is VerificationMode.SHARED:
            if self.image is not None or self.resources is not None:
                raise ValueError("shared verification cannot declare image or resources")
        elif self.resources is None:
            raise ValueError("separate verification requires explicit resources")
        return self


class VariantOverride(BaseModel):
    model_config = _FROZEN

    name: TaskId
    params: dict[str, Any] = Field(default_factory=dict)
    resources: ResourceOverride = ResourceOverride()
    timeouts: PhaseTimeoutOverride = PhaseTimeoutOverride()

    @field_validator("name")
    @classmethod
    def _not_base(cls, value: TaskId) -> TaskId:
        if value == "base":
            raise ValueError("variant name 'base' is reserved for the top-level Task")
        return value


class TaskManifestV1(BaseModel):
    """The strict authored shape of ``task.yaml``."""

    model_config = _FROZEN

    spec_type: Literal["core/v1"]
    name: TaskId
    environment: Literal["core/standard"] = "core/standard"
    image: ImageSpec
    resources: Resources = Resources()
    network: NetworkPolicy = NetworkPolicy()
    timeouts: PhaseTimeouts = PhaseTimeouts()
    artifacts: tuple[str, ...] = ()
    tools: ToolProvision = ToolProvision()
    params: dict[str, Any] = Field(default_factory=dict)
    verify: VerifySpec = VerifySpec()
    variants: tuple[VariantOverride, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    extras: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("artifacts")
    @classmethod
    def _absolute_artifacts(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        relative = [path for path in paths if not PurePosixPath(path).is_absolute()]
        if relative:
            raise ValueError("artifact paths must be absolute: " + ", ".join(relative))
        normalized = [PurePosixPath(path) for path in paths]
        if len(normalized) != len(set(normalized)):
            raise ValueError("artifact paths must be unique")
        for index, path in enumerate(normalized):
            for other in normalized[index + 1 :]:
                if path in other.parents or other in path.parents:
                    raise ValueError(f"artifact paths overlap: {path} and {other}")
        return paths

    @model_validator(mode="after")
    def _valid_manifest(self) -> Self:
        names = [variant.name for variant in self.variants]
        if len(names) != len(set(names)):
            raise ValueError("variant names must be unique")
        return self


class TaskSpec(BaseModel):
    """One rendered base or variant instance used by the runtime."""

    model_config = _FROZEN

    spec_type: Literal["core/v1"] = "core/v1"
    name: TaskId
    variant: str = Field(default="base", min_length=1)
    environment: Literal["core/standard"] = "core/standard"
    image: ImageSpec
    instruction: str
    resources: Resources = Resources()
    network: NetworkPolicy = NetworkPolicy()
    timeouts: PhaseTimeouts = PhaseTimeouts()
    artifacts: tuple[str, ...] = ()
    tools: ToolProvision = ToolProvision()
    params: dict[str, Any] = Field(default_factory=dict)
    verify: VerifySpec = VerifySpec()
    metadata: dict[str, Any] = Field(default_factory=dict)
    extras: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @property
    def id(self) -> TaskId:
        """Internal compatibility name; the authored/public field is ``name``."""
        return self.name

    @property
    def spec_hash(self) -> str:
        return content_hash(self.model_dump(mode="json"))

    @property
    def label(self) -> str:
        return str(self.name) if self.variant == "base" else f"{self.name}@{self.variant}"
