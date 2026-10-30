from __future__ import annotations

import copy
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ale.core.config import RunConfig
from ale.core.sandbox import ImageRef, PreparedTaskImage, Provider
from ale.core.taskspec import ImageKind
from ale.run import ale_image
from ale.run.providers import ProviderRegistry

ENGINE_ROOT = Path(__file__).resolve().parents[1]
#: The ALE bases of this engine version, for images a test builds or runs.
CONTAINER_BASE = ale_image("container-ubuntu22-base")
VM_BASE = ale_image("vm-ubuntu24-base")


def sibling_checkout(name: str) -> Path:
    """Locate a sibling repository from main or any linked worktree."""
    return next(
        (parent / name for parent in ENGINE_ROOT.parents if (parent / name).is_dir()),
        ENGINE_ROOT.parent / name,
    )


def provider_registry(
    provider: Provider | None = None,
    *,
    kind: ImageKind = ImageKind.CONTAINER,
) -> ProviderRegistry:
    registry = ProviderRegistry(RunConfig())
    if provider is not None:
        registry._instances[kind] = provider
    return registry


async def prepare_reference(
    provider: Provider,
    reference: str,
    *,
    kind: ImageKind = ImageKind.CONTAINER,
) -> PreparedTaskImage:
    return await provider.prepare_image(ImageRef(kind=kind, reference=reference))


#: The smallest valid ``task.toml``: a container Task with no network.
MINIMAL_TASK_CONFIG: dict[str, Any] = {
    "schema_version": "1.4+ale.1",
    "task": {"name": "agentslastexam/demo"},
    "environment": {"cpus": 1, "memory_mb": 512},
    "agent": {"user": "user", "timeout_sec": 120, "network_mode": "no-network"},
    "verifier": {"timeout_sec": 120},
}
_EMPTY_STAGE = "#!/usr/bin/env bash\nset -euo pipefail\n"


def task_toml(config: Mapping[str, Any]) -> str:
    """Render ``config`` as TOML: scalars and arrays first, then each table under a header."""

    def value(item: Any) -> str:
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, int | float):
            return repr(item)
        if isinstance(item, str):
            return json.dumps(item)
        if isinstance(item, Mapping):
            return "{" + ", ".join(f"{key} = {value(entry)}" for key, entry in item.items()) + "}"
        return "[" + ", ".join(value(entry) for entry in item) + "]"

    def table(prefix: tuple[str, ...], body: Mapping[str, Any]) -> list[str]:
        lines = [f"[{'.'.join(prefix)}]"] if prefix else []
        lines += [
            f"{key} = {value(entry)}"
            for key, entry in body.items()
            if entry is not None and not isinstance(entry, Mapping)
        ]
        for key, entry in body.items():
            if isinstance(entry, Mapping):
                lines += ["", *table((*prefix, key), entry)]
        return lines

    return "\n".join(table((), config)) + "\n"


def merged_config(*layers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deep-merge ``task.toml`` layers; a ``None`` value removes the key."""
    result: dict[str, Any] = {}
    for layer in layers:
        for key, entry in (layer or {}).items():
            if entry is None:
                result.pop(key, None)
            elif isinstance(entry, Mapping) and isinstance(result.get(key), Mapping):
                result[key] = merged_config(result[key], entry)
            else:
                result[key] = copy.deepcopy(entry)
    return result


def write_task(
    task: Path,
    *,
    name: str = "agentslastexam/demo",
    config: Mapping[str, Any] | None = None,
    dockerfile: str | None = f"FROM {CONTAINER_BASE}\n",
    instruction: str = "Do the task.\n",
    test: str | None = _EMPTY_STAGE,
    solve: str | None = _EMPTY_STAGE,
) -> Path:
    """Write one complete Task folder: the minimal configuration with ``config`` merged in.

    ``dockerfile`` is ``environment/Dockerfile``; ``None`` writes none.
    """
    full = merged_config(MINIMAL_TASK_CONFIG, {"task": {"name": name}}, config)
    task.mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text(task_toml(full), encoding="utf-8")
    (task / "instruction.md").write_text(instruction, encoding="utf-8")
    if dockerfile is not None:
        target = task / "environment" / "Dockerfile"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dockerfile, encoding="utf-8")
    stages = {"tests/test.sh": test, "solution/solve.sh": solve}
    for relative, body in stages.items():
        if body is not None:
            entry = task / relative
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(body, encoding="utf-8")
            entry.chmod(0o755)
    return task


def edit_task_config(task: Path, layer: Mapping[str, Any]) -> None:
    """Merge ``layer`` into a Task folder's ``task.toml``; a ``None`` value removes a key."""
    path = task / "task.toml"
    path.write_text(task_toml(merged_config(tomllib.loads(path.read_text()), layer)))
