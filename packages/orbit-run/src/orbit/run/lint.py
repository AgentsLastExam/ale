"""Fast static checks for self-contained Task folders."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ale.core.errors import TaskDefinitionError
from ale.core.task import CALIBRATION_DIR, SOLUTION_DIR, Stage, case_entry, case_path
from ale.core.taskspec import (
    SOLUTION_CASE,
    OperatingSystem,
    VariableReference,
    is_credential_name,
)
from ale.run.tasksets.manifest import (
    BUILD_SCRIPTS,
    ENVIRONMENT_DIR,
    INSTRUCTION,
    TASK_CONFIG,
    TaskFolder,
    discover_task_folders,
    load_tasks,
)

__all__ = ["Finding", "lint_repository"]

_FIXED_SETUP = re.compile(
    r"\b(apt-get|apt |dnf |yum |pip3? install|curl |wget |winget install|choco install|"
    r"Invoke-WebRequest|Invoke-RestMethod)",
    re.IGNORECASE,
)
_TOP_LEVEL = {
    TASK_CONFIG,
    INSTRUCTION,
    ENVIRONMENT_DIR,
    Stage.TESTS,
    Stage.VALIDATION,
    SOLUTION_DIR,
    CALIBRATION_DIR,
    ".ale-cache",
}
#: Harbor's way to run sidecars. ALE has one machine per role, so these would silently do nothing.
_COMPOSE_FILE = "docker-compose.yaml"


@dataclass(frozen=True)
class Finding:
    """A problem, which fails ``ale lint``, or a warning: what the Task leaves to a default
    ALE then chooses, or lets change under it, which never fails it."""

    path: Path
    message: str
    severity: Literal["error", "warning"] = "error"

    def __str__(self) -> str:
        warning = "warning: " if self.severity == "warning" else ""
        return f"{self.path}: {warning}{self.message}"


def lint_repository(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    source = path.expanduser().resolve()
    if not (source / TASK_CONFIG).is_file():
        for removed, replacement in (
            ("domain.yaml", "put the stable name in each Task's task.toml"),
            ("kits", "move Task-specific helpers below the owning Task"),
            ("images", "put image build materials below each Task's environment/ directory"),
            ("files", "put content below the Task stage that owns it"),
            ("skills", "build Task Skills into the image and set [environment].skills_dir"),
            ("mcp", "build Task MCP servers into the image and list [[environment.mcp_servers]]"),
        ):
            candidate = source / removed
            if candidate.exists():
                findings.append(Finding(candidate, f"removed concept; {replacement}"))

    try:
        folders = discover_task_folders(source)
    except TaskDefinitionError as error:
        return [*findings, Finding(source, str(error))]

    for folder in folders:
        findings.extend(_check_folder(folder))
    try:
        tasks = load_tasks(source)
    except TaskDefinitionError as error:
        findings.append(Finding(source / TASK_CONFIG, str(error)))
    else:
        findings.extend(
            Finding(task.folder.root, warning, "warning")
            for task in tasks
            for warning in task.warnings
        )
    return findings


def _check_folder(folder: TaskFolder) -> list[Finding]:
    findings: list[Finding] = []
    for removed in ("files", "kits", "skills", "mcp", "tools"):
        candidate = folder.root / removed
        if candidate.exists():
            findings.append(
                Finding(
                    candidate,
                    f"top-level {removed}/ is removed; place content in its owning Task stage",
                )
            )

    for entry in folder.root.iterdir():
        if entry.name not in _TOP_LEVEL:
            findings.append(
                Finding(entry, "unsupported top-level Task entry; move it into its owning stage")
            )
    calibration = folder.root / CALIBRATION_DIR
    if calibration.is_symlink() or (calibration.exists() and not calibration.is_dir()):
        findings.append(Finding(calibration, "calibration must be a directory, not a link or file"))
    elif calibration.is_dir():
        for entry in calibration.iterdir():
            if entry.name == SOLUTION_CASE:
                findings.append(
                    Finding(
                        entry, f"the reference solution lives in {SOLUTION_DIR}/; move it there"
                    )
                )
            elif entry.is_symlink() or not entry.is_dir():
                findings.append(
                    Finding(
                        entry, "calibration/ holds only case directories; move it into its case"
                    )
                )

    for directory in (folder.environment_dir, folder.root / Stage.TESTS):
        if (directory / _COMPOSE_FILE).is_file():
            findings.append(
                Finding(
                    directory / _COMPOSE_FILE,
                    "compose sidecars are not supported; run services in the Task's own image",
                )
            )

    config = _read_config(folder.root / TASK_CONFIG)
    environment = config.get("environment")
    try:
        operating_system = OperatingSystem(
            environment.get("os", "linux") if isinstance(environment, dict) else "linux"
        )
    except ValueError:
        operating_system = OperatingSystem.LINUX
    for entry in (
        f"{Stage.TESTS}/{Stage.TESTS.entry(operating_system)}",
        f"{case_path(SOLUTION_CASE)}/{case_entry(operating_system)}",
    ):
        if not (folder.root / entry).is_file():
            findings.append(Finding(folder.root, f"no {entry}"))
    if not (folder.root / INSTRUCTION).is_file():
        findings.append(Finding(folder.root, f"no {INSTRUCTION}"))

    builds_image = (
        folder.dockerfile is not None or folder.build_script(operating_system) is not None
    )
    if not builds_image and _has_files(folder.environment_dir / "assets"):
        findings.append(
            Finding(
                folder.environment_dir / "assets",
                "environment/assets is unused without an image build",
            )
        )

    setup = folder.stage_entry(Stage.SETUP, operating_system)
    if setup and _FIXED_SETUP.search(setup.read_text(encoding="utf-8", errors="replace")):
        # A Linux Task builds a VM with build.sh only when it has no Dockerfile.
        dockerfile = operating_system is OperatingSystem.LINUX and (
            folder.dockerfile is not None or folder.build_script(operating_system) is None
        )
        script = BUILD_SCRIPTS[operating_system]
        image_build = "the image's Dockerfile" if dockerfile else f"environment/{script}"
        findings.append(
            Finding(
                setup,
                f"fixed installation or download belongs in {image_build}; "
                "setup is for episode-dynamic initialization",
            )
        )
    findings.extend(_check_variables(folder.root / TASK_CONFIG, config))
    metadata = config.get("metadata")
    # A blocklist gives the agent nearly what public does: every host but a few, mirrors of
    # those included, so a reviewer needs its reason as well.
    if (mode := _agent_network_mode(config)) in ("public", "blocklist") and not (
        isinstance(metadata, dict) and metadata.get("network_justification")
    ):
        findings.append(
            Finding(
                folder.root / TASK_CONFIG,
                f"network_mode {mode!r} needs a reason in [metadata].network_justification",
            )
        )
    return findings


def _agent_network_mode(config: dict[str, object]) -> object:
    """The agent's network mode as Harbor resolves it: ``[agent]``'s, else ``[environment]``'s
    baseline, ``allow_internet`` included."""
    agent, environment = config.get("agent"), config.get("environment")
    for table in (agent, environment):
        if isinstance(table, dict) and table.get("network_mode") is not None:
            return table["network_mode"]
    if isinstance(environment, dict) and environment.get("allow_internet") is True:
        return "public"
    return None


def _check_variables(path: Path, config: dict[str, object]) -> list[Finding]:
    """A Task's ``env`` tables: no credential is written down, and no value misleads."""
    tables: list[tuple[str, object]] = []
    for table in ("environment", "verifier", "solution"):
        body = config.get(table)
        if isinstance(body, dict):
            tables.append((table, body.get("env")))
            if table != "verifier":
                continue
            validation = body.get("validation")
            for name, owner in (("verifier", body), ("verifier.validation", validation)):
                if not isinstance(owner, dict):
                    continue
                if owner is validation:
                    tables.append((name, owner.get("env")))
                nested = owner.get("environment")
                if isinstance(nested, dict):
                    tables.append((f"{name}.environment", nested.get("env")))
    findings: list[Finding] = []
    for table, env in tables:
        for key, value in env.items() if isinstance(env, dict) else ():
            if not isinstance(value, str):
                continue
            reference = VariableReference.parse(value)
            credential = is_credential_name(key) or (
                reference is not None and is_credential_name(reference.name)
            )
            # A default is written down as much as a literal is.
            if credential and (reference is None or reference.default):
                findings.append(
                    Finding(
                        path,
                        f"[{table}].env {key} holds a credential; write ${{NAME}} with no "
                        "default and set NAME in the checkout's .env",
                    )
                )
            elif reference is None and "${" in value:
                findings.append(
                    Finding(
                        path,
                        f"[{table}].env {key}: only a whole value ${{NAME}} is resolved; "
                        "a ${...} inside other text stays as written",
                    )
                )
    return findings


def _read_config(path: Path) -> dict[str, object]:
    """The configuration as far as it parses; loading reports what is wrong with it."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _has_files(path: Path) -> bool:
    return path.is_dir() and any(item.is_file() for item in path.rglob("*"))
