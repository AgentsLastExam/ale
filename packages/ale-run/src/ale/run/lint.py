"""Fast static checks for self-contained Task folders."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ale.core.errors import TaskDefinitionError
from ale.core.taskspec import OperatingSystem
from ale.run.tasksets.manifest import (
    INSTRUCTION,
    STAGE_ENTRY,
    TASK_MANIFEST,
    TaskFolder,
    discover_task_folders,
    load_tasks,
)

__all__ = ["Finding", "lint_repository"]

_FIXED_SETUP = re.compile(r"\b(apt-get|apt |dnf |yum |pip3? install|curl |wget )")
_CONTAINER_BASE = re.compile(r"^ghcr\.io/agentslastexam/container-ubuntu22-base:[^ ]+$")
_VM_BASE = re.compile(r"^ghcr\.io/agentslastexam/vm-ubuntu24-base:(?!latest$)[^ ]+$")
_VM_RESERVED_NAMES = {
    "ale-guestd.service",
    "autoinstall.yaml",
    "loader.conf",
    "mkosi.conf",
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
    if not (source / TASK_MANIFEST).is_file():
        for removed, replacement in (
            ("domain.yaml", "put the stable name in each Task's task.yaml"),
            ("kits", "move Task-specific helpers below the owning Task"),
            ("images", "put each Dockerfile below its Task image/ directory"),
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
        findings.append(Finding(source / TASK_MANIFEST, str(error)))
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

    allowed = {
        "task.yaml",
        "instruction.md",
        "image",
        "setup",
        "verify",
        "oracle",
        "tools",
        ".ale-cache",
    }
    for entry in folder.root.iterdir():
        if entry.name not in allowed:
            findings.append(
                Finding(entry, "unsupported top-level Task entry; move it into its owning stage")
            )

    manifest: dict[str, object] = {}
    manifest_path = folder.root / TASK_MANIFEST
    if manifest_path.is_file():
        try:
            loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            loaded = {}
        manifest = loaded if isinstance(loaded, dict) else {}
        if isinstance(manifest, dict):
            for field in ("image", "setup", "verify"):
                section = manifest.get(field)
                if isinstance(section, dict) and "assets" in section:
                    findings.append(
                        Finding(
                            manifest_path,
                            f"{field}.assets asset declarations are removed; "
                            "place canonical bytes below "
                            f"{field}/assets/",
                        )
                    )

    try:
        operating_system = OperatingSystem(str(manifest.get("os", "linux")))
    except ValueError:
        operating_system = OperatingSystem.LINUX
    stage_entry = "run.ps1" if operating_system is OperatingSystem.WINDOWS else STAGE_ENTRY
    required = {
        INSTRUCTION: folder.root / INSTRUCTION,
        f"verify/{stage_entry}": folder.root / "verify" / stage_entry,
        f"oracle/{stage_entry}": folder.root / "oracle" / stage_entry,
    }
    for label, file in required.items():
        if not file.is_file():
            findings.append(Finding(folder.root, f"no {label}"))

    for stage in ("setup", "verify", "oracle"):
        entry = folder.stage_entry(stage, operating_system)
        if (
            operating_system is OperatingSystem.LINUX
            and entry is not None
            and not _is_executable(entry)
        ):
            findings.append(Finding(entry, "not executable: chmod +x it"))

    dockerfile = folder.image_dir / "Dockerfile"
    image = manifest.get("image")
    kind = image.get("kind") if isinstance(image, dict) else None
    ref = image.get("ref") if isinstance(image, dict) else None
    if dockerfile.is_file():
        final = _final_from(dockerfile)
        expected = _VM_BASE if kind == "vm" else _CONTAINER_BASE
        if final is None or not expected.fullmatch(final):
            label = "vm-ubuntu24-base" if kind == "vm" else "container-ubuntu22-base"
            findings.append(
                Finding(
                    dockerfile,
                    f"final stage for declared {kind or 'unknown'} kind must derive directly "
                    f"from an ALE {label} image",
                )
            )
        if kind == "vm" and _vm_boot_override(dockerfile):
            findings.append(
                Finding(dockerfile, "VM final stage must not declare CMD or ENTRYPOINT")
            )
        if kind == "vm":
            for entry in folder.image_dir.rglob("*"):
                if entry.is_file() and (
                    entry.name in _VM_RESERVED_NAMES or entry.suffix in {".qcow2", ".raw"}
                ):
                    findings.append(
                        Finding(entry, "VM boot and disk materialization files are ALE-owned")
                    )
    elif ref is None:
        findings.append(Finding(folder.root, "no image/Dockerfile and no image.ref"))
    elif _has_files(folder.image_dir / "assets"):
        findings.append(
            Finding(folder.image_dir / "assets", "image/assets is unused for ref-only images")
        )

    verify = folder.root / "verify"
    if (verify / "check.py").exists() and not (verify / "verify.py").exists():
        findings.append(Finding(verify / "check.py", "rename the default verifier to verify.py"))

    setup = folder.stage_entry("setup", operating_system)
    if setup and _FIXED_SETUP.search(setup.read_text(encoding="utf-8", errors="replace")):
        findings.append(
            Finding(
                setup,
                "fixed installation or download belongs in image/Dockerfile; "
                "setup is for episode-dynamic initialization",
            )
        )
    return findings


def _final_from(path: Path) -> str | None:
    images: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^\s*FROM\s+([^\s]+)", line, flags=re.IGNORECASE)
        if match:
            images.append(match.group(1))
    return images[-1] if images else None


def _vm_boot_override(path: Path) -> bool:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    final_from = max(
        (index for index, line in enumerate(lines) if re.match(r"^\s*FROM\s+", line, re.I)),
        default=-1,
    )
    return any(re.match(r"^\s*(CMD|ENTRYPOINT)\b", line, re.I) for line in lines[final_from + 1 :])


def _has_files(path: Path) -> bool:
    return path.is_dir() and any(item.is_file() for item in path.rglob("*"))


def _is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o111)
