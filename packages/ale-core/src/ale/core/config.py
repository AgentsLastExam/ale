"""Layered configuration.

Files and flags must be interchangeable: anything settable in a config file is settable
on the command line and the other way round. Four layers, highest wins:

    command line  >  run config file  >  agent preset file  >  model defaults

Two rules keep this honest. Unknown keys are errors, never silent no-ops — a typo in a
budget ceiling must not read as "no ceiling". And the merged result is hashed into the
provenance record, so a run always carries the settings it actually used.

Override syntax is ``--set a.b.c=value`` with TOML scalar parsing, so
``--set gateway.limits.max_cost_usd=2.5`` is a float and ``--set agent.stream=true``
is a boolean.
"""

from __future__ import annotations

import os
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from ale.core.errors import ConfigError
from ale.core.ids import content_hash
from ale.core.taskspec import McpSource, SkillSource

__all__ = [
    "AgentConfig",
    "AgentJudgeConfig",
    "ArtifactPolicy",
    "AssetCollectionConfig",
    "GatewayLimits",
    "LLMJudgeConfig",
    "LoggingPolicy",
    "RunConfig",
    "SandboxRetentionConfig",
    "VerificationConfig",
    "ale_repo_path",
    "load_run_config",
    "merge_layers",
    "parse_override",
    "resolve_asset_collection",
    "select_agent_name",
]


class AssetCollectionConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    collection_slug: str = Field(min_length=1)
    source: Literal["cli", "environment"]


def ale_repo_path() -> Path:
    raw = os.environ.get("ALE_REPO_PATH", "")
    if not raw:
        raise ConfigError("ALE_REPO_PATH is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError("ALE_REPO_PATH must be absolute")
    resolved = path.resolve()
    if (
        not (resolved / "pyproject.toml").is_file()
        or not (resolved / "packages" / "ale-run").is_dir()
    ):
        raise ConfigError(f"ALE_REPO_PATH is not an ALE engine checkout: {resolved}")
    return resolved


def resolve_asset_collection(cli_value: str | None = None) -> AssetCollectionConfig:
    if cli_value and cli_value.strip():
        return AssetCollectionConfig(collection_slug=cli_value.strip(), source="cli")
    environment = os.environ.get("ALE_ASSETS_COLLECTION", "").strip()
    if environment:
        return AssetCollectionConfig(
            collection_slug=environment,
            source="environment",
        )
    raise ConfigError(
        "asset collection is required; pass --collection or set ALE_ASSETS_COLLECTION"
    )


class GatewayLimits(BaseModel):
    """Ceilings enforced by refusing the next model call.

    ``unlimited`` is explicit. Gateway limits never inherit native program defaults.
    """

    model_config = ConfigDict(extra="forbid")

    max_model_calls: Annotated[int, Field(gt=0, strict=True)] | Literal["unlimited"] = "unlimited"
    max_input_tokens: Annotated[int, Field(gt=0, strict=True)] | Literal["unlimited"] = "unlimited"
    max_output_tokens: Annotated[int, Field(gt=0, strict=True)] | Literal["unlimited"] = "unlimited"
    max_total_tokens: Annotated[int, Field(gt=0, strict=True)] | Literal["unlimited"] = "unlimited"
    max_cost_usd: Annotated[float, Field(gt=0, allow_inf_nan=False)] | Literal["unlimited"] = (
        "unlimited"
    )


class ArtifactPolicy(BaseModel):
    """What happens to the paths a task declared as its output.

    Declaring an output and keeping a copy of it are separate decisions: the task knows
    which paths hold its result, and the operator knows whether this particular run
    wants them on disk. A large sweep that only needs scores discards them; a debugging
    run keeps everything. Remote destinations belong here too when they arrive.
    """

    model_config = ConfigDict(extra="forbid")

    collect: Literal["host", "none"] = "host"


class LoggingPolicy(BaseModel):
    """Explicit retention choices; canonical evidence is never disabled."""

    model_config = ConfigDict(extra="forbid")

    native_logs: Literal["minimal", "debug"] = "minimal"
    transport_payloads: Literal["digests", "debug"] = "digests"
    token_data: Literal["none", "exact"] = "none"


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "claude-code"
    model: str = "claude-opus-4-8"
    authentication: Literal["auto", "api-key", "subscription"] = "auto"
    version: str | None = Field(
        default=None, description="Pin the agent build; None uses the image"
    )
    max_steps: int = Field(
        default=100,
        gt=0,
        description=(
            "Observe-act steps a stepwise agent may take in one episode. Here rather than "
            "with the gateway's ceilings because the gateway does not enforce it — the "
            "stepwise environment does, which is what makes it bind an agent that has "
            "never heard of it."
        ),
    )
    settings: dict[str, Any] = Field(
        default_factory=dict, description="Harness-specific settings, validated by the harness"
    )
    skills: tuple[SkillSource, ...] = ()
    mcp_servers: tuple[McpSource, ...] = ()


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialect: Literal[
        "anthropic",
        "openai-chat-completions",
        "openai-responses",
    ] = "anthropic"
    limits: GatewayLimits = GatewayLimits()
    base_url: str = Field(
        default="",
        description="Upstream endpoint. Empty selects the dialect's standard endpoint.",
    )
    api_key_env: str = Field(
        default="",
        description=(
            "Environment variable holding the upstream key. Empty selects the dialect's "
            "standard variable. Named rather than passed: a value on a command line is "
            "in shell history and in every process listing, a variable name is not."
        ),
    )


class LLMJudgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @field_validator("model", "reasoning_effort")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        return value.rstrip("/")


class AgentJudgeConfig(LLMJudgeConfig):
    adapter: Literal["codex-cli", "claude-code"]
    version: str = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def _exact_version(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) != 3 or any(not part.isdigit() for part in parts):
            raise ValueError("version must be an exact numeric semver such as 1.2.3")
        return value


class VerificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMJudgeConfig | None = None
    agent: AgentJudgeConfig | None = None


class SandboxRetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solver: Literal["destroy", "keep"] = "destroy"
    verifier: Literal["destroy", "keep"] = "destroy"


class ContainerProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["docker"] = "docker"
    gpus: tuple[Annotated[int, Field(ge=0, strict=True)], ...] = ()

    @field_validator("gpus")
    @classmethod
    def _gpu_indices(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if len(set(values)) != len(values):
            raise ValueError("Docker GPU indices must be unique")
        return values


class VmProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["qemu"] = "qemu"


class RunConfig(BaseModel):
    """Everything one invocation needs, after all layers are merged."""

    model_config = ConfigDict(extra="forbid")

    container: ContainerProviderConfig = ContainerProviderConfig()
    vm: VmProviderConfig = VmProviderConfig()
    artifacts: ArtifactPolicy = ArtifactPolicy()
    agent: AgentConfig = AgentConfig()
    gateway: GatewayConfig = GatewayConfig()
    verification: VerificationConfig = VerificationConfig()
    sandbox_retention: SandboxRetentionConfig = SandboxRetentionConfig()
    logging: LoggingPolicy = LoggingPolicy()
    episodes: int = Field(default=1, ge=1)
    seed: int = 0
    resume: bool = True
    tasks_ref: str | None = Field(default=None, description="Override the registry pin")
    concurrency: int = Field(default=1, ge=1)
    _preset_name: str | None = PrivateAttr(default=None)
    _preset_digest: str | None = PrivateAttr(default=None)
    _authentication_source: Literal["cli", "run", "preset", "default"] = PrivateAttr(
        default="default"
    )

    @property
    def config_hash(self) -> str:
        """Identity of the effective settings, recorded in provenance."""
        return content_hash(self.model_dump(mode="json"))

    @property
    def preset_name(self) -> str | None:
        return self._preset_name

    @property
    def preset_digest(self) -> str | None:
        return self._preset_digest

    @property
    def authentication_source(self) -> Literal["cli", "run", "preset", "default"]:
        return self._authentication_source


def parse_override(text: str) -> tuple[list[str], Any]:
    """Parse ``a.b.c=value`` into a key path and a TOML-typed value."""
    key, sep, raw = text.partition("=")
    if not sep or not key.strip():
        raise ConfigError(f"invalid override {text!r}: expected key.path=value")
    path = [part for part in key.strip().split(".") if part]
    if not path:
        raise ConfigError(f"invalid override {text!r}: empty key path")
    try:
        value = tomllib.loads(f"v = {raw.strip()}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw.strip()  # bare strings need no quoting
    return path, value


def _assign(target: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = target
    for part in path[:-1]:
        node = cursor.setdefault(part, {})
        if not isinstance(node, dict):
            raise ConfigError(f"cannot set {'.'.join(path)}: {part} is not a section")
        cursor = node
    cursor[path[-1]] = value


_ADDITIVE = {("agent", "skills"), ("agent", "mcp_servers")}


def _deep_merge(
    base: dict[str, Any], overlay: dict[str, Any], prefix: tuple[str, ...] = ()
) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value, (*prefix, key))
        elif (*prefix, key) in _ADDITIVE and isinstance(current, list) and isinstance(value, list):
            merged[key] = [*current, *value]
        else:
            merged[key] = value
    return merged


def _resolve_resource_paths(
    data: dict[str, Any], *, base: Path, origin: Literal["preset", "run", "cli"]
) -> dict[str, Any]:
    resolved = deepcopy(data)
    agent = resolved.get("agent")
    if not isinstance(agent, dict):
        return resolved
    for field in ("skills", "mcp_servers"):
        entries = agent.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            declared = entry.get("path") or entry.get("builtin")
            if path := entry.get("path"):
                candidate = Path(path)
                entry["path"] = str(candidate if candidate.is_absolute() else (base / candidate))
            entry["origin"] = origin
            entry["declared"] = str(declared)
    return resolved


def _read_toml(path: Path, *, origin: Literal["preset", "run"] | None = None) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
            return (
                _resolve_resource_paths(data, base=path.parent.resolve(), origin=origin)
                if origin
                else data
            )
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def merge_layers(
    *,
    preset: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
    overrides: list[str] | None = None,
) -> dict[str, Any]:
    """Merge configuration layers in precedence order (lowest first)."""
    merged: dict[str, Any] = {}
    for layer in (preset, run):
        if layer:
            merged = _deep_merge(merged, layer)
    override_layer: dict[str, Any] = {}
    for override in overrides or []:
        path, value = parse_override(override)
        _assign(override_layer, path, value)
    if override_layer:
        merged = _deep_merge(
            merged,
            _resolve_resource_paths(override_layer, base=Path.cwd(), origin="cli"),
        )
    return merged


def select_agent_name(*, run_path: Path | None = None, overrides: list[str] | None = None) -> str:
    """Resolve only the selector needed to choose an autonomous preset."""
    merged = merge_layers(
        run=_read_toml(run_path) if run_path else None,
        overrides=overrides,
    )
    agent = merged.get("agent")
    if isinstance(agent, dict) and isinstance(agent.get("name"), str):
        return agent["name"]
    return str(AgentConfig.model_fields["name"].default)


def load_run_config(
    *,
    preset_path: Path | None = None,
    run_path: Path | None = None,
    overrides: list[str] | None = None,
) -> RunConfig:
    """Build the effective configuration.

    Raises:
        ConfigError: on a missing file, malformed TOML, a bad override, or any key the
            schema does not define.
    """
    preset_data = _read_toml(preset_path, origin="preset") if preset_path else None
    run_data = _read_toml(run_path, origin="run") if run_path else None
    merged = merge_layers(preset=preset_data, run=run_data, overrides=overrides)
    try:
        config = RunConfig.model_validate(merged)
        if preset_path:
            config._preset_name = preset_path.stem
            config._preset_digest = content_hash(preset_path.read_text(encoding="utf-8"))
        for source, data in (("preset", preset_data), ("run", run_data)):
            agent = data.get("agent") if data else None
            if isinstance(agent, dict) and "authentication" in agent:
                config._authentication_source = source  # type: ignore[assignment]
        for override in overrides or ():
            path, _ = parse_override(override)
            if path == ["agent", "authentication"]:
                config._authentication_source = "cli"
        return config
    except Exception as exc:  # pydantic ValidationError, reported as a config problem
        raise ConfigError(f"invalid configuration: {exc}") from exc
