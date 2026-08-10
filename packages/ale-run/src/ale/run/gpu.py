"""Exclusive physical NVIDIA GPU leases."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import re
import uuid
from pathlib import Path
from typing import BinaryIO, Literal

from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import GpuDevice

_UUID = re.compile(
    r"^GPU-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)
_BDF = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$", re.IGNORECASE)


def normalize_nvidia_uuid(value: str) -> str:
    match = _UUID.fullmatch(value.strip())
    if not match:
        raise ValueError(f"invalid NVIDIA GPU UUID: {value!r}")
    return f"GPU-{match.group(1).lower()}"


def normalize_pci_bdf(value: str) -> str:
    value = value.strip().lower()
    if not _BDF.fullmatch(value):
        raise ValueError(f"invalid PCI BDF: {value!r}")
    return value


def parse_nvidia_smi(output: str) -> tuple[GpuDevice, ...]:
    devices: list[GpuDevice] = []
    try:
        rows = csv.reader(io.StringIO(output), skipinitialspace=True)
        for row in rows:
            if not row:
                continue
            if len(row) != 4:
                raise ValueError(f"expected 4 columns, got {len(row)}")
            gpu_uuid, model, pci_address, driver_version = (item.strip() for item in row)
            devices.append(
                GpuDevice(
                    id=normalize_nvidia_uuid(gpu_uuid),
                    model=model,
                    pci_address=pci_address,
                    driver_version=driver_version,
                )
            )
    except (ValueError, TypeError) as exc:
        raise ProviderCapabilityError(f"could not parse nvidia-smi output: {exc}") from exc
    if not devices:
        raise ProviderCapabilityError("nvidia-smi reported no physical GPUs")
    return tuple(devices)


class GpuLease:
    """Open file descriptors are the lease; process exit releases them."""

    def __init__(self, keys: tuple[str, ...], handles: tuple[BinaryIO, ...]) -> None:
        self.lease_id = uuid.uuid4().hex
        self.device_keys = keys
        self._handles = handles
        self.state: Literal["acquired", "attached", "released"] = "acquired"

    @classmethod
    def acquire(cls, candidates: tuple[str, ...], count: int, lock_dir: Path) -> GpuLease:
        if count < 1:
            raise ValueError("GPU lease count must be positive")
        keys = tuple(sorted(set(candidates)))
        if len(keys) < count:
            raise ProviderCapabilityError(
                f"task needs {count} GPUs but only {len(keys)} candidates are configured"
            )
        lock_dir.mkdir(parents=True, exist_ok=True)
        selected: list[str] = []
        handles: list[BinaryIO] = []
        for key in keys:
            path = lock_dir / f"{hashlib.sha256(key.encode()).hexdigest()}.lock"
            handle = path.open("a+b")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            selected.append(key)
            handles.append(handle)
            if len(selected) == count:
                return cls(tuple(selected), tuple(handles))
        for handle in handles:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        raise ProviderCapabilityError(
            f"task needs {count} GPUs but only {len(selected)} are currently available"
        )

    def attach(self) -> None:
        if self.state == "acquired":
            self.state = "attached"

    def release(self) -> None:
        if self.state == "released":
            return
        for handle in self._handles:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        self._handles = ()
        self.state = "released"
