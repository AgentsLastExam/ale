from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from ale.core.errors import ProviderCapabilityError
from ale.run.gpu import GpuLease, normalize_nvidia_uuid, normalize_pci_bdf

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
