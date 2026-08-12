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
from ale.core.result import SandboxOutcome
from ale.core.sandbox import ResourceAllocation
from ale.core.taskspec import ImageKind, ImageSpec, Resources, VerificationMode

__all__ = [
    "AgentProvenance",
    "AgentResourceProvenance",
    "AleVerifyProvenance",
    "AssetProvenance",
    "AuthenticationProvenance",
    "FrameworkProvenance",
    "GatewayProvenance",
    "HarnessPresetProvenance",
    "ImageProvenance",
    "JudgeProvenance",
    "LimitTermination",
    "ResourceProvenance",
    "RunLock",
    "SandboxProvenance",
    "TaskProvenance",
    "TaskSource",
    "VerificationProvenance",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")

SCHEMA_VERSION = 2


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

    name: TaskId
    variant: str
    spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source: TaskSource


class ImageProvenance(BaseModel):
    model_config = _FROZEN

    declaration: ImageSpec
    source: Literal["local", "ref"]
    input_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    image_source_identity: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    prepared_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_ref: str = Field(min_length=1)
    oci_identity: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    base_materials: tuple[str, ...] = ()
    resolved_reference: str | None = None
    materializer_identity: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    provider: str
    observed_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observed_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_source_shape(self) -> Self:
        local = self.source == "local"
        if local != (self.image_source_identity is not None):
            raise ValueError("local image provenance requires image_source_identity")
        if local == (self.resolved_reference is not None):
            raise ValueError("ref image provenance requires resolved_reference")
        if self.declaration.kind is ImageKind.VM and local:
            if self.oci_identity is None or self.materializer_identity is None:
                raise ValueError("local VM provenance requires OCI and materializer identities")
        elif self.oci_identity is not None or self.materializer_identity is not None:
            raise ValueError("only local VM provenance has OCI and materializer identities")
        return self


class ResourceProvenance(BaseModel):
    model_config = _FROZEN

    requested: Resources
    effective: ResourceAllocation


class AuthenticationProvenance(BaseModel):
    model_config = _FROZEN

    requested: Literal["auto", "api-key", "subscription"] = "api-key"
    effective: Literal["api-key", "subscription"] = "api-key"
    selection_source: Literal["cli", "run", "preset", "default"] = "default"
    provider: Literal["anthropic", "openai", "xai"] | None = None
    profile_slot_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    transport: Literal["gateway", "native-proxy", "subscription-relay"] = "gateway"
    credential_exposed_to_agent: bool = False
    gateway_observability: Literal["available", "unavailable"] = "available"
    validated_cli_version: str = ""


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
    authentication: AuthenticationProvenance = AuthenticationProvenance()


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
    """One observed judge execution; a change moves the resulting scores."""

    model_config = _FROZEN

    invocation_id: str
    kind: Literal["llm", "agent"]
    model: str
    reasoning_effort: str
    endpoint_identity: str
    prompt_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rubric_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    adapter: Literal["codex-cli", "claude-code"] | None = None
    adapter_version: str | None = None
    placement: Literal["verify"] = "verify"
    attempts: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _legacy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "invocation_id" in value:
            return value
        prompt_hash = value.get("prompt_hash")
        return {
            **value,
            "invocation_id": "legacy",
            "kind": "llm",
            "reasoning_effort": "unknown",
            "endpoint_identity": "unknown",
            "rubric_hash": prompt_hash,
        }


class AssetProvenance(BaseModel):
    model_config = _FROZEN

    repository: str
    task_path: str
    commit: str | None = None
    dirty: bool


class VerificationProvenance(BaseModel):
    model_config = _FROZEN

    mode: VerificationMode = VerificationMode.SHARED
    image: ImageProvenance | None = None
    resources: ResourceProvenance | None = None


class AleVerifyProvenance(BaseModel):
    model_config = _FROZEN

    version: str
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
    observability: Literal["available", "unavailable"] = "available"


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
    schema_version: Literal[2] = SCHEMA_VERSION


class RunLock(BaseModel):
    """The complete provenance of one episode."""

    model_config = _FROZEN

    task: TaskProvenance
    image: ImageProvenance
    resources: ResourceProvenance | None = None
    agent: AgentProvenance
    framework: FrameworkProvenance
    gateway: GatewayProvenance
    trajectory_schema: Literal["ATIF-v1.7"] = "ATIF-v1.7"
    result_schema_version: Literal[2] = 2
    transport_schema_version: Literal[1] = 1
    execution_schema_version: Literal[1] = 1
    sandbox: SandboxProvenance | None = None
    config_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    seed: int
    ale_verify: AleVerifyProvenance | None = None
    judges: tuple[JudgeProvenance, ...] = ()
    asset: AssetProvenance | None = None
    verification: VerificationProvenance = VerificationProvenance()
    sandbox_outcomes: tuple[SandboxOutcome, ...] = ()
    termination: LimitTermination | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_judge(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "judge" in migrated:
            judge = migrated.pop("judge")
            if judge is not None and not migrated.get("judges"):
                migrated["judges"] = [judge]
        if "assets" in migrated:
            legacy = migrated.pop("assets") or []
            if legacy and not migrated.get("asset"):
                first = legacy[0]
                migrated["asset"] = {
                    "repository": first.get("repository", ""),
                    "task_path": first.get("task_path", ""),
                    "commit": first.get("commit"),
                    "dirty": any(item.get("state") != "clean" for item in legacy),
                }
        migrated.setdefault("verification", {"mode": "shared"})
        migrated.setdefault("sandbox_outcomes", [])
        return migrated

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
        if self.resources is None:
            problems.append("effective resource allocation was not recorded")
        if self.asset is not None and (self.asset.dirty or self.asset.commit is None):
            problems.append("Task assets are dirty or have no synchronized commit")
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
