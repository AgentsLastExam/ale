from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.support import VM_BASE
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
    assert (task / "environment" / "Dockerfile").is_file()
    assert not (task / "environment" / "setup").exists()
    config = tomllib.loads((task / "task.toml").read_text())
    assert config["schema_version"] == "1.4"
    assert config["task"]["name"] == "agentslastexam/fresh"
    assert config["agent"]["network_mode"] == "no-network"


def test_new_task_folder_name_must_be_a_slug(tmp_path: Path) -> None:
    with pytest.raises(TaskDefinitionError, match="lowercase slug"):
        scaffold_task(tmp_path / "Fresh")


def test_stage_entries_are_executable(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "fresh")
    for entry in ("tests/test.sh", "solution/solve.sh"):
        assert (task / entry).stat().st_mode & 0o111


def test_stage_entries_do_not_require_host_executable_bits(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "fresh")
    for entry in ("tests/test.sh", "solution/solve.sh"):
        (task / entry).chmod(0o644)
    assert lint_repository(task) == []


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
        ("environment/Dockerfile", "environment/Dockerfile"),
        ("tests/test.sh", "tests/test.sh"),
        ("solution/solve.sh", "solution/solve.sh"),
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
    (task / "verify").mkdir()
    report = messages(collection)
    assert "stable name" in report
    assert "helpers below the owning Task" in report
    assert "top-level files/" in report
    assert "unsupported top-level Task entry" in report


def test_fixed_installation_in_setup_is_reported(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "fresh")
    (task / "environment" / "setup").mkdir()
    entry = task / "environment" / "setup" / "setup.sh"
    entry.write_text("#!/bin/sh\napt-get install -y jq\n")
    entry.chmod(0o755)
    assert "belongs in the image's Dockerfile" in messages(task)


@pytest.mark.parametrize(
    "base", ["python:3.12", "ghcr.io/agentslastexam/container-ubuntu22-base:latest", "debian"]
)
def test_any_base_image_is_accepted(tmp_path: Path, base: str) -> None:
    task = scaffold_task(tmp_path / "fresh")
    (task / "environment" / "Dockerfile").write_text(f"FROM {base}\n")
    assert messages(task) == ""


def _vm_task(tmp_path: Path) -> Path:
    task = scaffold_task(tmp_path / "fresh")
    config = task / "task.toml"
    config.write_text(config.read_text() + '\n[ale]\nsandbox = "vm"\n')
    return task


def test_vm_final_stage_owns_no_boot_contract(tmp_path: Path) -> None:
    task = _vm_task(tmp_path)
    (task / "environment" / "Dockerfile").write_text(f'FROM {VM_BASE}\nCMD ["bash"]\n')
    assert "CMD" in messages(task)
    (task / "environment" / "Dockerfile").write_text(f"FROM {VM_BASE}\n")
    (task / "environment" / "mkosi.conf").write_text("")
    assert "ALE-owned" in messages(task)
    # Setup is not part of the image build, so its file names are its own.
    (task / "environment" / "mkosi.conf").unlink()
    (task / "environment" / "setup").mkdir()
    (task / "environment" / "setup" / "mkosi.conf").write_text("")
    assert "ALE-owned" not in messages(task)


def test_assets_without_a_build_are_reported(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "fresh")
    config = task / "task.toml"
    config.write_text(
        config.read_text().replace("memory_mb = 4096", 'memory_mb = 4096\ndocker_image = "x:1"')
    )
    (task / "environment" / "Dockerfile").unlink()
    (task / "environment" / "assets").mkdir()
    (task / "environment" / "assets" / "unused.bin").write_bytes(b"unused")
    assert "environment/assets is unused" in messages(task)

    task = _vm_task(tmp_path / "vm")
    (task / "environment" / "Dockerfile").unlink()
    config = task / "task.toml"
    config.write_text(config.read_text() + 'vm_image = "ghcr.io/acme/task-vm:v1"\n')
    (task / "environment" / "assets").mkdir()
    (task / "environment" / "assets" / "unused.bin").write_bytes(b"unused")
    assert "environment/assets is unused" in messages(task)


def test_assets_cli_accepts_multiple_paths_collection_and_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def pull(paths, *, collection, force, revision):  # type: ignore[no-untyped-def]
        seen.update(paths=paths, collection=collection, force=force, revision=revision)
        return (
            SimpleNamespace(
                repository="repo",
                remote_repo_id="agents-last-exam/repo",
                collection_slug="agents-last-exam/assets-1",
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
            "agents-last-exam/assets-1",
            "--force",
            "--revision",
            "a" * 40,
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == {
        "paths": [first, second],
        "collection": "agents-last-exam/assets-1",
        "force": True,
        "revision": "a" * 40,
    }
