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

import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ale.core.errors import ConfigError
from ale.core.ids import content_hash

__all__ = [
    "AgentConfig",
    "ArtifactPolicy",
    "GatewayLimits",
    "RunConfig",
    "load_run_config",
    "merge_layers",
    "parse_override",
]


class GatewayLimits(BaseModel):
    """Ceilings enforced by refusing the next model call.

    ``None`` means unlimited, which is a deliberate choice rather than a default: an
    unbounded run is occasionally right, but it should be written down.
    """

    model_config = ConfigDict(extra="forbid")

    max_turns: int | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=400_000, gt=0)
    max_cost_usd: float | None = Field(default=5.0, gt=0)


class ArtifactPolicy(BaseModel):
    """What happens to the paths a task declared as its output.

    Declaring an output and keeping a copy of it are separate decisions: the task knows
    which paths hold its result, and the operator knows whether this particular run
    wants them on disk. A large sweep that only needs scores discards them; a debugging
    run keeps everything. Remote destinations belong here too when they arrive.
    """

    model_config = ConfigDict(extra="forbid")

    collect: Literal["host", "none"] = "host"


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "claude-code"
    model: str = "claude-opus-4-8"
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
    kwargs: dict[str, Any] = Field(
        default_factory=dict, description="Harness-specific settings, validated by the harness"
    )


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialect: str = "anthropic"
    limits: GatewayLimits = GatewayLimits()
    base_url: str | None = Field(default=None, description="Upstream provider endpoint")


class RunConfig(BaseModel):
    """Everything one invocation needs, after all layers are merged."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "docker"
    work_dir: str = Field(
        default="/home/user/work",
        description=(
            "Scratch directory created in every sandbox. Inside the agent's home, so it "
            "is owned by the agent without anything having to grant that afterwards."
        ),
    )
    artifacts: ArtifactPolicy = ArtifactPolicy()
    agent: AgentConfig = AgentConfig()
    gateway: GatewayConfig = GatewayConfig()
    episodes: int = Field(default=1, ge=1)
    seed: int = 0
    resume: bool = True
    tasks_ref: str | None = Field(default=None, description="Override the registry pin")
    concurrency: int = Field(default=1, ge=1)

    @property
    def config_hash(self) -> str:
        """Identity of the effective settings, recorded in provenance."""
        return content_hash(self.model_dump(mode="json"))


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


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
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
    for override in overrides or []:
        path, value = parse_override(override)
        _assign(merged, path, value)
    return merged


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
    merged = merge_layers(
        preset=_read_toml(preset_path) if preset_path else None,
        run=_read_toml(run_path) if run_path else None,
        overrides=overrides,
    )
    try:
        return RunConfig.model_validate(merged)
    except Exception as exc:  # pydantic ValidationError, reported as a config problem
        raise ConfigError(f"invalid configuration: {exc}") from exc
