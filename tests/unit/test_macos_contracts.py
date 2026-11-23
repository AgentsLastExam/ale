"""macOS task and Provider selection contracts, without infrastructure access."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.support import merged_config, write_task

from orbit.core.config import RunConfig
from orbit.core.errors import TaskDefinitionError
from orbit.core.taskspec import ImageKind, OperatingSystem
from orbit.run.providers import ProviderRegistry
from orbit.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit
IMAGE = "ghcr.io/example/macos@sha256:" + "a" * 64


def test_macos_vms_need_no_run_settings() -> None:
    assert "orchard" in RunConfig().sandbox.providers
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {"sandbox": {"orchard": {"endpoint": "https://orchard.example.test"}}}
        )


def _macos_task(path: Path, layer: dict | None = None, dockerfile: str | None = None) -> Path:
    base = {
        "artifacts": ["/Users/admin/output"],
        "environment": {"os": "macos", "image": IMAGE, "cpus": 2, "memory_mb": 4096},
        "agent": {"user": "admin", "network_mode": "public"},
    }
    return write_task(path, config=merged_config(base, layer), dockerfile=dockerfile)


def test_macos_task_is_a_vm_named_directly(tmp_path: Path) -> None:
    spec = load_tasks(_macos_task(tmp_path / "task"))[0].spec
    assert spec.os is OperatingSystem.MACOS
    assert spec.image.kind is ImageKind.VM
    assert spec.image.ref == IMAGE


@pytest.mark.parametrize(
    ("layer", "dockerfile"),
    [
        ({"environment": {"image": None}}, None),
        ({}, "FROM scratch\n"),
        ({"environment": {"image": None}}, "FROM scratch\n"),
    ],
)
def test_macos_task_names_or_builds_its_image(
    tmp_path: Path, layer: dict, dockerfile: str | None
) -> None:
    with pytest.raises(TaskDefinitionError, match="macOS Task"):
        load_tasks(_macos_task(tmp_path / "task", layer, dockerfile))


def _built_macos_task(path: Path, *, base: bool = True) -> Path:
    task = _macos_task(path, {"environment": {"image": IMAGE if base else None}})
    (task / "environment").mkdir(exist_ok=True)
    (task / "environment" / "build.sh").write_text("echo installed\n")
    return task


def test_macos_task_builds_on_its_base(tmp_path: Path) -> None:
    spec = load_tasks(_built_macos_task(tmp_path / "task"))[0].spec
    assert spec.image.kind is ImageKind.VM
    assert (spec.image.ref, spec.image.base_ref) == (None, IMAGE)


def test_macos_build_needs_a_base(tmp_path: Path) -> None:
    with pytest.raises(TaskDefinitionError, match=r"build\.sh requires \[environment\]\.image"):
        load_tasks(_built_macos_task(tmp_path / "task", base=False))


def test_a_linux_build_helper_named_build_sh_is_only_a_file(tmp_path: Path) -> None:
    task = write_task(tmp_path / "task", dockerfile="FROM scratch\n")
    (task / "environment" / "build.sh").write_text("echo helper\n")
    assert load_tasks(task)[0].spec.image.base_ref is None


def test_macos_verifier_builds_no_image(tmp_path: Path) -> None:
    task = _macos_task(tmp_path / "task", {"verifier": {"environment_mode": "separate"}})
    (task / "tests" / "Dockerfile").write_text("FROM scratch\n")
    with pytest.raises(TaskDefinitionError, match="only a Linux verifier builds"):
        load_tasks(task)


def test_a_macos_verifier_image_is_a_vm(tmp_path: Path) -> None:
    layer = {"verifier": {"environment": {"image": IMAGE}}}
    image = load_tasks(_macos_task(tmp_path / "task", layer))[0].spec.verify.image
    assert image is not None and (image.kind, image.ref) == (ImageKind.VM, IMAGE)


def test_the_registry_builds_orchard_once_by_its_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from orbit.run.providers import orchard, qemu

    built = []

    def construct(name):
        def make():
            built.append(name)
            return object()

        return make

    monkeypatch.setattr(orchard, "OrchardProvider", construct("orchard"))
    monkeypatch.setattr(qemu, "QemuProvider", construct("qemu"))
    registry = ProviderRegistry(RunConfig())
    macos = registry.get("orchard")
    assert registry.get("orchard") is macos
    assert registry.get("qemu") is not macos
    assert built == ["orchard", "qemu"]
