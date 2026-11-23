from __future__ import annotations

import os
from pathlib import Path

import pytest

from orbit.core.errors import ProviderCapabilityError
from orbit.core.sandbox import SandboxRequest
from orbit.core.taskspec import ImageKind, NetworkPolicy, Resources
from orbit.run.providers.qemu import QemuProvider
from tests.support import prepare_reference

IMAGE = Path(os.environ.get("ORBIT_TEST_QEMU_GPU_IMAGE", ""))
BDF = os.environ.get("ORBIT_TEST_QEMU_GPU_BDF", "")

pytestmark = [
    pytest.mark.needs_docker,
    pytest.mark.needs_kvm,
    pytest.mark.needs_gpu,
    pytest.mark.skipif(
        not BDF or not IMAGE.is_file(),
        reason="set ORBIT_TEST_QEMU_GPU_BDF and ORBIT_TEST_QEMU_GPU_IMAGE on a VFIO host",
    ),
]


@pytest.mark.asyncio
async def test_live_qemu_gpu_operation_attachment_and_exclusive_lease() -> None:
    provider = QemuProvider(image=IMAGE, gpu_devices=(BDF,))
    prepared = await prepare_reference(
        provider,
        "local://orbit-test-gpu-guest",
        kind=ImageKind.VM,
    )
    request = SandboxRequest(
        episode_id="live-qemu-gpu",
        prepared_image=prepared,
        resources=Resources(gpus=1),
        network=NetworkPolicy(),
    )
    sandbox = await provider.create(request)
    try:
        allocation = sandbox.allocation.gpu
        assert allocation is not None
        assert allocation.provider_addresses == (BDF.lower(),)
        assert len(allocation.observed_devices) == 1
        operation = await sandbox.exec(["nvidia-smi", "-L"])
        assert operation.ok, operation.stderr

        contender = QemuProvider(image=IMAGE, gpu_devices=(BDF,))
        with pytest.raises(ProviderCapabilityError, match="available"):
            await contender.create(request.model_copy(update={"episode_id": "gpu-contender"}))
    finally:
        await sandbox.destroy()
