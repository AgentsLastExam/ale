"""Rebuilding legacy task data for the new scheme.

The previous framework kept every task's data in one bucket, laid out as
``<domain>/<task>/<variant>/{input,software,reference}``, and the files inside refer to
absolute paths that only existed in that framework's virtual machines. Both facts have
to change before the data can be used here, and neither should be worked around at run
time — a task that needs a shim to read its own inputs is a task nobody can reason about.

So the data is rebuilt once, and two things are fixed while it is in flight:

* **paths** — every reference to the old roots becomes the fixed workspace path, because
  the workspace is now identical for every task;
* **shape** — a bundle is split so that gold answers are a separate component. Visibility
  is declared per component, and a bundle that mixes inputs with answers forces every
  task to declare the same data twice, once for each stage.

The result is uploaded to the assets repository, whose commits are immutable — which is
also what finally makes a data version something a run can pin.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MigrationReport", "rebuild_bundle", "rewrite_paths"]

#: The roots the previous framework used inside its guests, as regular expressions.
#: The Windows root appears with both separators — a backslash in shell commands, a
#: forward slash inside file:// URLs — so both are matched.
_LEGACY_ROOTS = (
    r"/media/user/data/agenthle",
    r"/media/user/data/ale-data",
    r"[A-Za-z]:[/\\]agenthle",
)

#: Where each legacy subdirectory lands in the fixed workspace.
_WORKSPACE = {
    "input": "/ale/input",
    "output": "/ale/output",
    "software": "/ale/software",
    "reference": "/ale/reference",
}

#: Files worth rewriting. Binary assets are copied untouched.
_TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".yaml", ".yml", ".cfg", ".html", ".css", ".js",
    ".sh", ".py", ".cmd", ".bat", ".ps1",
}  # fmt: skip


@dataclass
class MigrationReport:
    """What a rebuild did, so the result can be reviewed rather than trusted."""

    bundles: int = 0
    files_copied: int = 0
    files_rewritten: int = 0
    rewrites: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.bundles} bundle(s), {self.files_copied} file(s), "
            f"{self.files_rewritten} rewritten"
        )


def rewrite_paths(text: str, *, domain: str, task: str, variant: str) -> tuple[str, int]:
    """Replace legacy absolute paths with workspace paths.

    Returns the new text and how many substitutions were made. The task-specific
    segments disappear entirely: that coupling — data location derived from a task's
    name — is precisely what the new layout removes.
    """
    count = 0
    result = text
    for root in _LEGACY_ROOTS:
        prefix = rf"{root}[/\\]{re.escape(domain)}[/\\]{re.escape(task)}[/\\]{re.escape(variant)}"
        for legacy_dir, workspace_dir in _WORKSPACE.items():
            pattern = rf"{prefix}[/\\]{legacy_dir}"
            result, hits = re.subn(pattern, workspace_dir, result)
            count += hits
        # A reference to the variant directory itself becomes the workspace root.
        result, hits = re.subn(prefix, "/ale", result)
        count += hits
    return result, count


def _copy_tree(
    source: Path, target: Path, *, domain: str, task: str, variant: str, report: MigrationReport
) -> None:
    """Copy a directory, rewriting paths inside text files as it goes."""
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        if path.is_dir():
            continue
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if path.suffix.lower() in _TEXT_SUFFIXES:
            original = path.read_text(encoding="utf-8", errors="replace")
            rewritten, hits = rewrite_paths(original, domain=domain, task=task, variant=variant)
            destination.write_text(rewritten, encoding="utf-8")
            if hits:
                report.files_rewritten += 1
                report.rewrites.append(f"{path.relative_to(source.parent)} ({hits})")
        else:
            shutil.copy2(path, destination)
        report.files_copied += 1


def rebuild_bundle(
    source: Path, target_root: Path, *, domain: str, task: str, variant: str
) -> MigrationReport:
    """Rebuild one legacy variant bundle into the new layout.

    Produces two components rather than one::

        <domain>/<task>/<variant>/agent/{input,software}   staged before the agent runs
        <domain>/<task>/<variant>/reference/               staged only during scoring

    Splitting them is what lets a task name each component once, and what makes
    "answers cannot reach the agent" a property of the data rather than a rule someone
    has to remember.
    """
    report = MigrationReport(bundles=1)
    base = target_root / domain / task / variant

    for subdir in ("input", "software"):
        _copy_tree(
            source / subdir,
            base / "agent" / subdir,
            domain=domain,
            task=task,
            variant=variant,
            report=report,
        )
    _copy_tree(
        source / "reference",
        base / "reference",
        domain=domain,
        task=task,
        variant=variant,
        report=report,
    )
    return report
