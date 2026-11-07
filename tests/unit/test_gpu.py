from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from ale.core.errors import ProviderCapabilityError
from ale.run.gpu import GpuLease, accepts_model, normalize_nvidia_uuid, normalize_pci_bdf

pytestmark = pytest.mark.unit


def _try_lease(lock_dir: str, output: multiprocessing.Queue[bool]) -> None:
    try:
        lease = GpuLease.acquire(("GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",), 1, Path(lock_dir))
    except ProviderCapabilityError:
        output.put(False)
    else:
        output.put(True)
        lease.release()


def _acquire_and_exit(lock_dir: str, output: multiprocessing.Queue[bool]) -> None:
    GpuLease.acquire(("GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",), 1, Path(lock_dir))
    output.put(True)


@pytest.mark.parametrize(
    ("gpu_type", "model"),
    [
        ("H100", "NVIDIA H100 80GB HBM3"),
        ("h100", "NVIDIA H100 NVL"),
        ("nvidia-h100-80gb", "NVIDIA H100 80GB HBM3"),
        ("RTX-PRO-6000", "NVIDIA RTX PRO 6000 Blackwell Server Edition"),
        ("RTX-4090", "NVIDIA GeForce RTX 4090"),
        ("L40S", "NVIDIA L40S"),
        ("a100-80gb", "NVIDIA A100-SXM4-80GB"),
        # Harbor's names whose words are not the model's.
        ("nvidia-tesla-t4", "Tesla T4"),
        ("nvidia-tesla-a100", "NVIDIA A100-PCIE-40GB"),
        ("h100-mega", "NVIDIA H100 80GB HBM3"),
        ("nvidia-h200-141gb", "NVIDIA H200"),
    ],
)
def test_a_gpu_type_accepts_the_model_nvidia_smi_names(gpu_type: str, model: str) -> None:
    assert accepts_model((gpu_type,), model)
    assert accepts_model(("T4", gpu_type), model)


@pytest.mark.parametrize(
    ("gpu_type", "model"),
    [
        ("H100", "NVIDIA H200"),
        ("A10", "NVIDIA A100-SXM4-80GB"),
        ("L4", "NVIDIA L40S"),
        ("a100-40gb", "NVIDIA A100-SXM4-80GB"),
        ("nvidia-h100-80gb", "NVIDIA H100 NVL"),
    ],
)
def test_a_gpu_type_accepts_no_other_model(gpu_type: str, model: str) -> None:
    assert not accepts_model((gpu_type,), model)


def test_naming_no_gpu_type_accepts_every_model() -> None:
    assert accepts_model((), "NVIDIA H100 80GB HBM3")


def test_gpu_identities_are_canonical() -> None:
    assert (
        normalize_nvidia_uuid("gpu-AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA")
        == "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    assert normalize_pci_bdf("0000:65:00.0") == "0000:65:00.0"
    with pytest.raises(ValueError):
        normalize_nvidia_uuid("0")
    with pytest.raises(ValueError):
        normalize_pci_bdf("65:00.0")


def test_acquisition_is_all_or_nothing_and_idempotent(tmp_path: Path) -> None:
    keys = (
        "GPU-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    lease = GpuLease.acquire(keys, 2, tmp_path)
    assert lease.device_keys == tuple(sorted(keys))
    with pytest.raises(ProviderCapabilityError, match="available"):
        GpuLease.acquire(keys, 1, tmp_path)
    lease.release()
    lease.release()
    GpuLease.acquire(keys, 2, tmp_path).release()


def test_lock_excludes_another_process(tmp_path: Path) -> None:
    key = "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    lease = GpuLease.acquire((key,), 1, tmp_path)
    output: multiprocessing.Queue[bool] = multiprocessing.Queue()
    process = multiprocessing.Process(target=_try_lease, args=(str(tmp_path), output))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0
    assert output.get(timeout=2) is False
    lease.release()


def test_process_exit_releases_the_kernel_lock(tmp_path: Path) -> None:
    key = "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    output: multiprocessing.Queue[bool] = multiprocessing.Queue()
    process = multiprocessing.Process(target=_acquire_and_exit, args=(str(tmp_path), output))
    process.start()
    assert output.get(timeout=2) is True
    process.join(timeout=10)
    assert process.exitcode == 0
    GpuLease.acquire((key,), 1, tmp_path).release()
