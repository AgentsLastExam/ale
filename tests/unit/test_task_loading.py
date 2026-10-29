from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from tests.support import edit_task_config as edit

from ale.core.errors import TaskDefinitionError
from ale.core.task import Stage
from ale.core.taskspec import ImageKind, NetworkMode, VerificationMode
from ale.run.assets import ASSET_ROOTS
from ale.run.lint import lint_repository
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import discover_task_folders, load_tasks

pytestmark = pytest.mark.unit


def test_collection_order_and_names_are_configuration_owned(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    z = scaffold_task(collection / "z")
    scaffold_task(collection / "a")
    edit(z, {"task": {"name": "acme/zz"}})
    tasks = load_tasks(collection)
    assert [str(task.spec.name) for task in tasks] == ["agentslastexam/a", "acme/zz"]
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


def test_configuration_maps_onto_the_runtime_spec(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    edit(
        task,
        {
            "environment": {"cpus": 2, "memory_mb": 2048, "storage_mb": 4096, "gpus": 1},
            "agent": {"network_mode": "allowlist", "allowed_hosts": ["pypi.org"]},
            "metadata": {"network_justification": "installs a package"},
            "ale": {
                "sudo": True,
                "skills": ["tools/skills/reviewer"],
                "mcp_servers": ["tools/mcp/search.toml"],
            },
        },
    )
    spec = load_tasks(task)[0].spec
    assert spec.name == "agentslastexam/demo"
    assert spec.image.kind is ImageKind.CONTAINER and spec.image.user == "user"
    assert (spec.resources.cpus, spec.resources.memory_mb) == (2, 2048)
    assert (spec.resources.storage_mb, spec.resources.gpus, spec.resources.sudo) == (4096, 1, True)
    assert spec.network.mode is NetworkMode.ALLOWLIST
    assert spec.network.allowed_hosts == ("pypi.org",)
    assert (spec.timeouts.agent, spec.timeouts.verify) == (300, 120)
    assert spec.tools.skills[0].path == "tools/skills/reviewer"
    assert spec.tools.mcp_servers[0].path == "tools/mcp/search.toml"
    assert spec.metadata == {"network_justification": "installs a package"}
    assert spec.verify.environment_mode is VerificationMode.SHARED


def test_load_errors_name_the_key(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    edit(task, {"agent": {"user": None}, "environment": {"healthcheck": {"command": "true"}}})
    with pytest.raises(TaskDefinitionError, match=r"environment\.healthcheck is not supported"):
        load_tasks(task)
    edit(task, {"environment": {"healthcheck": None}})
    with pytest.raises(TaskDefinitionError, match=r"agent\.user is required"):
        load_tasks(task)
    edit(task, {"agent": {"user": "user", "shell": "bash"}})
    with pytest.raises(TaskDefinitionError, match=r"agent\.shell is not a task\.toml key"):
        load_tasks(task)
    edit(task, {"agent": {"shell": None}, "environment": {"docker_image": " "}})
    with pytest.raises(TaskDefinitionError, match="must not be blank"):
        load_tasks(task)
    (task / "task.toml").write_text("schema_version = ")
    with pytest.raises(TaskDefinitionError, match="invalid TOML"):
        load_tasks(task)


def test_asset_roots_and_setup_are_excluded_where_they_must_be(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    loaded = load_tasks(task)[0]
    for root in ASSET_ROOTS:
        asset = task / root / "large.bin"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(root.encode())
    after = load_tasks(task)[0]
    assert after.task_digest == loaded.task_digest
    assert after.non_verifier_digest == loaded.non_verifier_digest
    assert after.image_source_digest == loaded.image_source_digest

    (task / "environment/setup").mkdir()
    (task / "environment/setup/setup.sh").write_text("true\n")
    after = load_tasks(task)[0]
    assert after.task_digest != loaded.task_digest
    assert after.image_source_digest == loaded.image_source_digest
    (task / "environment/install.sh").write_text("true\n")
    assert load_tasks(task)[0].image_source_digest != loaded.image_source_digest
    changed = load_tasks(task)[0]
    (task / "tests/helper.py").write_text("pass\n")
    assert load_tasks(task)[0].non_verifier_digest == changed.non_verifier_digest


def test_vm_assets_are_excluded_from_the_vm_image_source(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    edit(task, {"ale": {"sandbox": "vm"}})
    loaded = load_tasks(task)[0]
    (task / "environment/assets").mkdir()
    (task / "environment/assets/disk.bin").write_bytes(b"large")
    after = load_tasks(task)[0]
    assert after.image_source_digest == loaded.image_source_digest
    assert after.task_digest == loaded.task_digest


def test_duplicate_names_fail_before_runtime(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    scaffold_task(collection / "one")
    two = scaffold_task(collection / "two")
    edit(two, {"task": {"name": "agentslastexam/one"}})
    with pytest.raises(TaskDefinitionError, match="duplicate Task name"):
        load_tasks(collection)


def test_a_named_image_wins_over_a_build_definition(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    edit(task, {"environment": {"docker_image": "ghcr.io/acme/task:v1"}})
    loaded = load_tasks(task)[0]
    assert loaded.spec.image.ref == "ghcr.io/acme/task:v1"
    assert loaded.image_source_digest is None
    (task / "environment/Dockerfile").unlink()
    assert load_tasks(task)[0].spec.image.ref == "ghcr.io/acme/task:v1"


def test_the_sandbox_decides_what_environment_dockerfile_builds(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    container = load_tasks(task)[0]
    assert container.spec.image.kind is ImageKind.CONTAINER
    edit(task, {"ale": {"sandbox": "vm"}})
    loaded = load_tasks(task)[0]
    assert loaded.spec.image.kind is ImageKind.VM
    assert loaded.image_source_digest == container.image_source_digest

    (task / "environment/Dockerfile").unlink()
    with pytest.raises(TaskDefinitionError, match=r"environment/Dockerfile or \[ale\].vm_image"):
        load_tasks(task)
    edit(task, {"ale": {"vm_image": "ghcr.io/acme/task-vm:v1"}})
    loaded = load_tasks(task)[0]
    assert loaded.spec.image.ref == "ghcr.io/acme/task-vm:v1"
    assert loaded.image_source_digest is None


@pytest.mark.parametrize("sandbox", ["container", "vm"])
def test_linux_tasks_reject_a_windows_build(tmp_path: Path, sandbox: str) -> None:
    task = scaffold_task(tmp_path / "demo")
    edit(task, {"ale": {"sandbox": sandbox}})
    (task / "environment/build.ps1").write_text("Write-Output 'build'\n")
    with pytest.raises(TaskDefinitionError, match="requires os 'windows'"):
        load_tasks(task)
    shutil.rmtree(task / "environment")
    with pytest.raises(TaskDefinitionError, match="environment/Dockerfile or"):
        load_tasks(task)


def _windows_task(tmp_path: Path, *, build: bool = False) -> Path:
    task = scaffold_task(tmp_path / "windows-demo")
    source = {"vm_base": "/images/windows-base.qcow2"} if build else {"vm_image": "/images/w.qcow2"}
    edit(
        task,
        {
            "artifacts": [r"C:\Users\user\output"],
            "environment": {"os": "windows"},
            "ale": {"sandbox": "vm", **source},
        },
    )
    (task / "tests/test.sh").rename(task / "tests/test.ps1")
    (task / "solution/solve.sh").rename(task / "solution/solve.ps1")
    shutil.rmtree(task / "environment")
    if build:
        (task / "environment").mkdir()
        (task / "environment/build.ps1").write_text("Write-Output 'build'\n")
    return task


def test_windows_task_uses_powershell_stage_entries(tmp_path: Path) -> None:
    task = _windows_task(tmp_path)
    loaded = load_tasks(task)[0]
    assert loaded.spec.os.value == "windows"
    assert loaded.image_source_digest is None
    assert loaded.spec.image.ref == "/images/w.qcow2"
    assert loaded.spec.image.base_ref is None
    assert loaded.folder.stage_entry(Stage.TESTS, loaded.spec.os).name == "test.ps1"  # type: ignore[union-attr]
    (task / "solution/solve.ps1").unlink()
    with pytest.raises(TaskDefinitionError, match=r"solution/solve\.ps1"):
        load_tasks(task)


def test_windows_build_script_tracks_source_and_ignores_assets(tmp_path: Path) -> None:
    task = _windows_task(tmp_path, build=True)
    entry = task / "environment/build.ps1"
    entry.write_text("$ErrorActionPreference = 'Stop'\n& .\\install.ps1\n")
    helper = task / "environment/install.ps1"
    helper.write_text("choco install -y git\n")
    loaded = load_tasks(task)[0]
    assert loaded.folder.build_script == entry
    assert loaded.spec.image.base_ref == "/images/windows-base.qcow2"
    assert loaded.spec.image.ref is None
    assert loaded.image_source_digest is not None
    assert lint_repository(task) == []

    assets = task / "environment/assets"
    assets.mkdir()
    (assets / "data.txt").write_text("input")
    assert load_tasks(task)[0].image_source_digest == loaded.image_source_digest
    assert lint_repository(task) == []
    helper.write_text("choco install -y git 7zip\n")
    changed = load_tasks(task)[0].image_source_digest
    assert changed != loaded.image_source_digest

    (task / "environment/setup").mkdir()
    (task / "environment/setup/setup.ps1").write_text("winget install Git.Git\n")
    assert load_tasks(task)[0].image_source_digest == changed
    assert any(
        "belongs in environment/build.ps1" in finding.message for finding in lint_repository(task)
    )


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        ("dockerfile", "builds environment/build.ps1, not a Dockerfile"),
        ("missing-base", r"build\.ps1 requires \[ale\]\.vm_base"),
        ("missing-script", r"vm_base requires environment/build\.ps1"),
        ("container", "requires \\[ale\\].sandbox 'vm'"),
        ("verifier-dockerfile", "Windows verifier"),
    ],
)
def test_windows_image_contract_rejects_invalid_sources(
    tmp_path: Path, invalid: str, message: str
) -> None:
    task = _windows_task(tmp_path, build=True)
    if invalid == "dockerfile":
        (task / "environment/Dockerfile").write_text("FROM scratch\n")
    elif invalid == "missing-base":
        edit(task, {"ale": {"vm_base": None, "vm_image": "/images/w.qcow2"}})
    elif invalid == "missing-script":
        (task / "environment/build.ps1").unlink()
    elif invalid == "container":
        edit(task, {"ale": {"sandbox": "container", "vm_base": None}})
    else:
        edit(task, {"verifier": {"environment_mode": "separate"}})
        (task / "tests/Dockerfile").write_text("FROM scratch\n")
    with pytest.raises(TaskDefinitionError, match=message):
        load_tasks(task)
    assert any(re.search(message, finding.message) for finding in lint_repository(task))


def test_separate_verification_follows_harbor(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    edit(task, {"environment": {"cpus": 2}, "verifier": {"environment_mode": "separate"}})
    verify = load_tasks(task)[0].spec.verify
    assert verify.environment_mode is VerificationMode.SEPARATE
    assert verify.image is None
    assert verify.resources is not None and verify.resources.cpus == 2

    edit(task, {"verifier": {"environment": {"cpus": 1, "memory_mb": 1024}}})
    verify = load_tasks(task)[0].spec.verify
    assert verify.resources is not None
    assert (verify.resources.cpus, verify.resources.memory_mb) == (1, 1024)
    assert verify.image is None

    (task / "tests/Dockerfile").write_text("FROM scratch\n")
    loaded = load_tasks(task)[0]
    assert loaded.spec.verify.image is not None
    assert loaded.spec.verify.image.kind is ImageKind.CONTAINER
    assert loaded.spec.verify.image.ref is None
    assert loaded.verifier_image_source_digest is not None

    edit(task, {"verifier": {"environment": {"docker_image": "example/verifier:v1"}}})
    loaded = load_tasks(task)[0]
    assert loaded.spec.verify.image is not None
    assert loaded.spec.verify.image.ref == "example/verifier:v1"
    assert loaded.verifier_image_source_digest is None

    edit(
        task,
        {
            "verifier": {"environment": {"docker_image": None}},
            "ale": {"verifier": {"sandbox": "vm"}},
        },
    )
    image = load_tasks(task)[0].spec.verify.image
    assert image is not None and image.kind is ImageKind.VM and image.ref is None
    (task / "tests/Dockerfile").unlink()
    with pytest.raises(TaskDefinitionError, match="requires vm_image or tests/Dockerfile"):
        load_tasks(task)
    edit(task, {"ale": {"verifier": {"vm_image": "ghcr.io/acme/verifier:v1"}}})
    image = load_tasks(task)[0].spec.verify.image
    assert image is not None and image.ref == "ghcr.io/acme/verifier:v1"


def test_verifier_dockerfile_requires_separate_verification(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "demo")
    (task / "tests/Dockerfile").write_text("FROM scratch\n")
    with pytest.raises(TaskDefinitionError, match="requires separate"):
        load_tasks(task)
