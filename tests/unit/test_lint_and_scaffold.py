from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ale.core.errors import TaskDefinitionError
from ale.run.cli.main import app
from ale.run.lint import lint_repository
from ale.run.scaffold import scaffold_task

pytestmark = pytest.mark.unit


def messages(path: Path) -> str:
    return "\n".join(finding.message for finding in lint_repository(path))


def test_new_task_is_standalone_and_lints_clean(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "fresh")
    assert lint_repository(task) == []
    assert (task / "image" / "Dockerfile").is_file()
    assert not (task / "setup").exists()
    assert "spec_type: core/v1" in (task / "task.yaml").read_text()
    assert "name: fresh" in (task / "task.yaml").read_text()
    assert "image:" in (task / "task.yaml").read_text()
    assert "kind: container" in (task / "task.yaml").read_text()


def test_stage_entries_are_executable(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "fresh")
    for stage in ("verify", "oracle"):
        assert (task / stage / "run.sh").stat().st_mode & 0o111


def test_refuses_to_clobber_without_force(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    scaffold_task(target)
    with pytest.raises(TaskDefinitionError, match="already exists"):
        scaffold_task(target)
    assert scaffold_task(target, force=True) == target


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("instruction.md", "instruction.md"),
        ("image/Dockerfile", "image/Dockerfile"),
        ("verify/run.sh", "verify/run.sh"),
        ("oracle/run.sh", "oracle/run.sh"),
    ],
)
def test_missing_required_file_is_reported(tmp_path: Path, relative: str, message: str) -> None:
    task = scaffold_task(tmp_path / "fresh")
    (task / relative).unlink()
    assert message in messages(task)


def test_removed_repository_concepts_are_reported(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    task = scaffold_task(collection / "tasks" / "fresh")
    (collection / "domain.yaml").write_text("name: old\n")
    (collection / "kits").mkdir()
    (task / "files").mkdir()
    report = messages(collection)
    assert "stable name" in report
    assert "helpers below the owning Task" in report
    assert "top-level files/" in report


@pytest.mark.parametrize("field", ["image", "setup", "verify"])
def test_removed_manifest_asset_fields_have_migration_guidance(tmp_path: Path, field: str) -> None:
    task = scaffold_task(tmp_path / "fresh")
    manifest = task / "task.yaml"
    manifest.write_text(manifest.read_text() + f"\n{field}:\n  assets: []\n")
    report = messages(task)
    assert "asset declarations are removed" in report
    assert f"{field}/assets" in report


def test_fixed_installation_in_setup_is_reported(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "fresh")
    (task / "setup").mkdir()
    entry = task / "setup" / "run.sh"
    entry.write_text("#!/bin/sh\napt-get install -y jq\n")
    entry.chmod(0o755)
    assert "belongs in image/Dockerfile" in messages(task)


def test_final_stage_must_be_ale_base(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "fresh")
    (task / "image" / "Dockerfile").write_text("FROM python:3.12\n")
    assert "final stage" in messages(task)


def test_vm_final_stage_matches_kind_and_owns_no_boot_contract(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "fresh")
    manifest = task / "task.yaml"
    manifest.write_text(manifest.read_text().replace("kind: container", "kind: vm"))
    dockerfile = task / "image" / "Dockerfile"
    dockerfile.write_text('FROM ghcr.io/agentslastexam/sandbox-base-vm-gui:24.04\nCMD ["bash"]\n')
    assert "CMD" in messages(task)
    dockerfile.write_text("FROM ghcr.io/agentslastexam/sandbox-base-cli:latest\n")
    assert "declared vm" in messages(task)


def test_ref_only_assets_are_rejected(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "fresh")
    (task / "image" / "Dockerfile").unlink()
    manifest = task / "task.yaml"
    manifest.write_text(
        manifest.read_text().replace("kind: container", "kind: vm\n  ref: ghcr.io/acme/task-vm:v1")
    )
    assets = task / "image" / "assets"
    assets.mkdir()
    (assets / "unused.bin").write_bytes(b"unused")
    assert "image/assets" in messages(task)


def test_assets_cli_accepts_multiple_paths_collection_and_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def pull(paths, *, collection, force):  # type: ignore[no-untyped-def]
        seen.update(paths=paths, collection=collection, force=force)
        return (
            SimpleNamespace(
                repository="repo",
                remote_repo_id="owner/repo",
                collection_slug="owner/assets-1",
                commit="a" * 40,
                tasks=(),
            ),
        )

    cli = importlib.import_module("ale.run.cli.main")
    monkeypatch.setattr(cli, "pull_assets", pull)
    first = tmp_path / "one"
    second = tmp_path / "two"
    result = CliRunner().invoke(
        app,
        [
            "assets",
            "pull",
            str(first),
            str(second),
            "--collection",
            "owner/assets-1",
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == {
        "paths": [first, second],
        "collection": "owner/assets-1",
        "force": True,
    }
