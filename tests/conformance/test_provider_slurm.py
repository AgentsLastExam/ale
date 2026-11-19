"""Slurm conformance on an explicitly configured cluster with a shared home directory."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest
from tests.support import CONTAINER_BASE

from ale.core.config import SlurmProviderConfig
from ale.core.sandbox import Identity, ImageRef, SandboxRequest
from ale.core.taskspec import Resources
from ale.core.testkit import ProviderConformance
from ale.run.framework import framework_identity
from ale.run.providers import missing_features
from ale.run.providers.slurm import SlurmProvider

pytestmark = [pytest.mark.conformance, pytest.mark.needs_slurm, pytest.mark.needs_docker]


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
        config = os.environ.get("ALE_TEST_SLURM_CONFIG")
        if not config:
            pytest.skip("set ALE_TEST_SLURM_CONFIG to a Run TOML with sandbox.slurm settings")
        settings = tomllib.loads(Path(config).read_text())["sandbox"]["slurm"]
        self.provider = SlurmProvider(SlurmProviderConfig.model_validate(settings))

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
