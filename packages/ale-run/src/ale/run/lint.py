"""Fast static checks for self-contained Task folders."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", re.I | re.M)
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
        load_tasks(source)
    except TaskDefinitionError as error:
        findings.append(Finding(source / TASK_CONFIG, str(error)))
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
    findings.extend(_check_image_versions(folder, config))
    agent, metadata = config.get("agent"), config.get("metadata")
    if (
        isinstance(agent, dict)
        and agent.get("network_mode") == "public"
        and not (isinstance(metadata, dict) and metadata.get("network_justification"))
    ):
        findings.append(
            Finding(
                folder.root / TASK_CONFIG,
                "network_mode 'public' needs a reason in [metadata].network_justification",
            )
        )
    return findings


def _moves(image: str) -> bool:
    """Whether ``image`` can change under a Task: no digest, and no tag or ``latest``."""
    if "@" in image or "$" in image:  # a build argument names it elsewhere
        return False
    if PurePosixPath(image).is_absolute():  # a local disk, whose content is its identity
        return False
    return image.rpartition("/")[2].partition(":")[2] in ("", "latest")


def _check_image_versions(folder: TaskFolder, config: dict[str, object]) -> list[Finding]:
    """Every image a Task names, built on or used directly, at an exact version."""
    named: list[tuple[Path, str]] = []
    for dockerfile in (folder.dockerfile, folder.verifier_dockerfile):
        if dockerfile is None:
            continue
        stages = _FROM.findall(dockerfile.read_text(encoding="utf-8", errors="replace"))
        aliases = {alias.lower() for _, alias in stages} | {"scratch"}
        named += [(dockerfile, base) for base, _ in stages if base.lower() not in aliases]
    environment = config.get("environment")
    verifier = config.get("verifier")
    nested = verifier.get("environment") if isinstance(verifier, dict) else None
    for table in (environment, nested):
        image = table.get("image") if isinstance(table, dict) else None
        if isinstance(image, str):
            named.append((folder.root / TASK_CONFIG, image))
    return [
        Finding(path, f"{image} can change under the Task; name an exact version")
        for path, image in named
        if _moves(image)
    ]


def _check_variables(path: Path, config: dict[str, object]) -> list[Finding]:
    """A Task's ``env`` tables: no credential is written down, and no value misleads."""
    tables: list[tuple[str, object]] = []
    for table in ("environment", "verifier", "solution"):
        body = config.get(table)
        if isinstance(body, dict):
            tables.append((table, body.get("env")))
            nested = body.get("environment") if table == "verifier" else None
            if isinstance(nested, dict):
                tables.append(("verifier.environment", nested.get("env")))
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
