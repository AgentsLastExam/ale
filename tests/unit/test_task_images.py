from __future__ import annotations

import asyncio
import io
import json
import tarfile
from pathlib import Path

import pytest

from ale.core.errors import ProviderStartError, TaskDefinitionError
from ale.core.ids import content_hash
from ale.core.sandbox import ImageRef, PreparedTaskImage
from ale.core.task import Task
from ale.core.taskspec import ImageKind, VerifySpec
from ale.run.cli.tasks import _prepare_images
from ale.run.task_images import (
    _materialize,
    _materialize_vm,
    _OciBuild,
    prepare_task_image,
    prepare_task_image_result,
    prepare_verifier_image,
)
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit


def _task(write_task_repo, **kwargs: object) -> object:  # type: ignore[no-untyped-def]
    repository = write_task_repo("ale-tasks-demo", tasks=("demo",), **kwargs)
    return load_tasks(repository / "tasks/demo")[0]


def _prepared(kind: ImageKind, source: str = "solver-local") -> PreparedTaskImage:
    digest = "sha256:" + "d" * 64
    values: dict[str, object] = {
        "kind": kind,
        "source": source,
        "input_identity": digest,
        "runtime_ref": "ale-local:fixture" if kind is ImageKind.CONTAINER else "/tmp/disk.qcow2",
        "prepared_identity": digest,
    }
    if source == "external-ref":
        values["resolved_reference"] = "ghcr.io/acme/image@" + digest
    else:
        values["image_source_identity"] = digest
    if kind is ImageKind.VM and source != "external-ref":
        values["oci_identity"] = digest
        values["materializer_identity"] = digest
    return PreparedTaskImage.model_validate(values)


class _Provider:
    def __init__(self, kind: ImageKind) -> None:
        self.kind = kind
        self.inputs: list[ImageRef | PreparedTaskImage] = []

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        assert image.kind is self.kind
        self.inputs.append(image)
        if isinstance(image, PreparedTaskImage):
            return image
        digest = "sha256:" + ("a" if self.kind is ImageKind.CONTAINER else "b") * 64
        return PreparedTaskImage(
            kind=self.kind,
            source="external-ref",
            input_identity=digest,
            runtime_ref=(
                "ghcr.io/acme/image@" + digest
                if self.kind is ImageKind.CONTAINER
                else "/cache/disk.qcow2"
            ),
            prepared_identity=digest,
            resolved_reference="ghcr.io/acme/image@" + digest,
        )


class _Registry:
    def __init__(self) -> None:
        self.providers = {kind: _Provider(kind) for kind in ImageKind}

    def get(self, kind: ImageKind) -> _Provider:
        return self.providers[kind]


@pytest.fixture
def windows_task(write_task_repo) -> Task:  # type: ignore[no-untyped-def]
    repository = write_task_repo(
        image={"kind": "vm", "ref": "/images/windows.qcow2"},
        with_image_dockerfile=False,
        manifest_suffix="os: windows\n",
    )
    folder = repository / "tasks/demo"
    (folder / "image/run.ps1").write_text("Write-Output 'installed'\n")
    for stage in ("verify", "oracle"):
        (folder / stage / "run.sh").rename(folder / stage / "run.ps1")
    return load_tasks(folder)[0]


@pytest.mark.asyncio
async def test_windows_image_cache_tracks_all_inputs_including_cli_batches(
    windows_task: Task, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ale.run.task_images as images

    builds: list[Path] = []
    runner = "sha256:" + "e" * 64

    async def runner_identity(reference: str) -> str:
        return runner

    async def build(base, context, output, resources, runner_identity):  # type: ignore[no-untyped-def]
        assert resources.cpus == 2 and resources.memory_mb == 4096
        assert runner_identity == runner
        builds.append(output)
        output.write_bytes(b"valid-disk")

    async def valid(path: Path) -> bool:
        return path.is_file() and path.read_bytes() == b"valid-disk"

    monkeypatch.setattr(images, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(images, "_docker_image_identity", runner_identity)
    monkeypatch.setattr(images, "_build_windows_image", build)
    monkeypatch.setattr(images, "_valid_qcow2", valid)
    registry = _Registry()
    first = await prepare_task_image_result(windows_task, registry)  # type: ignore[arg-type]
    second = await prepare_task_image_result(windows_task, registry)  # type: ignore[arg-type]
    assert len(builds) == 1
    assert [step.name for step in first.steps] == [
        "lint",
        "base-resolution",
        "windows-build",
        "artifact-check",
    ]
    assert second.steps[2].outcome == "reused"
    assert first.image == second.image
    assert first.image.base_materials == ("sha256:" + "b" * 64,)
    assert first.image.builder_identity and first.image.oci_identity is None

    assert windows_task.folder is not None
    asset = windows_task.folder.image_dir / "assets/input.txt"
    asset.parent.mkdir()
    for value in ("first", "changed"):
        asset.write_text(value)
        changed = await prepare_task_image(windows_task, registry)  # type: ignore[arg-type]
        assert changed.prepared_identity != first.image.prepared_identity
    assert len(builds) == 3
    (windows_task.folder.image_dir / "empty").mkdir()
    await prepare_task_image(windows_task, registry)  # type: ignore[arg-type]
    assert len(builds) == 4
    runner = "sha256:" + "f" * 64
    await prepare_task_image(windows_task, registry)  # type: ignore[arg-type]
    assert len(builds) == 5
    base = _prepared(ImageKind.VM, "external-ref")
    await images._prepare_windows_image(windows_task, base)
    assert len(builds) == 6

    other = load_tasks(windows_task.folder.root)[0]
    other.spec = other.spec.model_copy(
        update={
            "resources": other.spec.resources.model_copy(
                update={"storage_mb": (other.spec.resources.storage_mb or 0) + 4096}
            )
        }
    )
    monkeypatch.setattr("ale.run.cli.tasks.observe_task_assets", lambda task: None)
    results = await _prepare_images([windows_task, other], registry)  # type: ignore[arg-type]
    assert results[0].image.prepared_identity != results[1].image.prepared_identity
    assert len(builds) == 7  # The unchanged first Task reuses its disk; the larger one builds.


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["script", "input-change", "invalid-disk"])
async def test_failed_windows_build_does_not_publish_or_fall_back(
    windows_task: Task, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import ale.run.task_images as images

    async def runner_identity(reference: str) -> str:
        return "sha256:" + "e" * 64

    async def build(base, context, output, resources, runner):  # type: ignore[no-untyped-def]
        output.write_bytes(b"partial")
        if failure == "script":
            raise TaskDefinitionError("installer failed")
        if failure == "input-change":
            (context / "added-during-build").mkdir()

    async def valid(path: Path) -> bool:
        return False

    cache = tmp_path / "cache"
    monkeypatch.setattr(images, "cache_root", lambda: cache)
    monkeypatch.setattr(images, "_docker_image_identity", runner_identity)
    monkeypatch.setattr(images, "_build_windows_image", build)
    monkeypatch.setattr(images, "_valid_qcow2", valid)
    registry = _Registry()
    with pytest.raises((TaskDefinitionError, ProviderStartError), match="windows-build:"):
        await prepare_task_image(windows_task, registry)  # type: ignore[arg-type]
    assert len(registry.get(ImageKind.VM).inputs) == 1
    assert isinstance(registry.get(ImageKind.VM).inputs[0], ImageRef)
    assert list(cache.rglob("*.qcow2")) == []


@pytest.mark.asyncio
async def test_buildx_uses_fixed_context_and_observes_container_identity(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    task = _task(write_task_repo)
    commands: list[tuple[str, ...]] = []

    async def fake_run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
        commands.append(argv)
        if argv[:3] == ("docker", "buildx", "version"):
            return 0, "buildx", ""
        if argv[:3] == ("docker", "buildx", "build"):
            Path(argv[argv.index("--metadata-file") + 1]).write_text(
                json.dumps({"materials": [{"uri": "pkg:docker/base"}]})
            )
            return 0, "", ""
        return 0, "sha256:" + "c" * 64 + "\n", ""

    monkeypatch.setattr("ale.run.task_images._run", fake_run)
    registry = _Registry()
    prepared = await prepare_task_image(task, registry)  # type: ignore[arg-type]
    build = next(command for command in commands if command[:3] == ("docker", "buildx", "build"))
    assert build[-1] == str(task.folder.image_dir)  # type: ignore[attr-defined]
    assert prepared.kind is ImageKind.CONTAINER
    assert prepared.source == "solver-local"
    assert prepared.runtime_ref.startswith("ale-solver-local:")
    assert prepared.prepared_identity == "sha256:" + "c" * 64
    assert registry.get(ImageKind.CONTAINER).inputs == [prepared]


@pytest.mark.asyncio
async def test_dockerfile_wins_over_ref_and_build_failure_never_falls_back(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    task = _task(
        write_task_repo,
        image={"kind": "container", "ref": "ghcr.io/acme/fallback:v1"},
    )

    async def fake_run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
        if argv[:3] == ("docker", "buildx", "version"):
            return 0, "", ""
        return 1, "", "Dockerfile line 3 failed"

    monkeypatch.setattr("ale.run.task_images._run", fake_run)
    registry = _Registry()
    with pytest.raises(TaskDefinitionError, match="Dockerfile line 3"):
        await prepare_task_image(task, registry)  # type: ignore[arg-type]
    assert registry.get(ImageKind.CONTAINER).inputs == []


@pytest.mark.asyncio
async def test_ref_only_solver_is_prepared_by_matching_provider(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    task = _task(
        write_task_repo,
        image={"kind": "vm", "ref": "ghcr.io/acme/task-vm:v1"},
        with_image_dockerfile=False,
    )
    registry = _Registry()
    prepared = await prepare_task_image(task, registry)  # type: ignore[arg-type]
    assert prepared.kind is ImageKind.VM
    assert registry.get(ImageKind.VM).inputs == [
        ImageRef(kind="vm", reference="ghcr.io/acme/task-vm:v1")
    ]
    assert registry.get(ImageKind.CONTAINER).inputs == []


@pytest.mark.asyncio
async def test_preparation_result_orders_ref_and_artifact_stages(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    task = _task(
        write_task_repo,
        image={"kind": "vm", "ref": "ghcr.io/acme/task-vm:v1"},
        with_image_dockerfile=False,
    )
    result = await prepare_task_image_result(task, _Registry())  # type: ignore[arg-type]
    assert result.task == "demo@base"
    assert result.role == "solver"
    assert [step.name for step in result.steps] == [
        "lint",
        "reference-resolution",
        "artifact-check",
    ]
    assert all(step.outcome == "executed" for step in result.steps)


@pytest.mark.asyncio
async def test_preparation_failure_names_and_bounds_its_stage(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    task = _task(
        write_task_repo,
        image={"kind": "vm", "ref": "example.invalid/vm:latest"},
        with_image_dockerfile=False,
    )

    class _FailingProvider:
        async def prepare_image(self, image):  # type: ignore[no-untyped-def]
            raise ProviderStartError("x" * 3000 + "tail-marker")

    class _FailingRegistry:
        def get(self, kind):  # type: ignore[no-untyped-def]
            return _FailingProvider()

    with pytest.raises(ProviderStartError, match=r"^reference-resolution: ") as caught:
        await prepare_task_image_result(task, _FailingRegistry())  # type: ignore[arg-type]
    assert len(str(caught.value)) <= 2022
    assert str(caught.value).endswith("tail-marker")


@pytest.mark.asyncio
async def test_image_assets_do_not_change_the_local_source_identity(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    task = _task(write_task_repo)
    asset = task.folder.image_dir / "assets/data.txt"  # type: ignore[attr-defined]
    asset.parent.mkdir()
    asset.write_text("one")

    async def fake_run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
        if argv[:3] == ("docker", "buildx", "version"):
            return 0, "", ""
        if argv[:3] == ("docker", "buildx", "build"):
            Path(argv[argv.index("--metadata-file") + 1]).write_text("{}")
            return 0, "", ""
        return 0, "sha256:" + "c" * 64, ""

    monkeypatch.setattr("ale.run.task_images._run", fake_run)
    first = await prepare_task_image(task, _Registry())  # type: ignore[arg-type]
    asset.write_text("two")
    second = await prepare_task_image(task, _Registry())  # type: ignore[arg-type]
    assert first.input_identity == second.input_identity


@pytest.mark.asyncio
async def test_separate_verifier_reuses_complete_solver_image(write_task_repo) -> None:  # type: ignore[no-untyped-def]
    task = _task(write_task_repo)
    task.spec = task.spec.model_copy(
        update={
            "verify": VerifySpec.model_validate(
                {
                    "environment_mode": "separate",
                    "resources": {"cpus": 1, "memory_mb": 512, "storage_mb": None, "gpus": 0},
                }
            )
        }
    )
    task.prepared_image = _prepared(ImageKind.VM)
    assert await prepare_verifier_image(task, _Registry()) is task.prepared_image  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_local_vm_candidate_is_materialized_before_provider_check(
    write_task_repo, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    task = _task(write_task_repo, image={"kind": "vm"})
    vm = _prepared(ImageKind.VM)

    async def fake_build(**kwargs: object) -> _OciBuild:
        assert kwargs["source_digest"] == task.image_source_digest
        return _vm_build()

    async def fake_materialize(build: object, *, source: str) -> tuple[PreparedTaskImage, bool]:
        assert source == "solver-local"
        return vm, False

    monkeypatch.setattr("ale.run.task_images._build_oci", fake_build)
    monkeypatch.setattr("ale.run.task_images._materialize_vm_with_status", fake_materialize)
    registry = _Registry()
    assert await prepare_task_image(task, registry) == vm  # type: ignore[arg-type]
    assert registry.get(ImageKind.VM).inputs == [vm]


def test_vm_derivation_uses_oci_and_materializer_identities_only() -> None:
    from ale.run.task_images import vm_materialization_identity

    oci = "sha256:" + "1" * 64
    materializer = "sha256:" + "2" * 64
    expected = content_hash(
        {
            "output_contract": "ale-vm-qcow2/v1",
            "final_oci_identity": oci,
            "materializer_identity": materializer,
        }
    )
    assert vm_materialization_identity(oci, materializer) == expected


def _vm_build() -> _OciBuild:
    return _OciBuild(
        input_identity="sha256:" + "1" * 64,
        image_source_identity="sha256:" + "2" * 64,
        runtime_ref="ale-vm:fixture",
        oci_identity="sha256:" + "3" * 64,
        base_materials=("sha256:" + "4" * 64,),
    )


@pytest.mark.asyncio
async def test_unchanged_vm_derivation_materializes_once_under_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "ale.run.task_images._materializer_identity",
        lambda: asyncio.sleep(0, result="sha256:" + "5" * 64),
    )
    calls = 0

    async def valid(path: Path) -> bool:
        return path.is_file()

    async def materialize(_runtime_ref: str, disk: Path) -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        disk.write_bytes(b"qcow2")

    monkeypatch.setattr("ale.run.task_images._valid_qcow2", valid)
    monkeypatch.setattr("ale.run.task_images._materialize", materialize)
    first, second = await asyncio.gather(
        _materialize_vm(_vm_build(), source="solver-local"),
        _materialize_vm(_vm_build(), source="solver-local"),
    )
    assert first == second
    assert calls == 1


def _write_rootfs_tar(path: Path) -> None:
    payload = b"initrd"
    info = tarfile.TarInfo("boot/initrd.img-test")
    info.size = len(payload)
    with tarfile.open(path, "w") as archive:
        archive.addfile(info, io.BytesIO(payload))


@pytest.mark.asyncio
async def test_vm_materialization_publishes_only_valid_output_and_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = tmp_path / "final.qcow2"
    commands: list[tuple[str, ...]] = []

    async def run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
        commands.append(argv)
        if argv[:2] == ("docker", "create"):
            return 0, "container-id\n", ""
        if argv[:2] == ("docker", "export"):
            _write_rootfs_tar(Path(argv[argv.index("--output") + 1]))
        if argv[:2] == ("docker", "run"):
            output_mount = next(value for value in argv if "dst=/output" in value)
            output_dir = Path(output_mount.split("src=", 1)[1].split(",", 1)[0])
            (output_dir / "disk.qcow2").write_bytes(b"valid")
        return 0, "", ""

    async def valid(path: Path) -> bool:
        return path.read_bytes() == b"valid" if path.is_file() else False

    monkeypatch.setattr("ale.run.task_images._run", run)
    monkeypatch.setattr("ale.run.task_images._valid_qcow2", valid)
    await _materialize("ale-vm:fixture", disk)
    assert disk.read_bytes() == b"valid"
    assert not list(tmp_path.glob(".final-*"))
    assert any(command[:3] == ("docker", "rm", "-f") for command in commands)


@pytest.mark.asyncio
async def test_failed_vm_materialization_leaves_no_reusable_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = tmp_path / "final.qcow2"

    async def run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
        if argv[:2] == ("docker", "create"):
            return 0, "container-id\n", ""
        if argv[:2] == ("docker", "export"):
            _write_rootfs_tar(Path(argv[argv.index("--output") + 1]))
            return 0, "", ""
        if argv[:2] == ("docker", "run"):
            return 1, "", "materializer failed"
        return 0, "", ""

    monkeypatch.setattr("ale.run.task_images._run", run)
    with pytest.raises(Exception, match="materializer failed"):
        await _materialize("ale-vm:fixture", disk)
    assert not disk.exists()
    assert not list(tmp_path.glob(".final-*"))
