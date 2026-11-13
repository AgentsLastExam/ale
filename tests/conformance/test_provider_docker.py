"""The container backend against the shared provider contract."""

from __future__ import annotations

import os
import time

import pytest
from tests.support import CONTAINER_BASE, skip_without_kata

from ale.core.config import RunConfig
from ale.core.sandbox import Identity, ImageRef, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.core.testkit import ProviderConformance
from ale.run.framework import framework_identity
from ale.run.providers import ProviderRegistry
from ale.run.providers.docker.provider import KATA_RUNTIME, DockerProvider, _docker
from ale.run.scaffold import scaffold_task
from ale.run.task_images import prepare_task_image
from ale.run.tasksets.manifest import load_tasks

pytestmark = [pytest.mark.conformance, pytest.mark.needs_docker]


class TestDockerProvider(ProviderConformance):
    """Runs every assertion in the shared suite against docker.

    Our own base image, because the suite exercises the sandbox contract — and an image
    that does not satisfy the image contract cannot satisfy it.
    """

    provider = DockerProvider()
    image = ImageRef(
        kind="container",
        reference=CONTAINER_BASE,
    )
    gui_image = ImageRef(
        kind="container",
        reference=CONTAINER_BASE,
    )
    #: The base carries no framework; the Provider adds it.
    framework = framework_identity(user="user", vm=False)


@pytest.mark.needs_kvm
class TestDockerKataProvider(TestDockerProvider):
    """The same suite with every sandbox in a Kata microVM, whose network the Provider
    confines with tc filters rather than an internal bridge."""

    provider = DockerProvider(runtime="kata")

    @pytest.fixture(autouse=True)
    def _kata(self) -> None:
        skip_without_kata()


#: Allocates and touches memory 8 MiB at a time until the kernel kills it, printing the
#: MiB held after each step.
_ALLOCATE = """
held = []
while True:
    held.append(b"x" * (8 << 20))
    print(len(held) * 8, flush=True)
"""


async def _most_memory_mb(provider: DockerProvider) -> tuple[int, str | None, str]:
    """The most one process held, the runtime the allocation records, and the kernel."""
    request = SandboxRequest(
        episode_id=f"memory-{provider.runtime}",
        prepared_image=await provider.prepare_image(
            ImageRef(kind="container", reference=CONTAINER_BASE)
        ),
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
    )
    async with await provider.create(request) as sandbox:
        result = await sandbox.exec(["python3", "-c", _ALLOCATE], timeout_sec=120)
        assert not result.ok, "a 512 MB sandbox held memory without limit"
        kernel = await sandbox.exec(["uname", "-r"])
        return int(result.stdout.split()[-1]), sandbox.allocation.runtime, kernel.stdout.strip()


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_a_kata_sandbox_is_a_vm_whose_processes_get_the_declared_memory() -> None:
    """The guest kernel's own memory is added to the VM, not taken from the Task's: a
    process holds at least as much as on runc, with the same image and its desktop."""
    skip_without_kata()
    runc, recorded, host_kernel = await _most_memory_mb(DockerProvider())
    kata, kata_recorded, kernel = await _most_memory_mb(DockerProvider(runtime="kata"))
    assert (recorded, kata_recorded) == ("runc", KATA_RUNTIME)
    assert kernel != host_kernel == os.uname().release, "the Kata sandbox shares the Host's kernel"
    # The guest kernel's work for the desktop is not charged to the container in a VM, so
    # Kata leaves somewhat more: 312-320 MiB against 240-288 MiB on runc when measured.
    assert runc - 32 <= kata <= runc + 160, f"runc held {runc} MiB, Kata {kata} MiB"


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_a_sealed_kata_sandbox_reaches_no_other_container_on_its_bridge() -> None:
    """A Kata sandbox shares the default bridge, so its seal must cover its neighbours."""
    skip_without_kata()
    image = ImageRef(kind="container", reference=CONTAINER_BASE)
    neighbour_provider, provider = DockerProvider(), DockerProvider(runtime="kata")
    neighbour_request = SandboxRequest(
        episode_id="kata-neighbour",
        prepared_image=await neighbour_provider.prepare_image(image),
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(mode=NetworkMode.OPEN),
    )
    request = neighbour_request.model_copy(
        update={
            "episode_id": "kata-sealed",
            "prepared_image": await provider.prepare_image(image),
            "network": NetworkPolicy(mode=NetworkMode.BLOCK),
        }
    )
    probe = (
        "import socket, sys\n"
        "try:\n"
        "    socket.create_connection((sys.argv[1], 8000), timeout=5).close(); print('open')\n"
        "except OSError:\n"
        "    print('closed')\n"
    )
    async with await neighbour_provider.create(neighbour_request) as neighbour:
        served = await neighbour.exec(
            ["sh", "-c", "nohup python3 -m http.server 8000 >/dev/null 2>&1 &"]
        )
        assert served.ok, served.stderr
        code, address, stderr = await _docker(
            "inspect", "--format", "{{.NetworkSettings.Networks.bridge.IPAddress}}",
            neighbour.container,
        )  # fmt: skip
        assert code == 0 and address.strip(), stderr
        async with await provider.create(request) as sandbox:
            await sandbox.open_egress()
            reached = await sandbox.exec(["python3", "-c", probe, address.strip()])
            assert reached.stdout.strip() == "open", "the probe cannot see the neighbour at all"
            await sandbox.close_egress()
            sealed = await sandbox.exec(
                ["python3", "-c", probe, address.strip()], identity=Identity.AGENT
            )
            assert sealed.stdout.strip() == "closed", "a sealed sandbox reached a neighbour"
            # As on runc, what the seal drops fails at once rather than after a timeout.
            started = time.monotonic()
            lookup = await sandbox.exec(["getent", "hosts", "example.com"], identity=Identity.AGENT)
            assert lookup.exit_code != 0 and time.monotonic() - started < 10


@pytest.mark.asyncio
async def test_blocked_network_has_no_route_off_the_host() -> None:
    """Deny-all is topological: an internal bridge simply has nowhere to go."""
    from ale.core.sandbox import SandboxRequest
    from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources

    provider = DockerProvider()
    prepared = await provider.prepare_image(
        ImageRef(
            kind="container",
            reference=CONTAINER_BASE,
        )
    )
    request = SandboxRequest(
        episode_id="netprobe",
        prepared_image=prepared,
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
    )
    async with await provider.create(request) as sandbox:
        result = await sandbox.exec(
            [
                "python3",
                "-c",
                "import socket;socket.setdefaulttimeout(5);"
                "socket.create_connection(('1.1.1.1', 443))",
            ],
            timeout_sec=30,
        )
        assert result.exit_code != 0, "sandbox reached the internet on a blocked network"


@pytest.mark.asyncio
async def test_proxy_only_sandbox_can_resolve_the_host_proxy() -> None:
    provider = DockerProvider()
    prepared = await provider.prepare_image(
        ImageRef(
            kind="container",
            reference=CONTAINER_BASE,
        )
    )
    request = SandboxRequest(
        episode_id="proxy-only",
        prepared_image=prepared,
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(mode="allowlist", allowed_hosts=("example.com",)),
        proxy_url="http://0.0.0.0:9443",
    )
    async with await provider.create(request) as sandbox:
        result = await sandbox.exec(
            ["python3", "-c", "import socket; print(socket.gethostbyname('ale-gateway.internal'))"],
            timeout_sec=30,
        )
    assert result.ok, result.stderr


@pytest.mark.asyncio
async def test_prepared_task_image_starts_without_remote_resolution(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = load_tasks(scaffold_task(tmp_path / "task"))[0]
    prepared = await prepare_task_image(task, ProviderRegistry(RunConfig()))

    async def remote_resolution_is_forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("prepared Task images must not use remote resolution")

    monkeypatch.setattr(
        "ale.run.providers.docker.provider.resolve_container_image",
        remote_resolution_is_forbidden,
    )
    provider = DockerProvider()
    request = SandboxRequest(
        episode_id="prepared",
        prepared_image=prepared,
        resources=Resources(memory_mb=512),
        network=NetworkPolicy(),
    )
    async with await provider.create(request) as sandbox:
        assert sandbox.resolved_image.prepared_identity == prepared.prepared_identity
