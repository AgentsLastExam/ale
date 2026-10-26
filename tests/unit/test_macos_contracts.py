"""macOS task and Orchard configuration contracts, without infrastructure access."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ale.core.config import OrchardProviderConfig, RunConfig, load_run_config
from ale.core.errors import ConfigError, TaskDefinitionError
from ale.core.taskspec import ImageKind, OperatingSystem, TaskManifestV1, TaskSpec
from ale.run.providers import ProviderRegistry
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit
IMAGE = "ghcr.io/example/macos@sha256:" + "a" * 64


def test_orchard_configuration_uses_host_environment_names(tmp_path: Path) -> None:
    config = tmp_path / "run.toml"
    config.write_text(
        "[vm]\nprovider='orchard'\n[vm.orchard]\n"
        "endpoint='https://orchard.example.test/'\n"
        "gateway_host='ale.example.test'\n"
    )
    settings = load_run_config(run_path=config)
    orchard = settings.vm.orchard
    assert orchard is not None
    assert orchard.endpoint == "https://orchard.example.test"
    assert orchard.username_env == "ALE_ORCHARD_USERNAME"
    assert orchard.password_env == "ALE_ORCHARD_PASSWORD"
    assert orchard.python_path == "/opt/homebrew/bin/python3"
    assert (
        settings.model_dump(mode="json")["vm"]["orchard"]["password_env"] == "ALE_ORCHARD_PASSWORD"
    )


@pytest.mark.parametrize(
    "values",
    [
        {"provider": "orchard"},
        {"provider": "qemu", "orchard": {"endpoint": "https://orchard.example.test"}},
    ],
)
def test_provider_selection_requires_matching_configuration(values: dict) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"vm": values})


@pytest.mark.parametrize(
    "overrides",
    [
        {"endpoint": "https://user:secret@orchard.example.test"},
        {"endpoint": "https://orchard.example.test?password=secret"},
        {"endpoint": "file:///tmp/orchard"},
        {"endpoint": "http://orchard.example.test"},
        {"endpoint": "https://orchard.example.test:invalid"},
        {"password_env": "secret value"},
        {"python_path": "python3"},
        {"python_path": "/opt/../python3"},
        {"boot_timeout_sec": 0},
        {"driver_path": "/opt/driver"},
    ],
)
def test_invalid_orchard_settings_fail_before_connecting(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        OrchardProviderConfig.model_validate(
            {"endpoint": "https://orchard.example.test", **overrides}
        )


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


def test_registry_selects_orchard_without_creating_a_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    from ale.run.providers import orchard

    configured = OrchardProviderConfig(endpoint="https://orchard.example.test")
    settings = RunConfig.model_validate({"vm": {"provider": "orchard", "orchard": configured}})
    instances = []

    def construct(config):
        assert config == configured
        instance = object()
        instances.append(instance)
        return instance

    monkeypatch.setattr(orchard, "OrchardProvider", construct)
    registry = ProviderRegistry(settings)
    assert registry.get(ImageKind.VM) is registry.get(ImageKind.VM)
    assert len(instances) == 1


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost.",
        "x.localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "::",
        "::ffff:127.0.0.1",
        "https://ale.test",
        "ale.test/path",
        "user@ale.test",
        "ale.test:8080",
        "ale test",
    ],
)
def test_gateway_host_requires_remote_host_without_url_or_port(host: str) -> None:
    with pytest.raises(ValidationError, match="gateway_host"):
        OrchardProviderConfig(endpoint="https://orchard.test", gateway_host=host)


@pytest.mark.parametrize("host", ["ale.example.test", "192.0.2.10", "2001:db8::10"])
def test_gateway_host_accepts_remote_dns_and_ip_literals(host: str) -> None:
    assert (
        OrchardProviderConfig(endpoint="https://orchard.test", gateway_host=host).gateway_host
        == host
    )


@pytest.mark.parametrize("via_override", [False, True])
def test_worker_selection_is_rejected_in_files_and_overrides(tmp_path, via_override):
    config = tmp_path / "run.toml"
    config.write_text(
        "[vm]\nprovider='orchard'\n[vm.orchard]\nendpoint='https://orchard.test'\n"
        + ("" if via_override else "worker='mac-1'\n")
    )
    with pytest.raises(ConfigError, match="worker"):
        load_run_config(
            run_path=config, overrides=["vm.orchard.worker=mac-1"] if via_override else []
        )


def test_removed_local_image_configuration_is_rejected(tmp_path):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OrchardProviderConfig(endpoint="https://127.0.0.1:6120", local_tart_home=tmp_path)
