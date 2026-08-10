from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ale.core.config import RunConfig, SandboxRetentionConfig
from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import ImageKind, PreparedTaskImage, SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources
from ale.run.cli.main import app
from ale.run.episode import _Lease
from ale.run.providers import ProviderRegistry
from ale.run.providers.docker import DockerProvider
from ale.run.providers.qemu import QemuProvider

pytestmark = pytest.mark.unit


def test_provider_slots_are_strict_and_default_to_builtins() -> None:
    settings = RunConfig()
    assert settings.container.provider == "docker"
    assert settings.vm.provider == "qemu"
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"container": {"provider": "qemu"}})
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"container": {"gpus": [0, 0]}})
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"container": {"gpus": [-1]}})


def test_provider_registry_is_lazy_and_reuses_each_slot() -> None:
    registry = ProviderRegistry(RunConfig())
    assert registry._instances == {}
    container = registry.get(ImageKind.CONTAINER)
    machine = registry.get(ImageKind.VM)
    assert isinstance(container, DockerProvider)
    assert isinstance(machine, QemuProvider)
    assert registry.get(ImageKind.CONTAINER) is container
    assert registry.get(ImageKind.VM) is machine


def test_legacy_provider_option_is_not_public() -> None:
    result = CliRunner().invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--provider" not in result.output


def _request(kind: ImageKind, *, role: str = "solver") -> SandboxRequest:
    digest = "sha256:" + ("1" if kind is ImageKind.CONTAINER else "2") * 64
    prepared: dict[str, object] = {
        "kind": kind,
        "source": "external-ref",
        "input_identity": digest,
        "runtime_ref": "image@" + digest if kind is ImageKind.CONTAINER else "/tmp/disk.qcow2",
        "prepared_identity": digest,
        "resolved_reference": "registry/image@" + digest,
    }
    return SandboxRequest(
        episode_id="episode",
        role=role,
        prepared_image=PreparedTaskImage.model_validate(prepared),
        resources=Resources(),
        network=NetworkPolicy(),
    )


class _Sandbox:
    def __init__(self, name: str) -> None:
        self.sandbox_id = name
        self.destroyed = False

    async def destroy(self) -> None:
        self.destroyed = True

    def release_resources(self) -> None:
        pass


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.requests: list[SandboxRequest] = []

    async def create(self, request: SandboxRequest) -> _Sandbox:
        self.requests.append(request)
        return _Sandbox(f"{self.name}-{len(self.requests)}")


@pytest.mark.asyncio
async def test_one_lease_routes_each_request_and_attributes_its_provider() -> None:
    container = _Provider("docker")
    vm = _Provider("qemu")
    registry = SimpleNamespace(get=lambda kind: container if kind is ImageKind.CONTAINER else vm)
    lease = _Lease(registry, SandboxRetentionConfig())  # type: ignore[arg-type]

    solver = await lease.acquire(_request(ImageKind.CONTAINER))
    verifier = await lease.acquire(_request(ImageKind.VM, role="verifier"))
    await lease.release(solver)  # type: ignore[arg-type]
    await lease.release(verifier)  # type: ignore[arg-type]

    assert [request.image_kind for request in container.requests] == [ImageKind.CONTAINER]
    assert [request.image_kind for request in vm.requests] == [ImageKind.VM]
    assert {outcome.provider for outcome in lease.outcomes} == {"docker", "qemu"}


@pytest.mark.asyncio
async def test_missing_provider_fails_at_request_admission() -> None:
    class Registry:
        def get(self, kind: ImageKind) -> None:
            raise ProviderCapabilityError(f"no provider for {kind}")

    lease = _Lease(Registry(), SandboxRetentionConfig())  # type: ignore[arg-type]
    with pytest.raises(ProviderCapabilityError, match="no provider for vm"):
        await lease.acquire(_request(ImageKind.VM))
