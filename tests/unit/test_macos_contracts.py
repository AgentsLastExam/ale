"""macOS task and Orchard configuration contracts, without infrastructure access."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ale.core.errors import TaskDefinitionError
from ale.core.taskspec import ImageKind, OperatingSystem, TaskManifestV1, TaskSpec
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit
IMAGE = "ghcr.io/example/macos@sha256:" + "a" * 64


def test_macos_task_accepts_posix_artifacts_and_existing_vm_image() -> None:
    spec = TaskSpec.model_validate(
        {
            "name": "macos-smoke",
            "os": "macos",
            "image": {"kind": "vm", "ref": IMAGE, "user": "admin"},
            "instruction": "Save a document.",
            "artifacts": ["/Users/admin/output"],
            "network": {"mode": "open"},
        }
    )
    assert spec.os is OperatingSystem.MACOS
    assert spec.image.kind is ImageKind.VM


@pytest.mark.parametrize("model", [TaskSpec, TaskManifestV1])
@pytest.mark.parametrize(
    ("role", "image"),
    [
        ("solver", {"kind": "vm", "base_ref": IMAGE}),
        ("solver", {"kind": "vm"}),
        ("verifier", {"kind": "vm"}),
    ],
)
def test_macos_schema_leaves_image_sources_to_preparation(model, role, image):
    values = {
        "spec_type": "core/v1",
        "name": "macos-smoke",
        "os": "macos",
        "image": image if role == "solver" else {"kind": "vm", "ref": IMAGE},
    }
    if role == "verifier":
        values["verify"] = {
            "environment_mode": "separate",
            "image": image,
            "resources": {"cpus": 1, "memory_mb": 512, "storage_mb": None, "gpus": 0},
        }
    if model is TaskSpec:
        values["instruction"] = "Save a document."
    model.model_validate(values)


@pytest.mark.parametrize("model", [TaskSpec, TaskManifestV1])
def test_macos_images_require_vm_kind(model):
    values = {
        "spec_type": "core/v1",
        "name": "macos-smoke",
        "os": "macos",
        "image": {"kind": "container", "ref": IMAGE},
    }
    if model is TaskSpec:
        values["instruction"] = "Save a document."
    with pytest.raises(ValidationError, match=r"macOS solver images require image\.kind: vm"):
        model.model_validate(values)


@pytest.mark.parametrize(
    ("role", "image"),
    [
        ("solver", {"kind": "vm", "base_ref": IMAGE}),
        ("solver", {"kind": "vm"}),
        ("verifier", {"kind": "vm"}),
    ],
)
def test_macos_loading_rejects_sources_without_supported_preparation(write_task_repo, role, image):
    root = write_task_repo(
        image=image if role == "solver" else {"kind": "vm", "ref": IMAGE},
        with_image_dockerfile=False,
        verifier_image=image if role == "verifier" else None,
        manifest_suffix="os: macos\n",
    )
    with pytest.raises(
        TaskDefinitionError, match=r"image\.base_ref requires|requires .*image\.ref"
    ):
        load_tasks(root / "tasks/demo")


def test_macos_separate_verifier_cannot_select_container() -> None:
    with pytest.raises(ValidationError, match="macOS verifier"):
        TaskSpec.model_validate(
            {
                "name": "macos-smoke",
                "os": "macos",
                "image": {"kind": "vm", "ref": IMAGE},
                "instruction": "Save a document.",
                "verify": {
                    "environment_mode": "separate",
                    "image": {"kind": "container", "ref": "ubuntu:24.04"},
                    "resources": {"cpus": 1, "memory_mb": 512, "storage_mb": None, "gpus": 0},
                },
            }
        )


@pytest.mark.parametrize("role", ["solver", "verifier"])
def test_macos_task_loading_rejects_docker_builds(write_task_repo, role: str) -> None:
    root = write_task_repo(
        image={"kind": "vm", "ref": IMAGE},
        with_image_dockerfile=role == "solver",
        verifier_image={"kind": "vm", "ref": IMAGE} if role == "verifier" else None,
        with_verifier_dockerfile=role == "verifier",
        manifest_suffix="os: macos\n",
    )
    with pytest.raises(TaskDefinitionError, match="macOS"):
        load_tasks(root / "tasks/demo")
