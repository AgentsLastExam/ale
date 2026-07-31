"""The cheap half of task admission.

Every case here is a mistake a real task author makes, and each asserts the *specific*
message rather than merely that something failed — a linter that says "invalid manifest"
sends someone to read a schema, which is the failure this module exists to avoid.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.errors import TaskDefinitionError
from ale.run.lint import lint_repository
from ale.run.scaffold import scaffold_task

pytestmark = pytest.mark.unit


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tasks").mkdir(parents=True)
    (root / "domain.yaml").write_text("name: demo\nrequires_core: '>=0.1,<0.2'\n")
    return root


def messages(root: Path) -> str:
    return "\n".join(finding.message for finding in lint_repository(root))


class TestScaffold:
    def test_a_new_task_passes_lint_immediately(self, tmp_path: Path) -> None:
        root = repo(tmp_path)
        scaffold_task(root / "tasks" / "fresh")
        assert lint_repository(root) == []

    def test_stage_entries_are_executable(self, tmp_path: Path) -> None:
        """A verifier the sandbox cannot run is the same as no verifier."""
        created = scaffold_task(repo(tmp_path) / "tasks" / "fresh")
        for stage in ("setup", "verify", "oracle"):
            assert (created / stage / "run.sh").stat().st_mode & 0o111

    def test_refuses_to_clobber_without_force(self, tmp_path: Path) -> None:
        target = repo(tmp_path) / "tasks" / "fresh"
        scaffold_task(target)
        with pytest.raises(TaskDefinitionError, match="already exists"):
            scaffold_task(target)
        assert scaffold_task(target, force=True) == target


class TestLint:
    def test_missing_instruction_is_reported(self, tmp_path: Path) -> None:
        root = repo(tmp_path)
        created = scaffold_task(root / "tasks" / "fresh")
        (created / "instruction.md").unlink()
        assert "no instruction.md" in messages(root)

    def test_missing_verifier_is_reported(self, tmp_path: Path) -> None:
        root = repo(tmp_path)
        created = scaffold_task(root / "tasks" / "fresh")
        (created / "verify" / "run.sh").unlink()
        assert "nothing would score this task" in messages(root)

    def test_non_executable_entry_is_reported(self, tmp_path: Path) -> None:
        root = repo(tmp_path)
        created = scaffold_task(root / "tasks" / "fresh")
        (created / "verify" / "run.sh").chmod(0o644)
        assert "chmod +x" in messages(root)

    def test_missing_oracle_is_reported(self, tmp_path: Path) -> None:
        root = repo(tmp_path)
        created = scaffold_task(root / "tasks" / "fresh")
        (created / "oracle" / "run.sh").unlink()
        assert "no oracle" in messages(root)

    def test_legacy_manual_validation_does_not_bypass_the_oracle(self, tmp_path: Path) -> None:
        root = repo(tmp_path)
        created = scaffold_task(root / "tasks" / "fresh")
        (created / "oracle" / "run.sh").unlink()
        manifest = created / "task.yaml"
        manifest.write_text(
            manifest.read_text() + '\nvalidate: { mode: manual, reason: "human" }\n'
        )
        report = messages(root)
        assert "no oracle" in report
        assert "validate" in report

    def test_a_branch_revision_is_reported(self, tmp_path: Path) -> None:
        """The whole premise of an asset mount is that naming it twice reads one thing."""
        root = repo(tmp_path)
        created = scaffold_task(root / "tasks" / "fresh")
        manifest = created / "task.yaml"
        manifest.write_text(
            manifest.read_text().replace(
                "setup:\n  assets: []",
                "setup:\n  assets: [{ repo: o/d, revision: main, path: p, dest: /d }]",
            )
        )
        assert "not a commit" in messages(root)

    def test_a_relative_destination_is_reported(self, tmp_path: Path) -> None:
        root = repo(tmp_path)
        created = scaffold_task(root / "tasks" / "fresh")
        manifest = created / "task.yaml"
        manifest.write_text(
            manifest.read_text().replace(
                "setup:\n  assets: []",
                "setup:\n  assets: [{ repo: o/d, revision: abc1234, path: p, dest: rel }]",
            )
        )
        assert "must be absolute" in messages(root)

    def test_an_unknown_kit_is_reported(self, tmp_path: Path) -> None:
        """Reported by the loader, which is also what would refuse to run it."""
        root = repo(tmp_path)
        created = scaffold_task(root / "tasks" / "fresh")
        manifest = created / "task.yaml"
        manifest.write_text(
            manifest.read_text().replace(
                "verify:\n  assets: []\n  kits: []",
                "verify:\n  assets: []\n  kits: [ghost]",
            )
        )
        assert "must exist as kits/ghost/__init__.py" in messages(root)

    def test_an_undeclared_placeholder_is_reported(self, tmp_path: Path) -> None:
        """Strict rendering is a lint finding, not a surprise at run time."""
        root = repo(tmp_path)
        created = scaffold_task(root / "tasks" / "fresh")
        (created / "instruction.md").write_text("Write ${greeting} and ${nowhere}\n")
        assert "undeclared" in messages(root)

    def test_legacy_path_templating_is_reported(self, tmp_path: Path) -> None:
        root = repo(tmp_path)
        created = scaffold_task(root / "tasks" / "fresh")
        (created / "instruction.md").write_text("Write ${greeting} into {self.output_dir}\n")
        assert "legacy" in messages(root)

    def test_a_repository_without_a_domain_manifest_is_reported(self, tmp_path: Path) -> None:
        stray = tmp_path / "not-a-repo"
        stray.mkdir()
        assert "domain.yaml" in messages(stray)
