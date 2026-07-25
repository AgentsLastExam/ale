#!/usr/bin/env python3
"""One-off: rewrite legacy absolute paths in task data, then publish it.

The previous framework's data files refer to paths that only existed inside its virtual
machines (``/media/user/data/agenthle/<domain>/<task>/<variant>/input`` and friends).
Those references have to be fixed once, at migration time, rather than shimmed at run
time — a task that needs a shim to read its own inputs is a task nobody can reason about.

The directory layout is left exactly as it is. A task addresses whichever subdirectory it
needs by declaring a component for it, so nothing has to be restructured.

Usage:
    python scripts/migrate_legacy_data.py <source-tree> <target-tree> [--domain NAME]

The target is what gets uploaded to the assets repository. This script is deliberately
not part of the `ale` command line: it runs once per data drop, not as part of anyone's
workflow.
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

#: What each legacy subdirectory becomes. These are the paths the rebuilt tasks declare,
#: matching the convention the demo tasks use; a domain that wants another layout says so
#: in its own manifests and passes different values here.
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
    """Copy one legacy variant bundle, rewriting the paths inside it.

    The layout is preserved: ``input``, ``software`` and ``reference`` stay where they
    are, and a task points a component at whichever one it needs.
    """
    report = MigrationReport(bundles=1)
    base = target_root / domain / task / variant
    for subdir in ("input", "software", "reference"):
        _copy_tree(
            source / subdir,
            base / subdir,
            domain=domain,
            task=task,
            variant=variant,
            report=report,
        )
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--domain", help="Migrate one domain only")
    args = parser.parse_args(argv)

    total = MigrationReport()
    for domain_dir in sorted(p for p in args.source.iterdir() if p.is_dir()):
        if args.domain and domain_dir.name != args.domain:
            continue
        for task_dir in sorted(p for p in domain_dir.iterdir() if p.is_dir()):
            for variant_dir in sorted(p for p in task_dir.iterdir() if p.is_dir()):
                report = rebuild_bundle(
                    variant_dir,
                    args.target,
                    domain=domain_dir.name,
                    task=task_dir.name,
                    variant=variant_dir.name,
                )
                total.bundles += report.bundles
                total.files_copied += report.files_copied
                total.files_rewritten += report.files_rewritten
                total.rewrites.extend(report.rewrites)

    print(total.summary())
    for line in total.rewrites:
        print(f"  rewrote {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
