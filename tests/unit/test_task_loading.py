from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ale.core.errors import TaskDefinitionError
from ale.run.lint import lint_repository
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import discover_task_folders, load_tasks

pytestmark = pytest.mark.unit


def test_collection_order_and_names_are_manifest_owned(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    z = scaffold_task(collection / "z")
    scaffold_task(collection / "a")
    (z / "task.yaml").write_text((z / "task.yaml").read_text().replace("name: z", "name: zz"))
    tasks = load_tasks(collection)
    assert [str(task.spec.name) for task in tasks] == ["a", "zz"]
    assert [folder.root.name for folder in discover_task_folders(collection)] == ["a", "z"]


def test_git_repository_source_context_is_bound_to_each_task(
    write_task_repo,
) -> None:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("one",))
    task = load_tasks(repository / "tasks" / "one")[0]
    assert task.source.repository_root == repository.resolve()
    assert task.source.repository_name == "ale-tasks-demo"
    assert task.source.task_relative_path == "tasks/one"


def test_copy_preserves_effective_spec_and_digests(tmp_path: Path) -> None:
    original = scaffold_task(tmp_path / "source" / "demo")
    copied = tmp_path / "other" / "renamed-folder"
    shutil.copytree(original, copied)
    one = load_tasks(original)[0]
    two = load_tasks(copied)[0]
    assert one.spec == two.spec
    assert one.task_digest == two.task_digest
    assert one.image_source_digest == two.image_source_digest


def test_all_stage_asset_roots_are_excluded_from_task_identity(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    before = load_tasks(task)[0].task_digest
    for stage in ("image", "setup", "verify", "oracle"):
        asset = task / stage / "assets" / "large.bin"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(stage.encode())
    assert load_tasks(task)[0].task_digest == before


def test_duplicate_names_fail_before_runtime(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    scaffold_task(collection / "one")
    two = scaffold_task(collection / "two")
    manifest = two / "task.yaml"
    manifest.write_text(manifest.read_text().replace("name: two", "name: one"))
    with pytest.raises(TaskDefinitionError, match="duplicate Task name"):
        load_tasks(collection)


def test_ref_only_solver_has_no_local_source_digest(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    (task / "image" / "Dockerfile").unlink()
    manifest = task / "task.yaml"
    manifest.write_text(
        manifest.read_text().replace("kind: container", "kind: vm\n  ref: ghcr.io/acme/task-vm:v1")
    )
    loaded = load_tasks(task)[0]
    assert loaded.spec.image.kind.value == "vm"
    assert loaded.image_source_digest is None


def _windows_task(tmp_path: Path, *, build: bool = False) -> Path:
    task = scaffold_task(tmp_path / "windows-demo")
    reference = "base_ref" if build else "ref"
    manifest = task / "task.yaml"
    manifest.write_text(
        manifest.read_text()
        .replace("name: windows-demo", "name: windows-demo\nos: windows")
        .replace("kind: container", f"kind: vm\n  {reference}: /images/windows-base.qcow2")
        .replace("/home/user/output", r"C:\Users\user\output")
    )
    for stage in ("verify", "oracle"):
        (task / stage / "run.sh").rename(task / stage / "run.ps1")
    (task / "image" / "Dockerfile").unlink()
    if build:
        (task / "image" / "run.ps1").write_text("Write-Output 'build'\n")
    return task


def test_windows_task_uses_powershell_stage_entries(tmp_path: Path) -> None:
    task = _windows_task(tmp_path)
    loaded = load_tasks(task)[0]
    assert loaded.spec.os.value == "windows"
    assert loaded.image_source_digest is None
    assert loaded.spec.image.ref == "/images/windows-base.qcow2"
    assert loaded.spec.image.base_ref is None
    assert loaded.folder.stage_entry("verify", loaded.spec.os).name == "run.ps1"  # type: ignore[union-attr]


def test_windows_image_script_tracks_source_and_accepts_assets(tmp_path: Path) -> None:
    task = _windows_task(tmp_path, build=True)
    entry = task / "image" / "run.ps1"
    entry.write_text("$ErrorActionPreference = 'Stop'\n& .\\install.ps1\n")
    helper = task / "image" / "install.ps1"
    helper.write_text("choco install -y git\n")
    loaded = load_tasks(task)[0]
    assert loaded.folder.image_script == entry
    assert loaded.spec.image.base_ref == "/images/windows-base.qcow2"
    assert loaded.spec.image.ref is None
    assert loaded.image_source_digest is not None
    assert lint_repository(task) == []

    assets = task / "image" / "assets"
    assets.mkdir()
    (assets / "data.txt").write_text("input")
    assert load_tasks(task)[0].image_source_digest == loaded.image_source_digest
    assert lint_repository(task) == []
    helper.write_text("choco install -y git 7zip\n")
    assert load_tasks(task)[0].image_source_digest != loaded.image_source_digest

    (task / "setup").mkdir()
    (task / "setup" / "run.ps1").write_text("winget install Git.Git\n")
    assert any("belongs in image/run.ps1" in finding.message for finding in lint_repository(task))


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        ("dockerfile", "image/Dockerfile"),
        ("missing-base-ref", "image.base_ref"),
        ("ref-with-script", "image.base_ref"),
        ("both-refs", "mutually exclusive"),
        ("missing-script", "image/run.ps1"),
        ("container", "image.kind: vm"),
        ("verifier-dockerfile", "reuse the solver image or use a ref"),
    ],
)
def test_windows_image_contract_rejects_invalid_sources(
    tmp_path: Path, invalid: str, message: str
) -> None:
    task = _windows_task(tmp_path, build=True)
    manifest = task / "task.yaml"
    if invalid == "dockerfile":
        (task / "image" / "Dockerfile").write_text("FROM scratch\n")
    elif invalid == "missing-base-ref":
        manifest.write_text(
            manifest.read_text().replace("  base_ref: /images/windows-base.qcow2\n", "")
        )
    elif invalid == "ref-with-script":
        manifest.write_text(manifest.read_text().replace("base_ref:", "ref:"))
    elif invalid == "both-refs":
        manifest.write_text(manifest.read_text().replace("kind: vm", "kind: vm\n  ref: task.qcow2"))
    elif invalid == "missing-script":
        (task / "image" / "run.ps1").unlink()
    elif invalid == "container":
        manifest.write_text(manifest.read_text().replace("kind: vm", "kind: container"))
    else:
        (task / "verify" / "Dockerfile").write_text("FROM scratch\n")
    with pytest.raises(TaskDefinitionError, match=message):
        load_tasks(task)
    assert any(message in finding.message for finding in lint_repository(task))


def test_linux_base_ref_is_rejected_instead_of_ignored(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    manifest = task / "task.yaml"
    manifest.write_text(
        manifest.read_text().replace("kind: container", "kind: vm\n  base_ref: base.qcow2")
    )
    with pytest.raises(TaskDefinitionError, match=r"image\.base_ref requires os: windows"):
        load_tasks(task)


def test_linux_powershell_helper_still_requires_dockerfile(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    (task / "image" / "run.ps1").write_text("Write-Output 'build'\n")
    assert load_tasks(task)[0].image_source_digest is not None
    (task / "image" / "Dockerfile").unlink()
    with pytest.raises(TaskDefinitionError, match="requires os: windows"):
        load_tasks(task)


def test_missing_solver_dockerfile_and_ref_fails_loading(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    (task / "image" / "Dockerfile").unlink()
    with pytest.raises(TaskDefinitionError, match=r"image\.ref"):
        load_tasks(task)


def test_unknown_environment_is_rejected_at_load_time(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    manifest = task / "task.yaml"
    manifest.write_text(manifest.read_text() + "environment: custom/unknown\n")
    with pytest.raises(TaskDefinitionError, match="environment"):
        load_tasks(task)


def test_separate_verifier_dockerfile_requires_explicit_image_kind(
    tmp_path: Path,
) -> None:
    task = scaffold_task(tmp_path / "demo")
    manifest = task / "task.yaml"
    manifest.write_text(
        manifest.read_text()
        + "verify:\n"
        + "  environment_mode: separate\n"
        + "  resources: {cpus: 1, memory_mb: 512, storage_mb: null, gpus: 0}\n"
    )
    (task / "verify/Dockerfile").write_text("FROM scratch\n")
    with pytest.raises(TaskDefinitionError, match=r"verify\.image\.kind"):
        load_tasks(task)

    manifest.write_text(
        manifest.read_text().replace("  resources:", "  image: {kind: vm}\n  resources:")
    )
    loaded = load_tasks(task)[0]
    assert loaded.spec.verify.image is not None
    assert loaded.spec.verify.image.kind.value == "vm"
    assert loaded.verifier_image_source_digest is not None


def test_verifier_dockerfile_rejects_shared_mode_but_allows_ignored_ref(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    (task / "verify/Dockerfile").write_text("FROM scratch\n")
    with pytest.raises(TaskDefinitionError, match="requires separate"):
        load_tasks(task)

    manifest = task / "task.yaml"
    manifest.write_text(
        manifest.read_text()
        + "verify:\n"
        + "  environment_mode: separate\n"
        + "  image: {kind: container, ref: example/verifier:latest}\n"
        + "  resources: {cpus: 1, memory_mb: 512, storage_mb: null, gpus: 0}\n"
    )
    loaded = load_tasks(task)[0]
    assert loaded.spec.verify.image is not None
    assert loaded.spec.verify.image.ref == "example/verifier:latest"


def test_external_verifier_requires_kind_and_ref(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    manifest = task / "task.yaml"
    resources = "{cpus: 1, memory_mb: 512, storage_mb: null, gpus: 0}"
    manifest.write_text(
        manifest.read_text()
        + "verify:\n"
        + "  environment_mode: separate\n"
        + "  image: {kind: vm}\n"
        + f"  resources: {resources}\n"
    )
    with pytest.raises(TaskDefinitionError, match=r"verify\.image\.ref"):
        load_tasks(task)

    manifest.write_text(
        manifest.read_text().replace(
            "image: {kind: vm}", "image: {kind: vm, ref: ghcr.io/acme/verifier:v1}"
        )
    )
    assert load_tasks(task)[0].spec.verify.image is not None


def test_removed_verifier_build_mapping_is_rejected(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    manifest = task / "task.yaml"
    manifest.write_text(
        manifest.read_text()
        + "verify:\n"
        + "  environment_mode: separate\n"
        + "  image: {build: verify}\n"
        + "  resources: {cpus: 1, memory_mb: 512, storage_mb: null, gpus: 0}\n"
    )
    with pytest.raises(TaskDefinitionError):
        load_tasks(task)
