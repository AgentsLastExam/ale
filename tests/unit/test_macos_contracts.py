"""macOS task and Orchard configuration contracts, without infrastructure access."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.support import merged_config, write_task

from ale.core.config import OrchardProviderConfig, RunConfig, load_run_config
from ale.core.errors import ConfigError, TaskDefinitionError
from ale.core.taskspec import ImageKind, OperatingSystem
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
