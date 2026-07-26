"""Static checks on a task repository.

Everything here is answerable without starting a container, which is the whole point:
a task author iterating on a manifest should learn about a typo in under a second, not
after an image pull. ``ale validate`` is the other half — it runs the oracle and costs a
container — and the two are deliberately separate so the cheap one can run on every save.

A finding names the file it is about and says what to do. "invalid manifest" sends
someone reading a schema; "revision must be a commit, not a branch" does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ale.core.errors import TaskDefinitionError
from ale.core.taskspec import TaskSpec
from ale.run.tasksets.manifest import (
    DOMAIN_MANIFEST,
    INSTRUCTION,
    STAGE_ENTRY,
    TASK_MANIFEST,
    ManifestTaskset,
    TaskFolder,
)

__all__ = ["Finding", "lint_repository"]

#: A branch or tag moves; a run that recorded one cannot be reproduced from it.
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class Finding:
    """One problem, where it is, and what to do about it."""

    path: Path
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def lint_repository(path: Path) -> list[Finding]:
    """Check every task the given path selects.

    Loading is itself most of the check: the manifest schema, strict instruction
    rendering and legacy-path rejection all raise during load, so they are reported
    here rather than reimplemented.
    """
    findings: list[Finding] = []

    try:
        taskset = ManifestTaskset(path)
    except TaskDefinitionError as error:
        return [Finding(path / DOMAIN_MANIFEST, str(error))]

    seen: dict[str, Path] = {}
    for folder in taskset.folders():
        manifest = folder.root / TASK_MANIFEST
        try:
            specs = [task.spec for task in taskset.load_folder(folder)]
        except TaskDefinitionError as error:
            findings.append(Finding(manifest, str(error)))
            continue

        findings.extend(_check_folder(folder))
        for spec in specs:
            findings.extend(_check_spec(spec, manifest))
            label = spec.label
            if label in seen:
                findings.append(
                    Finding(manifest, f"duplicate task {label}, already defined by {seen[label]}")
                )
            seen[label] = manifest

    return findings


def _check_folder(folder: TaskFolder) -> list[Finding]:
    """The files a runnable task cannot do without."""
    findings: list[Finding] = []

    if not (folder.root / INSTRUCTION).is_file():
        findings.append(Finding(folder.root, f"no {INSTRUCTION}: the agent would get no prompt"))

    verify = folder.stage_entry("verify")
    if verify is None:
        findings.append(
            Finding(folder.root, f"no verify/{STAGE_ENTRY}: nothing would score this task")
        )
    elif not _is_executable(verify):
        findings.append(Finding(verify, "not executable: chmod +x it"))

    for stage in ("setup", "oracle"):
        entry = folder.stage_entry(stage)
        if entry is not None and not _is_executable(entry):
            findings.append(Finding(entry, "not executable: chmod +x it"))

    return findings


def _check_spec(spec: TaskSpec, manifest: Path) -> list[Finding]:
    findings: list[Finding] = []

    if spec.validate_.mode == "oracle" and not (manifest.parent / "oracle" / STAGE_ENTRY).is_file():
        findings.append(
            Finding(
                manifest,
                "no oracle, and validation is not declared manual. A task nobody can solve "
                "is a broken task; add oracle/run.sh or declare validate: {mode: manual, "
                'reason: "..."}',
            )
        )

    for stage_name, stage in (("setup", spec.setup), ("verify", spec.verify)):
        for mount in stage.assets:
            if not _COMMIT.match(mount.revision):
                findings.append(
                    Finding(
                        manifest,
                        f"{stage_name} asset {mount.path!r} pins revision {mount.revision!r}, "
                        "which is not a commit. A branch moves, so two runs naming it would "
                        "not read the same bytes",
                    )
                )
            if not mount.dest.startswith("/"):
                findings.append(
                    Finding(manifest, f"{stage_name} asset dest {mount.dest!r} must be absolute")
                )

    for artifact in spec.artifacts:
        if not artifact.startswith("/"):
            findings.append(Finding(manifest, f"artifact path {artifact!r} must be absolute"))

    return findings


def _is_executable(path: Path) -> bool:
    return path.stat().st_mode & 0o111 != 0
