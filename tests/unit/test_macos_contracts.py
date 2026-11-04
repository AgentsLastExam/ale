"""macOS task and Provider selection contracts, without infrastructure access."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.support import merged_config, write_task

from ale.core.config import RunConfig
from ale.core.errors import TaskDefinitionError
from ale.core.taskspec import ImageKind, OperatingSystem
from ale.run.providers import ProviderRegistry
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit
IMAGE = "ghcr.io/example/macos@sha256:" + "a" * 64


@pytest.mark.parametrize(
    "vm",
    [
        {"provider": "orchard"},
        {"orchard": {"endpoint": "https://orchard.example.test"}},
    ],
)
def test_macos_vms_need_no_run_settings(vm: dict) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"vm": vm})


def _macos_task(path: Path, layer: dict | None = None, dockerfile: str | None = None) -> Path:
    base = {
        "artifacts": ["/Users/admin/output"],
        "environment": {"os": "macos", "vm_image": IMAGE, "cpus": 2, "memory_mb": 4096},
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
        ({"environment": {"vm_image": None}}, None),
        ({}, "FROM scratch\n"),
        ({"environment": {"vm_image": None}}, "FROM scratch\n"),
    ],
)
def test_macos_task_names_its_image(tmp_path: Path, layer: dict, dockerfile: str | None) -> None:
    with pytest.raises(TaskDefinitionError, match="macOS Task names its image"):
        load_tasks(_macos_task(tmp_path / "task", layer, dockerfile))


def test_macos_verifier_builds_no_image(tmp_path: Path) -> None:
    task = _macos_task(tmp_path / "task", {"verifier": {"environment_mode": "separate"}})
    (task / "tests" / "Dockerfile").write_text("FROM scratch\n")
    with pytest.raises(TaskDefinitionError, match="only a Linux verifier builds"):
        load_tasks(task)


def test_macos_verifier_cannot_run_in_a_container(tmp_path: Path) -> None:
    layer = {"verifier": {"environment": {"docker_image": "ubuntu:24.04"}}}
    with pytest.raises(TaskDefinitionError, match="only a Linux verifier runs in a container"):
        load_tasks(_macos_task(tmp_path / "task", layer))


def test_registry_sends_macos_vms_to_orchard(monkeypatch: pytest.MonkeyPatch) -> None:
    from ale.run.providers import orchard, qemu

    built = []

    def construct(name):
        def make():
            built.append(name)
            return object()

        return make

    monkeypatch.setattr(orchard, "OrchardProvider", construct("orchard"))
    monkeypatch.setattr(qemu, "QemuProvider", construct("qemu"))
    registry = ProviderRegistry(RunConfig())
    macos = registry.get(ImageKind.VM, OperatingSystem.MACOS)
    assert registry.get(ImageKind.VM, OperatingSystem.MACOS) is macos
    assert registry.get(ImageKind.VM, OperatingSystem.WINDOWS) is not macos
    assert built == ["orchard", "qemu"]
