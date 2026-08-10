from __future__ import annotations

import json

import pytest

from ale.core.errors import ProviderCapabilityError
from ale.run.providers.docker import (
    _gpu_run_args,
    _managed_gpu_ids,
    _parse_host_nvidia_smi,
    _parse_nvidia_smi,
    _storage_run_args,
    _verify_docker_gpu,
    destroy_retained,
    list_retained,
)

pytestmark = pytest.mark.unit

UUID = "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_nvidia_smi_rows_are_strict_and_normalized() -> None:
    (device,) = _parse_nvidia_smi(f"{UUID.upper()}, NVIDIA RTX 6000, 00000000:65:00.0, 570.1\n")
    assert device.id == UUID
    assert device.model == "NVIDIA RTX 6000"
    assert device.driver_version == "570.1"
    with pytest.raises(ProviderCapabilityError, match="nvidia-smi"):
        _parse_nvidia_smi("not,csv")


def test_host_nvidia_smi_rows_are_indexed() -> None:
    devices = _parse_host_nvidia_smi(
        f"2, {UUID.upper()}, NVIDIA RTX 6000, 00000000:65:00.0, 570.1\n"
    )
    assert devices[2].id == UUID


def test_docker_receives_only_selected_uuids() -> None:
    assert _gpu_run_args((UUID,)) == [
        "--gpus",
        f"device={UUID}",
        "--env",
        "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
    ]


def test_container_must_observe_exact_selected_uuids() -> None:
    devices = _parse_nvidia_smi(f"{UUID}, RTX, 00000000:65:00.0, 570.1\n")
    assert _verify_docker_gpu((UUID,), devices) == devices
    with pytest.raises(ProviderCapabilityError, match="mismatch"):
        _verify_docker_gpu(
            ("GPU-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",),
            devices,
        )


def test_storage_quota_uses_docker_native_writable_layer_limit() -> None:
    assert _storage_run_args(4096) == ["--storage-opt", "size=4096M"]


@pytest.mark.asyncio
async def test_managed_gpu_labels_are_reconciled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def docker(*args: str, **kwargs: object) -> tuple[int, str, str]:
        assert "label=ale.managed=true" in args
        return 0, f"{UUID},GPU-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n\n", ""

    monkeypatch.setattr("ale.run.providers.docker._docker", docker)
    assert await _managed_gpu_ids() == {
        UUID,
        "GPU-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    }


@pytest.mark.asyncio
async def test_retained_listing_includes_stale_state_and_cleanup_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def docker(*args: str, **kwargs: object) -> tuple[int, str, str]:
        if args[:3] == ("ps", "-a", "-q"):
            return 0, "container-id\n", ""
        assert args[0] == "inspect"
        return (
            0,
            json.dumps(
                [
                    {
                        "Name": "/ale-episode-solver",
                        "Image": "sha256:image",
                        "Config": {
                            "Labels": {
                                "ale.managed": "true",
                                "ale.retention": "keep",
                                "ale.episode": "episode",
                                "ale.role": "solver",
                                "ale.gpus": UUID,
                            }
                        },
                        "State": {"Running": False},
                    }
                ]
            ),
            "",
        )

    monkeypatch.setattr("ale.run.providers.docker._docker", docker)
    assert await list_retained() == [
        {
            "provider": "docker",
            "handle": "ale-episode-solver",
            "episode": "episode",
            "role": "solver",
            "image": "sha256:image",
            "gpus": (UUID,),
            "running": False,
            "cleanup_command": "ale sandbox destroy ale-episode-solver",
        }
    ]


@pytest.mark.asyncio
async def test_destroy_rejects_non_ale_container(monkeypatch: pytest.MonkeyPatch) -> None:
    async def docker(*args: str, **kwargs: object) -> tuple[int, str, str]:
        return 0, json.dumps([{"Config": {"Labels": {}}}]), ""

    monkeypatch.setattr("ale.run.providers.docker._docker", docker)
    with pytest.raises(ProviderCapabilityError, match="not an ALE-retained"):
        await destroy_retained("someone-elses-container")
