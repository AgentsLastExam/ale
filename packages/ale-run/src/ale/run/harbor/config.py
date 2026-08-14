"""Supported subset of Harbor's single-step ``task.toml`` contract."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Literal, Self

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources

__all__ = [
    "HarborArtifactConfig",
    "HarborEnvironmentConfig",
    "HarborHealthcheckConfig",
    "HarborTaskConfig",
    "HarborVerifierConfig",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class HarborHealthcheckConfig(BaseModel):
    model_config = _FROZEN

    command: str = Field(min_length=1)
    interval_sec: float = Field(default=5, gt=0)
    timeout_sec: float = Field(default=30, gt=0)
    start_period_sec: float = Field(default=0, ge=0)
    start_interval_sec: float = Field(default=5, gt=0)
    retries: int = Field(default=3, ge=1)


class _NetworkConfig(BaseModel):
    model_config = _FROZEN

    network_mode: Literal["no-network", "public", "allowlist"] | None = None
    allowed_hosts: tuple[str, ...] | None = None

    def network_policy(self, fallback: NetworkPolicy | None = None) -> NetworkPolicy:
        if self.network_mode is None:
            return fallback or NetworkPolicy(mode=NetworkMode.OPEN)
        mode = {
            "no-network": NetworkMode.BLOCK,
            "public": NetworkMode.OPEN,
            "allowlist": NetworkMode.ALLOWLIST,
        }[self.network_mode]
        return NetworkPolicy(mode=mode, allowed_hosts=self.allowed_hosts or ())

    # --- validation ---

    @model_validator(mode="after")
    def _valid_network(self) -> Self:
        if self.network_mode == "allowlist" and not self.allowed_hosts:
            raise ValueError("network_mode='allowlist' requires allowed_hosts")
        if self.network_mode != "allowlist" and self.allowed_hosts:
            raise ValueError("allowed_hosts requires network_mode='allowlist'")
        return self


class HarborEnvironmentConfig(_NetworkConfig):
    build_timeout_sec: float = Field(default=600, gt=0)
    docker_image: str | None = Field(default=None, min_length=1)
    os: Literal["linux"] = "linux"
    cpus: int | None = Field(default=None, ge=1)
    memory_mb: int | None = Field(default=None, ge=128)
    storage_mb: int | None = Field(default=None, ge=256)
    gpus: int | None = Field(default=None, ge=0)
    gpu_types: None = None
    tpu: None = None
    mcp_servers: tuple[Any, ...] = Field(default=(), max_length=0)
    env: dict[str, str] = Field(default_factory=dict)
    skills_dir: None = None
    healthcheck: HarborHealthcheckConfig | None = None
    workdir: str | None = Field(default=None, min_length=1)
    allow_internet: bool | None = None

    def resources(self, fallback: Resources | None = None) -> Resources:
        base = fallback or Resources()
        return Resources(
            cpus=self.cpus or base.cpus,
            memory_mb=self.memory_mb or base.memory_mb,
            storage_mb=self.storage_mb if self.storage_mb is not None else base.storage_mb,
            gpus=self.gpus if self.gpus is not None else base.gpus,
            sudo=False,
        )

    # --- validation ---

    @model_validator(mode="before")
    @classmethod
    def _legacy_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for legacy, current in (("memory", "memory_mb"), ("storage", "storage_mb")):
            if legacy not in data:
                continue
            parsed = _size_mb(data.pop(legacy), legacy)
            if current in data and data[current] != parsed:
                raise ValueError(f"conflicting {legacy} and {current}")
            data[current] = parsed
        if data.get("allow_internet") is not None and "network_mode" not in data:
            data["network_mode"] = "public" if data["allow_internet"] else "no-network"
        return data

    @field_validator("docker_image")
    @classmethod
    def _nonblank_image(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("docker_image must not be blank")
        return value


class HarborAgentConfig(_NetworkConfig):
    timeout_sec: float | None = Field(default=None, gt=0)
    user: None = None


class HarborVerifierConfig(_NetworkConfig):
    timeout_sec: float = Field(default=600, gt=0)
    env: dict[str, str] = Field(default_factory=dict)
    user: None = None
    environment_mode: Literal["shared", "separate"] | None = None
    environment: HarborEnvironmentConfig | None = None
    collect: tuple[Any, ...] = Field(default=(), max_length=0)

    # --- validation ---

    @model_validator(mode="after")
    def _valid_environment(self) -> Self:
        if self.environment_mode == "shared" and self.environment is not None:
            raise ValueError("shared verifier cannot declare an environment")
        return self


class HarborSolutionConfig(BaseModel):
    model_config = _FROZEN

    env: dict[str, str] = Field(default_factory=dict)


class HarborArtifactConfig(BaseModel):
    model_config = _FROZEN

    source: str = Field(min_length=1)
    destination: str | None = None
    exclude: tuple[str, ...] = ()
    service: Literal["main"] | None = None

    # --- validation ---

    @field_validator("source")
    @classmethod
    def _safe_source(cls, value: str) -> str:
        if ".." in PurePosixPath(value).parts:
            raise ValueError("artifact source must not contain '..'")
        return value

    @field_validator("destination")
    @classmethod
    def _safe_destination(cls, value: str | None) -> str | None:
        if not value:
            return None
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("artifact destination must stay below the artifact directory")
        if value.rstrip("/") == "manifest.json":
            raise ValueError("artifact destination 'manifest.json' is reserved")
        return value


class HarborPackageInfo(BaseModel):
    model_config = _FROZEN

    name: str = Field(min_length=1)
    version: str | None = None
    description: str = ""
    authors: tuple[dict[str, str], ...] = ()
    keywords: tuple[str, ...] = ()


class HarborTaskConfig(BaseModel):
    model_config = _FROZEN

    version: str = Field(
        default="1.4",
        validation_alias=AliasChoices("version", "schema_version"),
    )
    task: HarborPackageInfo | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    verifier: HarborVerifierConfig = HarborVerifierConfig()
    agent: HarborAgentConfig = HarborAgentConfig()
    environment: HarborEnvironmentConfig = HarborEnvironmentConfig()
    solution: HarborSolutionConfig = HarborSolutionConfig()
    source: str | None = None
    multi_step_reward_strategy: None = None
    steps: None = None
    artifacts: tuple[str | HarborArtifactConfig, ...] = ()

    @property
    def normalized_artifacts(self) -> tuple[HarborArtifactConfig, ...]:
        return tuple(
            HarborArtifactConfig(source=item) if isinstance(item, str) else item
            for item in self.artifacts
        )


def _size_mb(value: object, field: str) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an integer MB value or a size like '2G'")
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMG])\s*", value, re.IGNORECASE)
    if match is None:
        raise ValueError(f"invalid {field} size {value!r}")
    number = float(match.group(1))
    unit = match.group(2).upper()
    return int(number * {"K": 1 / 1024, "M": 1, "G": 1024}[unit])
