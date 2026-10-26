"""The Daytona backend against the shared Provider contract.

Creates real, billable sandboxes on Daytona, so it runs only when ``DAYTONA_API_KEY`` is
set and is selected with ``-m needs_daytona``. Daytona allocates whole GiB; the request
hook rounds the suite's small default resources up to that, which the Provider itself
refuses to do. ``ALE_TEST_DAYTONA_GPU=1`` also runs the suite on GPU sandboxes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
from collections.abc import AsyncIterator

import pytest
from tests.support import CONTAINER_BASE

from ale.core.sandbox import Identity, ImageRef, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.core.testkit import ProviderConformance
from ale.run.providers.daytona import DAEMON_PORTS, DaytonaProvider

pytestmark = [
    pytest.mark.conformance,
    pytest.mark.needs_docker,
    pytest.mark.needs_daytona,
    pytest.mark.skipif(not os.environ.get("DAYTONA_API_KEY"), reason="DAYTONA_API_KEY is not set"),
]


#: What the agent reaches. Each check exchanges data: under Daytona's allowlist a bare TCP
#: connect to port 443 always succeeds, because Daytona's own proxy accepts it first.
ALLOWLIST_PROBE = r"""
import json, os, socket, ssl, sys, urllib.request
def tls(host, name):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, 443), timeout=8) as raw:
            with context.wrap_socket(raw, server_hostname=name):
                return "open"
    except OSError:
        return "closed"
def resolves(name):
    try:
        socket.getaddrinfo(name, 443)
        return "open"
    except OSError:
        return "closed"
def fetch(url):
    try:
        urllib.request.urlopen(url, timeout=10).read()
        return "open"
    except Exception:
        return "closed"
print(json.dumps({
    "example.com": tls("example.com", "example.com"),
    "www.example.com": tls("www.example.com", "www.example.com"),
    "one.one.one.one at 1.1.1.1": tls("1.1.1.1", "one.one.one.one"),
    "1.1.1.1 with no name": tls("1.1.1.1", None),
    "github.com resolves": resolves("github.com"),
    "gateway": fetch(sys.argv[1]),
    "proxy variables": ",".join(k for k in os.environ if k.lower().endswith("_proxy")) or "none",
}))
"""


@contextlib.asynccontextmanager
async def _gateway() -> AsyncIterator[int]:
    """A stand-in Gateway on the Host that answers any request with 200."""

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _whole_gib(megabytes: int) -> int:
    return math.ceil(megabytes / 1024) * 1024


class TestDaytonaProvider(ProviderConformance):
    """Every assertion in the shared suite, run against Daytona."""

    provider = DaytonaProvider()
    image = ImageRef(kind="container", reference=CONTAINER_BASE)
    gui_image = image  # the ALE container base is a desktop image, and Daytona must run it

    async def request(self, **overrides: object) -> SandboxRequest:
        resources = overrides.get("resources", Resources(cpus=1, memory_mb=1024))
        assert isinstance(resources, Resources)
        overrides["resources"] = resources.model_copy(
            update={
                "memory_mb": _whole_gib(resources.memory_mb),
                "storage_mb": (
                    None if resources.storage_mb is None else _whole_gib(resources.storage_mb)
                ),
            }
        )
        return await super().request(**overrides)

    @pytest.mark.asyncio
    async def test_the_agent_cannot_reach_the_platform_daemon(self) -> None:
        """Daytona's daemon takes commands without authentication and runs them as root."""
        probe = (
            "import socket, sys\n"
            "for host, family in (('127.0.0.1', socket.AF_INET), ('::1', socket.AF_INET6)):\n"
            "    for port in map(int, sys.argv[1:]):\n"
            "        try:\n"
            "            socket.socket(family).connect((host, port))\n"
            "            print('open', host, port)\n"
            "        except OSError:\n"
            "            pass\n"
        )
        ports = [str(port) for port in DAEMON_PORTS]
        async with await self.provider.create(await self.request()) as sandbox:
            agent = await sandbox.exec(["python3", "-c", probe, *ports], identity=Identity.AGENT)
            assert agent.ok and agent.stdout.strip() == "", agent.stdout
            framework = await sandbox.exec(["python3", "-c", probe, *ports])
            assert "open 127.0.0.1 2280" in framework.stdout  # the daemon is there for root

    @pytest.mark.asyncio
    async def test_no_credential_of_the_host_is_in_the_sandbox(self) -> None:
        request = await self.request()
        async with await self.provider.create(request) as sandbox:
            hunt = await sandbox.exec(
                ["sh", "-c", "env | grep -ciE 'daytona_api|api_key|secret' || true"]
            )
            assert hunt.stdout.strip() == "0", "a credential reached the sandbox environment"

    @pytest.mark.asyncio
    async def test_allowlist_mode_reaches_the_declared_hosts_directly_and_nothing_else(
        self,
    ) -> None:
        """Daytona enforces the allowlist itself, by name, so allowed traffic never passes
        through the Host. The Gateway still comes through the tunnel, and no proxy is set."""
        async with _gateway() as port:
            request = await self.request(
                network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)),
                gateway_url=f"http://127.0.0.1:{port}",
                proxy_url="http://127.0.0.1:9",  # the engine passes one; Daytona does not use it
                proxy_token="t" * 32,
            )
            async with await self.provider.create(request) as sandbox:
                await sandbox.open_egress()  # setup, as an episode runs it
                await sandbox.close_egress()
                agent = await sandbox.exec(
                    ["python3", "-c", ALLOWLIST_PROBE, str(sandbox.gateway_url)],
                    identity=Identity.AGENT,
                    timeout_sec=120,
                )
                assert agent.ok, agent.stderr
                assert json.loads(agent.stdout) == {
                    "example.com": "open",
                    "www.example.com": "open",  # a subdomain, as ALE's allowlist matches
                    "one.one.one.one at 1.1.1.1": "closed",
                    "1.1.1.1 with no name": "closed",
                    "github.com resolves": "closed",
                    "gateway": "open",
                    "proxy variables": "none",
                }
                await sandbox.open_egress()  # verification gets the network back
                reopened = await sandbox.exec(["python3", "-c", ALLOWLIST_PROBE, "http://0:1"])
                assert json.loads(reopened.stdout)["one.one.one.one at 1.1.1.1"] == "open"


#: Initializes CUDA through the driver alone, so it needs no toolkit in the image, and
#: allocates memory on the device to prove the GPU is usable, not only listed.
CUDA = r"""
import ctypes
cuda = ctypes.CDLL("libcuda.so.1")
assert cuda.cuInit(0) == 0, "cuInit failed"
count = ctypes.c_int()
assert cuda.cuDeviceGetCount(ctypes.byref(count)) == 0
device, context, memory = ctypes.c_int(), ctypes.c_void_p(), ctypes.c_uint64()
assert cuda.cuDeviceGet(ctypes.byref(device), 0) == 0
assert cuda.cuCtxCreate_v2(ctypes.byref(context), 0, device) == 0, "no CUDA context"
assert cuda.cuMemAlloc_v2(ctypes.byref(memory), 1 << 30) == 0, "could not allocate 1 GiB"
assert cuda.cuMemFree_v2(memory) == 0
print(count.value)
"""


@pytest.mark.needs_gpu
@pytest.mark.skipif(
    os.environ.get("ALE_TEST_DAYTONA_GPU") != "1",
    reason="set ALE_TEST_DAYTONA_GPU=1 to run the suite on Daytona GPU sandboxes",
)
class TestDaytonaGpuProvider(TestDaytonaProvider):
    """Every assertion again on sandboxes with a GPU, which Daytona runs in its GPU region,
    on other machines than the rest: the seal, the tunnel and the desktop must hold there too.
    """

    async def request(self, **overrides: object) -> SandboxRequest:
        resources = overrides.get("resources", Resources(cpus=1, memory_mb=1024))
        assert isinstance(resources, Resources)
        overrides["resources"] = resources.model_copy(update={"gpus": max(resources.gpus, 1)})
        return await super().request(**overrides)

    @pytest.mark.asyncio
    async def test_the_agent_computes_on_the_gpu_the_lock_records(self) -> None:
        async with await self.provider.create(await self.request()) as sandbox:
            allocation = sandbox.allocation.gpu
            assert allocation is not None and allocation.requested_count == 1
            (device,) = allocation.observed_devices
            assert "H100" in device.model, device.model
            used = await sandbox.exec(["python3", "-c", CUDA], identity=Identity.AGENT)
            assert used.ok and used.stdout.strip() == "1", used.stderr
            listed = await sandbox.exec(
                ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
                identity=Identity.AGENT,
            )
            assert listed.stdout.strip() == device.id
