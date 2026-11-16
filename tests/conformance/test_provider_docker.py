"""The container backend against the shared provider contract."""

from __future__ import annotations

import asyncio
import os
import re
import time

import pytest
from tests.support import CONTAINER_BASE, skip_without_kata

from ale.core.errors import GuestUnreachableError
from ale.core.sandbox import SANDBOX_GATEWAY_PORT, Identity, ImageRef, SandboxRequest
from ale.core.taskspec import ContainerFeature, NetworkMode, NetworkPolicy, Resources
from ale.core.testkit import ProviderConformance
from ale.run import guestd
from ale.run.framework import framework_identity
from ale.run.providers import missing_features
from ale.run.providers.docker import kata
from ale.run.providers.docker.provider import (
    DockerProvider,
    DockerSandbox,
    _bridge_host_ip,
    _bridge_mac,
    _docker,
    _in_namespace,
)
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
    missing_features = staticmethod(missing_features)


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
    in_vm, kata_recorded, kernel = await _most_memory_mb(DockerProvider(runtime="kata"))
    assert (recorded, kata_recorded) == ("runc", kata.RUNTIME)
    assert kernel != host_kernel == os.uname().release, "the Kata sandbox shares the Host's kernel"
    # The guest kernel's work for the desktop is not charged to the container in a VM, so
    # Kata leaves somewhat more: 312-320 MiB against 240-288 MiB on runc when measured.
    assert runc - 32 <= in_vm <= runc + 160, f"runc held {runc} MiB, Kata {in_vm} MiB"


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_a_kata_sandbox_can_mount_only_when_it_asked_to() -> None:
    """Kata grants ``mount`` with a capability, which a Sandbox that did not ask lacks."""
    skip_without_kata()
    provider = DockerProvider(runtime="kata")
    request = SandboxRequest(
        episode_id="kata-unmounted",
        prepared_image=await provider.prepare_image(
            ImageRef(kind="container", reference=CONTAINER_BASE)
        ),
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
    )
    async with await provider.create(request) as sandbox:
        missing = await missing_features(sandbox, {ContainerFeature.MOUNT})
    assert set(missing) == {ContainerFeature.MOUNT}, missing
    asked = request.model_copy(
        update={
            "episode_id": "kata-mounted",
            "resources": Resources(
                cpus=1, memory_mb=512, container_features=(ContainerFeature.MOUNT,)
            ),
        }
    )
    async with await provider.create(asked) as sandbox:
        assert await missing_features(sandbox, {ContainerFeature.MOUNT}) == {}


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_a_kata_sandbox_resolves_names_while_open_and_fails_at_once_while_sealed() -> None:
    """On its own network the guest resolves through the Host's upstream servers, which
    Docker's embedded DNS server would keep out of its reach; sealed, a lookup fails at
    once, as on runc, rather than after a timeout."""
    skip_without_kata()
    provider = DockerProvider(runtime="kata")
    request = SandboxRequest(
        episode_id="kata-names",
        prepared_image=await provider.prepare_image(
            ImageRef(kind="container", reference=CONTAINER_BASE)
        ),
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
    )
    async with await provider.create(request) as sandbox:
        await sandbox.open_egress()
        lookup = await sandbox.exec(["getent", "hosts", "example.com"], identity=Identity.AGENT)
        assert lookup.ok, "an open sandbox resolves no name"
        await sandbox.close_egress()
        started = time.monotonic()
        lookup = await sandbox.exec(["getent", "hosts", "example.com"], identity=Identity.AGENT)
        assert lookup.exit_code != 0 and time.monotonic() - started < 10


async def _plain_request(
    provider: DockerProvider, episode: str, *features: ContainerFeature
) -> SandboxRequest:
    return SandboxRequest(
        episode_id=episode,
        prepared_image=await provider.prepare_image(
            ImageRef(kind="container", reference=CONTAINER_BASE)
        ),
        resources=Resources(cpus=1, memory_mb=512, container_features=features),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
    )


#: Sends, as root, ``count`` frames no sealed sandbox may get past its seal:
#: ``frame DST_MAC SRC_MAC SRC DST PORT`` a TCP SYN as a whole Ethernet frame on eth0,
#: ``arp DST_MAC SRC_MAC SENDER TARGET`` an ARP reply.
_FORGE = r"""
import socket, struct, sys

def checksum(data):
    data += bytes(len(data) % 2)
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF

def syn(src, dst, port):
    source, target = socket.inet_aton(src), socket.inet_aton(dst)
    tcp = struct.pack("!HHIIBBHHH", 40000, port, 1, 0, 5 << 4, 0x02, 65535, 0, 0)
    pseudo = source + target + struct.pack("!BBH", 0, 6, len(tcp))
    tcp = tcp[:16] + struct.pack("!H", checksum(pseudo + tcp)) + tcp[18:]
    head = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0, source, target)
    return head[:10] + struct.pack("!H", checksum(head)) + head[12:] + tcp

def mac(text):
    return bytes.fromhex(text.replace(":", ""))

kind, count, args = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
head = mac(args[0]) + mac(args[1])
if kind == "frame":
    frame = head + b"\x08\x00" + syn(args[2], args[3], int(args[4]))
else:
    sender, target = socket.inet_aton(args[2]), socket.inet_aton(args[3])
    reply = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 2) + mac(args[1]) + sender
    reply += bytes(6) + target
    frame = head + b"\x08\x06" + reply
with socket.socket(socket.AF_PACKET, socket.SOCK_RAW) as link:
    link.bind(("eth0", 0))
    for _ in range(count):
        link.send(frame)
print("sent")
"""
#: Connects to the fixed Gateway port, whatever answers there.
_DIAL = (
    "import socket\n"
    "try:\n"
    f"    socket.create_connection(('ale-gateway.internal', {SANDBOX_GATEWAY_PORT}), 5).close()\n"
    "except OSError:\n"
    "    pass\n"
)


async def _acted(container: str, device: str, hook: str) -> dict[int, int]:
    """How many frames each tc filter at ``device``'s ``hook`` has acted on, by
    preference."""
    code, shown, stderr = await _in_namespace(container, f"tc -s filter show dev {device} {hook}")
    assert code == 0, stderr
    counts: dict[int, int] = {}
    pref = 0
    for line in shown.splitlines():
        if line.startswith("filter ") and " pref " in line:
            pref = int(line.split(" pref ")[1].split()[0])
        elif found := re.search(r"Sent \d+ bytes (\d+) pkt", line):
            counts[pref] = max(counts.get(pref, 0), int(found[1]))
    return counts


async def _forged_past(
    sandbox: DockerSandbox, device: str, hook: str, *forgery: str, count: int = 20
) -> tuple[int, int]:
    """How many frames the seal's pass filters (10, 11, 20) passed and its drops (12, 21)
    dropped while ``count`` forged ones were sent, once they all reached the seal or ten
    seconds passed, since a frame can reach the device after the command that wrote it has
    returned. The sandbox's own ARP for the Host may pass meanwhile."""
    before = await _acted(sandbox.container, device, hook)
    sent = await sandbox.exec(
        [guestd.PYTHON["linux"], "-I", "-c", _FORGE, forgery[0], str(count), *forgery[1:]]
    )
    assert sent.ok, sent.stderr
    deadline = time.monotonic() + 10
    while True:
        after = await _acted(sandbox.container, device, hook)
        passed, dropped = (
            sum(after.get(pref, 0) - before.get(pref, 0) for pref in prefs)
            for prefs in ((10, 11, 20), (12, 21))
        )
        if passed + dropped >= count or time.monotonic() > deadline:
            return passed, dropped
        await asyncio.sleep(0.2)


async def _addresses(container: str, network: str = "bridge") -> tuple[str, str]:
    """A container's address and MAC on a network, Docker's default bridge unless named."""
    on = f'(index .NetworkSettings.Networks "{network}")'
    code, out, stderr = await _docker(
        "inspect", "--format", f"{{{{{on}.IPAddress}}}} {{{{{on}.MacAddress}}}}", container
    )
    assert code == 0, stderr
    ip, mac = out.split()
    return ip, mac


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_root_in_a_sealed_kata_vm_forges_nothing_past_its_seal() -> None:
    """Root in the VM writes any frame: one from another address to the Gateway's port, one
    to another MAC and an ARP reply claiming another address are dropped at the seal, while
    the sandbox's own frames still pass."""
    skip_without_kata()
    provider = DockerProvider(runtime="kata")
    request = (await _plain_request(provider, "kata-forge")).model_copy(
        update={"gateway_url": "http://127.0.0.1:41999"}
    )
    async with await provider.create(request) as sandbox:
        peers = await sandbox.confinement.peers(sandbox.container)
        host, ip, mac = peers.host_ip, peers.ip, peers.mac
        port = str(SANDBOX_GATEWAY_PORT)
        forgeries = [
            ("frame", peers.host_mac, mac, "172.17.255.250", host, port),
            ("frame", "02:42:ac:11:00:fe", mac, ip, host, port),
            ("arp", "ff:ff:ff:ff:ff:ff", mac, "172.17.0.254", host),
        ]
        for forgery in forgeries:
            passed, dropped = await _forged_past(sandbox, kata.TAP, "ingress", *forgery)
            assert passed <= 2 and dropped >= 10, (forgery, passed, dropped)
        before = await _acted(sandbox.container, kata.TAP, "ingress")
        await sandbox.exec(["python3", "-c", _DIAL], identity=Identity.AGENT)
        after = await _acted(sandbox.container, kata.TAP, "ingress")
        assert after.get(10, 0) > before.get(10, 0), "the sandbox's own frames no longer pass"


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_a_sealed_sandbox_gets_nothing_a_neighbour_forges_as_the_host() -> None:
    """A Kata sandbox boots on a network of its own, so a container with raw sockets on
    Docker's default bridge cannot reach its segment: a TCP SYN it writes as the Host, from
    its own MAC or the Host's, to the sandbox's MAC or through the Host, and an ARP reply
    that claims the Host's address never arrive, while the Host's own frames still do.
    Counted by tc filters on the sandbox's veth, whose ingress sees every frame that arrives
    there."""
    skip_without_kata()
    server = await asyncio.start_server(lambda _, writer: writer.close(), "0.0.0.0", 0)
    port = server.sockets[0].getsockname()[1]
    provider = DockerProvider(runtime="kata")
    request = (await _plain_request(provider, "kata-from-host")).model_copy(
        update={"gateway_url": f"http://127.0.0.1:{port}"}
    )
    forger = "ale-kata-host-forger"
    await _docker("rm", "-f", forger)
    code, _, stderr = await _docker(
        "run", "-d", "--name", forger, "--runtime", "runc", "--cap-add", "NET_RAW",
        "--entrypoint", "sleep", CONTAINER_BASE, "infinity",
    )  # fmt: skip
    assert code == 0, stderr
    try:
        async with server, await provider.create(request) as sandbox:
            peers = await sandbox.confinement.peers(sandbox.container)
            _, forger_mac = await _addresses(forger)
            through_host = await _bridge_mac("bridge", await _bridge_host_ip(None))
            counted = [
                f"protocol ip flower dst_ip {peers.ip} ip_proto tcp dst_port 2222",
                f"protocol arp flower arp_tip {peers.ip} arp_sha {forger_mac}",
            ]
            count = " && ".join(
                f"tc filter add dev eth0 ingress pref {pref} {spec} action continue"
                for pref, spec in enumerate(counted, 1)
            )
            code, _, stderr = await _in_namespace(sandbox.container, count)
            assert code == 0, stderr
            for forgery in (
                ("frame", peers.mac, forger_mac, peers.host_ip, peers.ip, "2222"),
                ("frame", peers.mac, peers.host_mac, peers.host_ip, peers.ip, "2222"),
                ("frame", through_host, forger_mac, peers.host_ip, peers.ip, "2222"),
                ("arp", peers.mac, forger_mac, peers.host_ip, peers.ip),
            ):
                code, _, stderr = await _docker(
                    "exec", "--user", "0", forger,
                    "python3", "-I", "-c", _FORGE, forgery[0], "20", *forgery[1:],
                )  # fmt: skip
                assert code == 0, stderr
            await asyncio.sleep(2)
            arrived = await _acted(sandbox.container, "eth0", "ingress")
            assert (arrived.get(1, 0), arrived.get(2, 0)) == (0, 0), arrived
            dial = (
                "import socket\n"
                f"socket.create_connection(('ale-gateway.internal', {SANDBOX_GATEWAY_PORT}), 10)"
                ".close()\n"
                "print('connected')\n"
            )
            reached = await sandbox.exec(["python3", "-c", dial], identity=Identity.AGENT)
            assert reached.stdout.strip() == "connected", reached.stderr
    finally:
        await _docker("rm", "-f", forger)


@pytest.mark.asyncio
async def test_a_docker_sandbox_killed_from_outside_says_so() -> None:
    provider = DockerProvider()
    async with await provider.create(await _plain_request(provider, "killed-runc")) as sandbox:
        running = asyncio.ensure_future(sandbox.exec(["sleep", "60"], timeout_sec=120))
        await asyncio.sleep(2)
        code, _, stderr = await _docker("kill", sandbox.container)
        assert code == 0, stderr
        with pytest.raises(GuestUnreachableError, match="itself died: it was killed from outside"):
            await running


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
    prepared = await prepare_task_image(task, DockerProvider())

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
