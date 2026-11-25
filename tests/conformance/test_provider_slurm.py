"""Slurm conformance, run on a login node of an explicitly configured cluster, which shares
Orbit's cache with its compute nodes."""

from __future__ import annotations

import pytest
from tests.support import CONTAINER_BASE, LIST_POLICIES, check_a_sealed_list, slurm_config

from orbit.core.sandbox import Identity, ImageRef, SandboxRequest
from orbit.core.taskspec import NetworkPolicy, Resources
from orbit.core.testkit import ProviderConformance
from orbit.run.framework import framework_identity
from orbit.run.providers import missing_features
from orbit.run.providers.slurm import SlurmProvider

pytestmark = [pytest.mark.conformance, pytest.mark.needs_slurm]


class TestSlurmProvider(ProviderConformance):
    image = ImageRef(kind="container", reference=CONTAINER_BASE)
    gui_image = image
    framework = framework_identity(user="user", vm=False)
    missing_features = staticmethod(missing_features)

    async def request(self, **overrides: object) -> SandboxRequest:
        # The desktop, guestd and the gVisor kernel all live in this one job.
        overrides.setdefault("resources", Resources(cpus=2, memory_mb=2048))
        return await super().request(**overrides)

    @pytest.fixture(autouse=True)
    def cluster(self) -> None:
        self.provider = SlurmProvider(slurm_config())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("policy", LIST_POLICIES)
    async def test_a_sealed_list_reaches_what_it_admits_through_the_relay(
        self, policy: NetworkPolicy
    ) -> None:
        await check_a_sealed_list(self.provider, self.request, policy)

    @pytest.mark.asyncio
    async def test_the_agent_can_allocate_cuda_memory_on_every_assigned_gpu(self) -> None:
        partitions = [part for part in self.provider.config.partitions if part.gpu_model]
        if not partitions:
            pytest.skip("no GPU partition configured")
        program = """
import ctypes as c
import sys

cuda = c.CDLL("libcuda.so.1")
def call(name, *args):
    result = getattr(cuda, name)(*args)
    assert result == 0, (name, result)

call("cuInit", 0)
count = c.c_int()
call("cuDeviceGetCount", c.byref(count))
assert count.value == int(sys.argv[1]), count.value
for index in range(count.value):
    device, context, memory = c.c_int(), c.c_void_p(), c.c_uint64()
    call("cuDeviceGet", c.byref(device), index)
    call("cuDevicePrimaryCtxRetain", c.byref(context), device)
    call("cuCtxSetCurrent", context)
    call("cuMemAlloc_v2", c.byref(memory), c.c_size_t(4096))
    call("cuMemFree_v2", memory)
    call("cuDevicePrimaryCtxRelease_v2", device)
"""
        for part in partitions:
            count = min(2, part.max_gpus)
            resources = Resources(
                cpus=2, memory_mb=2048, gpus=count, gpu_types=(part.gpu_model.name,)
            )
            async with await self.provider.create(
                await self.request(resources=resources)
            ) as sandbox:
                result = await sandbox.exec(
                    ["python3", "-c", program, str(count)], identity=Identity.AGENT
                )
                assert result.ok, result.stderr
