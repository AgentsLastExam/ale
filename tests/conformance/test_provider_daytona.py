"""The Daytona backend against the shared Provider contract.

Creates real, billable sandboxes on Daytona, so it runs only when ``ORBIT_DAYTONA_API_KEY``
is set and is selected with ``-m needs_daytona``. Daytona allocates whole GiB; the request
hook rounds the suite's small default resources up to that, which the Provider itself
refuses to do. ``ORBIT_TEST_DAYTONA_GPU=1`` also runs the suite on GPU sandboxes.
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

from orbit.core.sandbox import Identity, ImageRef, SandboxRequest
from orbit.core.taskspec import NetworkMode, NetworkPolicy, Resources
from orbit.core.testkit import ProviderConformance
from orbit.run.gateway.proxy import EgressProxy
from orbit.run.gateway.session import GatewaySession, SessionRegistry
from orbit.run.providers import missing_features
from orbit.run.providers.daytona.provider import DAEMON_PORTS, DaytonaProvider

pytestmark = [
    pytest.mark.conformance,
    pytest.mark.needs_docker,
    pytest.mark.needs_daytona,
    pytest.mark.skipif(
        not os.environ.get("ORBIT_DAYTONA_API_KEY"), reason="ORBIT_DAYTONA_API_KEY is not set"
    ),
]


#: What the agent reaches. Each check exchanges data: a GPU runner under ``networkBlockAll``
#: accepts a bare TCP connect to port 443 and passes nothing on. A host through the proxy is
#: reported with its certificate's issuer, which must be the host's own, not Daytona's.
ALLOWLIST_PROBE = r"""
import http.client, json, os, socket, ssl, sys, urllib.parse, urllib.request
proxy = urllib.parse.urlsplit(os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:9")
def through_proxy(name):
    connection = http.client.HTTPSConnection(
        proxy.hostname, proxy.port, timeout=15, context=ssl.create_default_context()
    )
    connection.set_tunnel(name, 443)
    try:
        connection.connect()
        issuer = dict(field[0] for field in connection.sock.getpeercert()["issuer"])
        return "open, issued by " + issuer.get("organizationName", "?")
    except OSError:
        return "closed"
    finally:
        connection.close()
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
    "example.com": through_proxy("example.com"),
    "www.example.com": through_proxy("www.example.com"),
    "one.one.one.one at 1.1.1.1": tls("1.1.1.1", "one.one.one.one"),
    "1.1.1.1 with no name": tls("1.1.1.1", None),
    "github.com resolves": resolves("github.com"),
    "gateway": fetch(sys.argv[1]),
    "proxy": proxy.hostname,
}))
"""


@contextlib.asynccontextmanager
async def _egress_proxy(policy: NetworkPolicy) -> AsyncIterator[tuple[str, str]]:
    """The Host's egress proxy, with a session for ``policy``: its URL and the token."""
    sessions = SessionRegistry()
    session = sessions.open(
        GatewaySession(episode_id="conformance", model="", purpose="egress", policy=policy)
    )
    proxy = EgressProxy(sessions, host="127.0.0.1")
    await proxy.start()
    try:
        yield proxy.url, session.token
    finally:
        await proxy.stop()


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
    gui_image = image  # the Orbit container base is a desktop image, and Daytona must run it
    missing_features = staticmethod(missing_features)

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
    async def test_allowlist_mode_reaches_the_declared_hosts_through_the_proxy_alone(
        self,
    ) -> None:
        """Sealed as ``block`` is, the sandbox reaches its hosts through the Host's proxy, which
        the tunnel carries in beside the Gateway; TLS is the host's own, end to end."""
        allowlist = NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",))
        async with _gateway() as port, _egress_proxy(allowlist) as (proxy, token):
            request = await self.request(
                network=allowlist,
                gateway_url=f"http://127.0.0.1:{port}",
                proxy_url=proxy,
                proxy_token=token,
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
                seen = json.loads(agent.stdout)
                issued = seen.pop("example.com")
                # Daytona's own allowlist re-signed with "netleash ephemeral CA".
                assert issued.startswith("open, issued by ") and "netleash" not in issued
                assert seen == {
                    "www.example.com": "closed",  # a name is that host alone
                    "one.one.one.one at 1.1.1.1": "closed",
                    "1.1.1.1 with no name": "closed",
                    "github.com resolves": "closed",
                    "gateway": "open",
                    "proxy": "127.0.0.1",  # guestd's relay
                }
                await sandbox.open_egress()  # verification gets the network back
                reopened = await sandbox.exec(["python3", "-c", ALLOWLIST_PROBE, "http://0:1"])
                assert json.loads(reopened.stdout)["one.one.one.one at 1.1.1.1"] == "open"

    @pytest.mark.asyncio
    async def test_blocklist_mode_reaches_all_but_its_hosts_through_the_proxy_alone(
        self,
    ) -> None:
        """Sealed as an allowlist is, the sandbox reaches every host but the listed ones
        through the Host's proxy, and nothing directly."""
        blocklist = NetworkPolicy(mode=NetworkMode.BLOCKLIST, blocked_hosts=("www.example.com",))
        async with _gateway() as port, _egress_proxy(blocklist) as (proxy, token):
            request = await self.request(
                network=blocklist,
                gateway_url=f"http://127.0.0.1:{port}",
                proxy_url=proxy,
                proxy_token=token,
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
                seen = json.loads(agent.stdout)
                issued = seen.pop("example.com")
                assert issued.startswith("open, issued by ") and "netleash" not in issued
                assert seen == {
                    "www.example.com": "closed",  # blocked
                    "one.one.one.one at 1.1.1.1": "closed",  # direct, not through the proxy
                    "1.1.1.1 with no name": "closed",
                    "github.com resolves": "closed",
                    "gateway": "open",
                    "proxy": "127.0.0.1",
                }

    @pytest.mark.asyncio
    async def test_a_pattern_reaches_its_name_and_the_names_below_it(self) -> None:
        """As Orbit's own proxy matches it, so a Task gets the same hosts on every Provider."""
        pattern = NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("*.example.com",))
        async with _gateway() as port, _egress_proxy(pattern) as (proxy, token):
            request = await self.request(
                network=pattern,
                gateway_url=f"http://127.0.0.1:{port}",
                proxy_url=proxy,
                proxy_token=token,
            )
            async with await self.provider.create(request) as sandbox:
                await sandbox.open_egress()
                await sandbox.close_egress()
                agent = await sandbox.exec(
                    ["python3", "-c", ALLOWLIST_PROBE, str(sandbox.gateway_url)],
                    identity=Identity.AGENT,
                    timeout_sec=120,
                )
                assert agent.ok, agent.stderr
                seen = json.loads(agent.stdout)
                assert seen["example.com"].startswith("open")
                assert seen["www.example.com"].startswith("open")
                assert seen["github.com resolves"] == "closed"


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

#: Loads the library the NVIDIA Vulkan ICD names and initializes EGL on every device through
#: GLVND, as an offscreen renderer does, so it needs nothing beyond the Orbit base. Prints
#: the vendors of the devices that initialized.
RENDERING = r"""
import ctypes, json
ctypes.CDLL("libGLX_nvidia.so.0")
egl = ctypes.CDLL("libEGL.so.1")
egl.eglGetProcAddress.restype = ctypes.c_void_p
egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
query_devices = ctypes.CFUNCTYPE(
    ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int)
)(egl.eglGetProcAddress(b"eglQueryDevicesEXT"))
pointer = ctypes.c_void_p
platform_display = ctypes.CFUNCTYPE(pointer, ctypes.c_uint, pointer, pointer)(
    egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
)
egl.eglInitialize.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
egl.eglQueryString.restype = ctypes.c_char_p
egl.eglQueryString.argtypes = [ctypes.c_void_p, ctypes.c_int]
devices, count = (ctypes.c_void_p * 16)(), ctypes.c_int()
query_devices(16, devices, ctypes.byref(count))
vendors = []
for device in devices[: count.value]:
    display = platform_display(0x313F, device, None)  # EGL_PLATFORM_DEVICE_EXT
    if egl.eglInitialize(display, None, None):
        vendors.append(egl.eglQueryString(display, 0x3053).decode())  # EGL_VENDOR
print(json.dumps(vendors))
"""


@pytest.mark.needs_gpu
@pytest.mark.skipif(
    os.environ.get("ORBIT_TEST_DAYTONA_GPU") != "1",
    reason="set ORBIT_TEST_DAYTONA_GPU=1 to run the suite on Daytona GPU sandboxes",
)
class TestDaytonaGpuProvider(TestDaytonaProvider):
    """Every assertion again on sandboxes with an H100, which Daytona runs in its GPU region,
    on other machines than the rest: the seal, the tunnel and the desktop must hold there too.
    """

    async def request(self, **overrides: object) -> SandboxRequest:
        resources = overrides.get("resources", Resources(cpus=1, memory_mb=1024))
        assert isinstance(resources, Resources)
        overrides["resources"] = resources.model_copy(
            update={"gpus": max(resources.gpus, 1), "gpu_types": resources.gpu_types or ("H100",)}
        )
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

    @pytest.mark.asyncio
    async def test_the_agent_finds_the_rendering_drivers(self) -> None:
        """EGL and Vulkan load the NVIDIA driver by the sonames GLVND and the ICD name."""
        async with await self.provider.create(await self.request()) as sandbox:
            found = await sandbox.exec(["python3", "-c", RENDERING], identity=Identity.AGENT)
            assert found.ok, found.stderr
            assert "NVIDIA" in json.loads(found.stdout), found.stdout
