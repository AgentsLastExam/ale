"""Fast static checks for self-contained Task folders."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ale.core.errors import TaskDefinitionError
from ale.core.task import Stage
from ale.core.taskspec import OperatingSystem
from ale.run.tasksets.manifest import (
    ENVIRONMENT_DIR,
    INSTRUCTION,
    SETUP_DIR,
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
_VM_RESERVED_NAMES = {
    "ale-guestd.service",
    "autoinstall.yaml",
    "loader.conf",
    "mkosi.conf",
}
_TOP_LEVEL = {
    TASK_CONFIG,
    INSTRUCTION,
    ENVIRONMENT_DIR,
    Stage.TESTS,
    Stage.SOLUTION,
    "tools",
    ".ale-cache",
}


@dataclass(frozen=True)
class Finding:
    path: Path
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def lint_repository(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    source = path.expanduser().resolve()
    if not (source / TASK_CONFIG).is_file():
        for removed, replacement in (
            ("domain.yaml", "put the stable name in each Task's task.toml"),
            ("kits", "move Task-specific helpers below the owning Task"),
            ("images", "put image build materials below each Task's environment/ directory"),
            ("files", "put content below the Task stage that owns it"),
            ("skills", "move Task Skills below tools/skills/"),
            ("mcp", "move Task MCP below tools/mcp/"),
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
        load_tasks(source)
    except TaskDefinitionError as error:
        findings.append(Finding(source / TASK_CONFIG, str(error)))
    return findings


def _check_folder(folder: TaskFolder) -> list[Finding]:
    findings: list[Finding] = []
    for removed in ("files", "kits", "skills", "mcp"):
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

    config = _read_config(folder.root / TASK_CONFIG)
    environment = config.get("environment")
    try:
        operating_system = OperatingSystem(
            environment.get("os", "linux") if isinstance(environment, dict) else "linux"
        )
    except ValueError:
        operating_system = OperatingSystem.LINUX
    for stage in (Stage.TESTS, Stage.SOLUTION):
        entry = f"{stage}/{stage.entry(operating_system)}"
        if not (folder.root / entry).is_file():
            findings.append(Finding(folder.root, f"no {entry}"))
    if not (folder.root / INSTRUCTION).is_file():
        findings.append(Finding(folder.root, f"no {INSTRUCTION}"))

    builds_image = folder.dockerfile is not None or folder.build_script is not None
    if not builds_image and _has_files(folder.environment_dir / "assets"):
        findings.append(
            Finding(
                folder.environment_dir / "assets",
                "environment/assets is unused without an image build",
            )
        )
    ale = config.get("ale")
    vm = isinstance(ale, dict) and ale.get("sandbox") == "vm"
    if vm and folder.dockerfile is not None:
        # Any base image is accepted; the engine adds what it needs when it prepares one.
        if _vm_boot_override(folder.dockerfile):
            findings.append(
                Finding(folder.dockerfile, "VM final stage must not declare CMD or ENTRYPOINT")
            )
        for entry in folder.environment_dir.rglob("*"):
            in_context = entry.relative_to(folder.environment_dir).parts[0] != SETUP_DIR
            if (
                in_context
                and entry.is_file()
                and (entry.name in _VM_RESERVED_NAMES or entry.suffix in {".qcow2", ".raw"})
            ):
                findings.append(
                    Finding(entry, "VM boot and disk materialization files are ALE-owned")
                )

    setup = folder.stage_entry(Stage.SETUP, operating_system)
    if setup and _FIXED_SETUP.search(setup.read_text(encoding="utf-8", errors="replace")):
        image_build = (
            "environment/build.ps1"
            if operating_system is OperatingSystem.WINDOWS
            else "the image's Dockerfile"
        )
        findings.append(
            Finding(
                setup,
                f"fixed installation or download belongs in {image_build}; "
                "setup is for episode-dynamic initialization",
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


def _vm_boot_override(path: Path) -> bool:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    final_from = max(
        (index for index, line in enumerate(lines) if re.match(r"^\s*FROM\s+", line, re.I)),
        default=-1,
    )
    return any(re.match(r"^\s*(CMD|ENTRYPOINT)\b", line, re.I) for line in lines[final_from + 1 :])


def _has_files(path: Path) -> bool:
    return path.is_dir() and any(item.is_file() for item in path.rglob("*"))
