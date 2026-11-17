"""The container backend against the shared provider contract."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Literal

import pytest
from tests.support import CONTAINER_BASE, skip_without_gvisor, skip_without_kata

from ale.core.errors import GuestUnreachableError, MemoryExhaustedError
from ale.core.sandbox import SANDBOX_GATEWAY_PORT, Identity, ImageRef, SandboxRequest
from ale.core.taskspec import ContainerFeature, NetworkMode, NetworkPolicy, Resources
from ale.core.testkit import NESTED_CHECKS, ProviderConformance
from ale.run import guestd
from ale.run.framework import framework_identity
from ale.run.providers import missing_features
from ale.run.providers.docker import gvisor, kata, storage
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
    #: A measurement after the write, and its kill, on a loaded Host.
    storage_sec = storage.WATCH_SEC + 15


@pytest.mark.needs_kvm
class TestDockerKataProvider(TestDockerProvider):
    """The same suite with every sandbox in a Kata microVM, whose network the Provider
    confines with tc filters rather than an internal bridge."""

    provider = DockerProvider(runtime="kata")

    @pytest.fixture(autouse=True)
    def _kata(self) -> None:
        skip_without_kata()


class TestDockerGvisorProvider(TestDockerProvider):
    """The same suite with every sandbox on gVisor, whose network the Provider confines with
    tc filters on the veth's egress and ALE's chains in netstack; the container feature test
    runs one sandbox under the runtime registered for raw and packet sockets and one under
    the runtime registered for container engines."""

    provider = DockerProvider(runtime="gvisor")

    @pytest.fixture(autouse=True)
    def _gvisor(self) -> None:
        skip_without_gvisor()


#: Allocates and touches memory 8 MiB at a time until the kernel kills it, printing the
#: MiB held after each step.
_ALLOCATE = """
held = []
while True:
    held.append(b"x" * (8 << 20))
    print(len(held) * 8, flush=True)
"""


#: Ends every process of a sandbox of the container base but ALE's own, which run on the
#: framework's Python (guestd and, under gVisor, its memory watcher), its command's and
#: supervisord, which reloads with no programs: the desktop's memory, which varies by about
#: 50 MiB from run to run on a loaded Host, then leaves what one process holds alone.
_QUIET = """
import glob, os, signal, time
for conf in glob.glob("/etc/supervisor/conf.d/*.conf"):
    os.remove(conf)
os.kill(1, signal.SIGHUP)
time.sleep(3)
keep = {1, os.getpid(), os.getppid()}
for _ in range(10):
    left = []
    for name in os.listdir("/proc"):
        if name.isdigit() and int(name) not in keep:
            try:
                state = open(f"/proc/{name}/stat").read().rsplit(")", 1)[1].split()[0]
                command = open(f"/proc/{name}/cmdline", "rb").read()
                # A zombie, such as a child guestd has yet to reap, holds no memory.
                if state != "Z" and not command.startswith(b"/opt/ale/python/"):
                    left.append(name)
                    os.kill(int(name), signal.SIGKILL)
            except OSError:
                pass
    if not left:
        break
    time.sleep(1)
else:
    raise SystemExit(f"still running: {left}")
"""


async def _most_memory_mb(
    provider: DockerProvider, *, quiet: bool = False, memory_mb: int = 512
) -> tuple[int, str | None, str]:
    """The most one process held in a Sandbox of ``memory_mb``, beside the desktop or,
    ``quiet``, alone, the runtime the allocation records, and the kernel."""
    request = SandboxRequest(
        episode_id=f"memory-{provider.runtime}-{memory_mb}",
        prepared_image=await provider.prepare_image(
            ImageRef(kind="container", reference=CONTAINER_BASE)
        ),
        resources=Resources(cpus=1, memory_mb=memory_mb),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
    )
    async with await provider.create(request) as sandbox:
        if quiet:
            stopped = await sandbox.exec(["python3", "-c", _QUIET], timeout_sec=120)
            assert stopped.ok, stopped.stdout + stopped.stderr
        result = await sandbox.exec(["python3", "-c", _ALLOCATE], timeout_sec=600)
        assert not result.ok, f"a {memory_mb} MB sandbox held memory without limit"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mb", [512, 2048, 4096, 8192])
async def test_processes_in_a_gvisor_sandbox_get_the_declared_memory(memory_mb: int) -> None:
    """The Sentry's own memory is added to the container's limit, not taken from the Task's:
    with the desktop ended, one process holds what it holds under runc, give or take the
    allocator's 8 MiB steps and the Sentry's own memory, which varies by up to 48 MiB from run
    to run."""
    skip_without_gvisor()
    runc, recorded, host_kernel = await _most_memory_mb(
        DockerProvider(), quiet=True, memory_mb=memory_mb
    )
    on_gvisor, gvisor_recorded, kernel = await _most_memory_mb(
        DockerProvider(runtime="gvisor"), quiet=True, memory_mb=memory_mb
    )
    assert (recorded, gvisor_recorded) == ("runc", gvisor.RUNTIME)
    assert kernel != host_kernel, "the gVisor sandbox reports the Host's kernel"
    assert abs(on_gvisor - runc) <= 32, f"runc held {runc} MiB, gVisor {on_gvisor} MiB"


@pytest.mark.asyncio
async def test_a_gvisor_sandbox_has_mount_and_raw_sockets_only_when_it_asked() -> None:
    """``mount`` is a capability and ``raw_sockets`` the raw sockets runtime, which a
    Sandbox that did not ask lacks; the container's memory holds the Sentry's beside the
    Task's."""
    skip_without_gvisor()
    provider = DockerProvider(runtime="gvisor")
    wanted = {ContainerFeature.MOUNT, ContainerFeature.RAW_SOCKETS}
    async with await provider.create(await _plain_request(provider, "gvisor-plain")) as sandbox:
        assert set(await missing_features(sandbox, wanted)) == wanted
        assert sandbox.allocation.runtime == gvisor.RUNTIME
        code, limit, stderr = await _docker(
            "inspect", "--format", "{{.HostConfig.Memory}}", sandbox.container
        )
        assert code == 0 and int(limit) == gvisor.memory_mb(512) << 20, stderr
    asked = await _plain_request(provider, "gvisor-asked", *sorted(wanted))
    async with await provider.create(asked) as sandbox:
        assert await missing_features(sandbox, wanted) == {}
        assert sandbox.allocation.runtime == gvisor.NET_RAW


#: Sends, as root, ``count`` frames no sealed sandbox may get past its seal:
#: ``ip SRC DST PORT`` a TCP SYN from a raw IP socket,
#: ``frame DST_MAC SRC_MAC SRC DST PORT`` the same as a whole Ethernet frame on eth0,
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
if kind == "ip":
    with socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW) as raw:
        for _ in range(count):
            raw.sendto(syn(args[0], args[1], int(args[2])), (args[1], 0))
else:
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


@pytest.mark.asyncio
async def test_a_sealed_gvisor_sandbox_with_raw_sockets_forges_nothing_past_its_seal() -> None:
    """Root with raw sockets writes any IP packet: one from another address to the Gateway's
    port is dropped at the seal, while the sandbox's own pass. The sandbox dials first, so
    netstack has resolved the Host's address and sends what is forged at once: while it
    resolves, it drops most of what it is given."""
    skip_without_gvisor()
    provider = DockerProvider(runtime="gvisor")
    request = (
        await _plain_request(provider, "gvisor-forge", ContainerFeature.RAW_SOCKETS)
    ).model_copy(update={"gateway_url": "http://127.0.0.1:41999"})
    async with await provider.create(request) as sandbox:
        before = await _acted(sandbox.container, gvisor.VETH, "egress")
        await sandbox.exec(["python3", "-c", _DIAL], identity=Identity.AGENT)
        after = await _acted(sandbox.container, gvisor.VETH, "egress")
        assert after.get(10, 0) > before.get(10, 0), "the sandbox's own frames do not pass"
        host = sandbox.confinement.host_ip
        passed, dropped = await _forged_past(
            sandbox, gvisor.VETH, "egress", "ip", "172.17.255.250", host, "41999"
        )
        assert passed <= 2 and dropped >= 10, (passed, dropped)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [pytest.param("kata", marks=pytest.mark.needs_kvm), "gvisor"])
async def test_a_sealed_sandbox_gets_nothing_a_neighbour_forges_as_the_host(
    runtime: Literal["kata", "gvisor"],
) -> None:
    """A Kata or gVisor sandbox boots on a network of its own, so a container with raw
    sockets on Docker's default bridge cannot reach its segment: a TCP SYN it writes as the
    Host, from its own MAC or the Host's, to the sandbox's MAC or through the Host, and an
    ARP reply that claims the Host's address never arrive, while the Host's own frames still
    do. Counted by tc filters on the sandbox's veth, whose ingress sees every frame that
    arrives there, whether or not netstack has read it."""
    (skip_without_kata if runtime == "kata" else skip_without_gvisor)()
    server = await asyncio.start_server(lambda _, writer: writer.close(), "0.0.0.0", 0)
    port = server.sockets[0].getsockname()[1]
    provider = DockerProvider(runtime=runtime)
    request = (await _plain_request(provider, f"{runtime}-from-host")).model_copy(
        update={"gateway_url": f"http://127.0.0.1:{port}"}
    )
    forger = f"ale-{runtime}-host-forger"
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
async def test_a_sealed_gvisor_sandbox_with_its_chains_flushed_still_reaches_nothing() -> None:
    """The chains in netstack only make refusals fast; the filters outside are the boundary.
    With the chains flushed, the Internet and a raw ICMP echo stay out of reach."""
    skip_without_gvisor()
    provider = DockerProvider(runtime="gvisor")
    request = await _plain_request(provider, "gvisor-flushed", ContainerFeature.RAW_SOCKETS)
    reach = (
        "import socket, struct\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=5).close(); print('tcp open')\n"
        "except OSError:\n"
        "    print('tcp closed')\n"
        "with socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP) as icmp:\n"
        "    icmp.settimeout(5)\n"
        "    body = struct.pack('!BBHHH', 8, 0, 0, 7, 1) + b'ale'\n"
        "    words = sum(struct.unpack('!5H', body[:10])) + (body[10] << 8)\n"
        "    total = (words & 0xFFFF) + (words >> 16)\n"
        "    body = body[:2] + struct.pack('!H', ~total & 0xFFFF) + body[4:]\n"
        "    icmp.sendto(body, ('1.1.1.1', 0))\n"
        "    try:\n"
        "        icmp.recv(1024); print('icmp open')\n"
        "    except OSError:\n"
        "        print('icmp closed')\n"
    )
    async with await provider.create(request) as sandbox:
        await sandbox.open_egress()
        opened = await sandbox.exec(["python3", "-c", reach], timeout_sec=60)
        assert opened.stdout.split("\n")[:2] == ["tcp open", "icmp open"], opened.stdout
        await sandbox.close_egress()
        # What root holding CAP_NET_ADMIN in netstack could do.
        flush = gvisor.set_netstack(sandbox.container, {"tables": {"filter": {}, "nat": {}}})
        code, _, stderr = await _docker(*flush, timeout=60)
        assert code == 0, stderr
        sealed = await sandbox.exec(["python3", "-c", reach], timeout_sec=60)
        assert sealed.stdout.split("\n")[:2] == ["tcp closed", "icmp closed"], sealed.stdout


@pytest.mark.asyncio
async def test_a_gvisor_sandbox_resolves_names_while_open_and_fails_at_once_while_sealed() -> None:
    """On its own network netstack resolves through the Host's upstream servers, where
    Docker's embedded DNS server would answer nothing; sealed, a connection off the Host and
    a lookup are refused at once."""
    skip_without_gvisor()
    provider = DockerProvider(runtime="gvisor")
    request = await _plain_request(provider, "gvisor-names")
    probe = (
        "import socket, time\n"
        "started = time.monotonic()\n"
        "try:\n"
        "    socket.create_connection(('192.0.2.1', 8000), timeout=5).close(); print('open')\n"
        "except OSError:\n"
        "    print('closed', round(time.monotonic() - started, 2))\n"
    )
    async with await provider.create(request) as sandbox:
        await sandbox.open_egress()
        lookup = await sandbox.exec(["getent", "hosts", "example.com"], identity=Identity.AGENT)
        assert lookup.ok, "an open sandbox resolves no name"
        await sandbox.close_egress()
        sealed = await sandbox.exec(["python3", "-c", probe], identity=Identity.AGENT)
        assert sealed.stdout.split()[:1] == ["closed"], (
            f"a sealed sandbox reached past the Host: {sealed.stdout}{sealed.stderr}"
        )
        _, seconds = sealed.stdout.split()
        assert float(seconds) < 1, "the seal's refusal waited for a timeout"
        started = time.monotonic()
        lookup = await sandbox.exec(["getent", "hosts", "example.com"], identity=Identity.AGENT)
        assert lookup.exit_code != 0 and time.monotonic() - started < 10


#: Docker CE from Docker's own repository, as a Task's image preinstalls it.
_DOCKER_CE = f"""\
FROM {CONTAINER_BASE}
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \\
    && install -m 0755 -d /etc/apt/keyrings \\
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \\
    && echo "deb [signed-by=/etc/apt/keyrings/docker.asc]" \\
        "https://download.docker.com/linux/ubuntu jammy stable" \\
        >/etc/apt/sources.list.d/docker.list \\
    && apt-get update && apt-get install -y --no-install-recommends \\
        docker-ce docker-ce-cli containerd.io \\
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /home/user/output && chown -R user:user /home/user/output
"""
#: Starts a container engine's daemon, by the command ``START``, and waits for it.
_START_ENGINE = """
eval "$START" >/tmp/engine-start.log 2>&1
for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info --format '{{.ServerVersion}}'
"""
#: Installs Docker from the image's distribution and starts its daemon by hand.
_INSTALL_ENGINE = """
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq docker.io >/dev/null || exit 2
(dockerd >/tmp/dockerd.log 2>&1 &)
for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker pull -q busybox:1.36.1 >/dev/null
"""


async def _volumes(container: str) -> dict[str, str]:
    """The Docker volumes a container mounts, by the directory each is mounted at."""
    code, raw, stderr = await _docker(
        "inspect", "--format", "{{json .Mounts}}", container, timeout=30
    )
    assert code == 0, stderr
    return {m["Destination"]: m["Name"] for m in json.loads(raw) if m["Type"] == "volume"}


@pytest.mark.asyncio
async def test_a_gvisor_sandbox_with_a_container_engine_keeps_its_storage_on_volumes() -> None:
    """Under the runtime registered for container engines, which may mount, with each of
    the engines' storage directories on a volume of its own that goes with the Sandbox. A
    Sandbox that did not ask has none of it."""
    skip_without_gvisor()
    provider = DockerProvider(runtime="gvisor")
    async with await provider.create(await _plain_request(provider, "gvisor-no-engine")) as sandbox:
        assert sandbox.allocation.runtime == gvisor.RUNTIME
        assert await _volumes(sandbox.container) == {}
    asked = await _plain_request(provider, "gvisor-engine", ContainerFeature.NESTED_CONTAINERS)
    async with await provider.create(asked) as sandbox:
        assert sandbox.allocation.runtime == gvisor.NESTED
        assert await missing_features(sandbox, {ContainerFeature.MOUNT}) == {}
        volumes = await _volumes(sandbox.container)
        assert set(volumes) == set(gvisor.ENGINE_STORAGE)
    for name in volumes.values():
        code, _, _ = await _docker("volume", "inspect", name, timeout=30)
        assert code != 0, f"volume {name} outlived its Sandbox"


@pytest.mark.asyncio
@pytest.mark.parametrize("start", ["service docker start", "(dockerd >/tmp/dockerd.log 2>&1 &)"])
async def test_a_gvisor_sandbox_runs_the_docker_its_image_has(tmp_path, start: str) -> None:
    """Docker CE preinstalled in a Task's image, started by its init script or by hand,
    runs containers as the shared suite shows of Docker installed during the run."""
    skip_without_gvisor()
    root = scaffold_task(tmp_path / "docker-ce")
    (root / "environment" / "Dockerfile").write_text(_DOCKER_CE)
    provider = DockerProvider(runtime="gvisor")
    request = SandboxRequest(
        episode_id="gvisor-docker-ce",
        prepared_image=await prepare_task_image(load_tasks(root)[0], provider),
        resources=Resources(
            cpus=2, memory_mb=4096, container_features=(ContainerFeature.NESTED_CONTAINERS,)
        ),
        network=NetworkPolicy(mode=NetworkMode.OPEN),
    )
    async with await provider.create(request) as sandbox:
        started = await sandbox.exec(
            ["sh", "-c", _START_ENGINE], env={"START": start}, timeout_sec=120
        )
        assert started.ok and started.stdout.strip(), started.stderr[-2000:]
        checked = await sandbox.exec(["sh", "-c", NESTED_CHECKS], timeout_sec=600)
        assert "nested-container-ok" in checked.stdout, checked.stderr[-2000:]


@pytest.mark.asyncio
async def test_a_sealed_gvisor_sandbox_running_containers_reaches_nothing_past_its_seal() -> None:
    """The engine's bridges, rules and NAT are inside netstack, and the filters outside still
    bind what its containers send: open, a container reaches the Internet; sealed, it does
    not, nor once root, who holds ``CAP_NET_ADMIN`` here, has flushed ALE's chains."""
    skip_without_gvisor()
    provider = DockerProvider(runtime="gvisor")
    request = await _plain_request(
        provider, "gvisor-engine-seal", ContainerFeature.NESTED_CONTAINERS
    )
    request = request.model_copy(
        update={"resources": request.resources.model_copy(update={"memory_mb": 2048})}
    )
    reach = (
        "docker run --rm busybox:1.36.1 sh -c "
        "'nc -w 5 1.1.1.1 443 </dev/null && echo open || echo closed'"
    )
    async with await provider.create(request) as sandbox:
        await sandbox.open_egress()
        installed = await sandbox.exec(["sh", "-c", _INSTALL_ENGINE], timeout_sec=600)
        assert installed.ok, installed.stderr[-2000:]
        opened = await sandbox.exec(["sh", "-c", reach], timeout_sec=60)
        assert opened.stdout.strip() == "open", opened.stdout + opened.stderr
        await sandbox.close_egress()
        sealed = await sandbox.exec(["sh", "-c", reach], timeout_sec=60)
        assert sealed.stdout.strip() == "closed", sealed.stdout + sealed.stderr
        flush = gvisor.set_netstack(sandbox.container, {"tables": {"filter": {}, "nat": {}}})
        code, _, stderr = await _docker(*flush, timeout=60)
        assert code == 0, stderr
        flushed = await sandbox.exec(["sh", "-c", reach], timeout_sec=60)
        assert flushed.stdout.strip() == "closed", flushed.stdout + flushed.stderr


#: Forks 16 MiB processes until the kernel ends the sandbox.
_MANY = """
import os, time
for _ in range(400):
    if os.fork() == 0:
        held = bytearray(16 << 20)
        for page in range(0, len(held), 4096):
            held[page] = 1
        time.sleep(600)
        os._exit(0)
    time.sleep(0.02)
time.sleep(600)
"""


@pytest.mark.asyncio
async def test_a_runc_sandbox_whose_processes_take_its_memory_dies_of_it() -> None:
    """The OOM killer ends its processes, largest first, up to the container's main process,
    and the sandbox goes. Docker records the kills."""
    provider = DockerProvider()
    async with await provider.create(await _plain_request(provider, "many-runc")) as sandbox:
        with pytest.raises(MemoryExhaustedError) as lost:
            await sandbox.exec(["python3", "-c", _MANY], identity=Identity.AGENT, timeout_sec=300)
    assert str(lost.value).startswith(
        "the sandbox itself died after an OOM kill in it (its Task declares 512 MiB): Docker "
        "recorded OOMKilled, and its main process exited with code "
    )


@pytest.mark.asyncio
async def test_a_gvisor_sandbox_whose_processes_take_its_memory_loses_them_one_by_one() -> None:
    """The Sentry ends them one at a time below the container's limit on the Host, as a guest
    kernel would, and counts each; the sandbox, guestd in it and the forking process live on,
    as under Kata."""
    skip_without_gvisor()
    provider = DockerProvider(runtime="gvisor")
    async with await provider.create(await _plain_request(provider, "many-gvisor")) as sandbox:
        forked = await sandbox.exec(
            ["python3", "-c", _MANY], identity=Identity.AGENT, timeout_sec=90
        )
        assert forked.timed_out, forked.stdout + forked.stderr
        counted = await sandbox.exec(["cat", gvisor.OOM_KILLS])
        assert counted.ok and int(counted.stdout.split()[-1]) >= 300, counted.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["gvisor", "kata"])
async def test_a_sandbox_that_fills_its_memory_with_a_tmpfs_dies_of_it(runtime: str) -> None:
    """Under gVisor a tmpfs is the Sentry's memory, and under Kata the microVM's, which no
    process's end frees: the kernel ends what it may until it reaches guestd or the sandbox,
    and its count in the sandbox, which a watcher there reports, says so."""
    if runtime == "gvisor":
        skip_without_gvisor()
    else:
        skip_without_kata()
    provider = DockerProvider(runtime=runtime)
    request = await _plain_request(provider, f"{runtime}-tmpfs", ContainerFeature.MOUNT)
    fill = (
        "mkdir -p /mnt/fill && mount -t tmpfs -o size=4g none /mnt/fill && "
        "dd if=/dev/zero of=/mnt/fill/zeros bs=1M count=2048"
    )
    async with await provider.create(request) as sandbox:
        with pytest.raises(MemoryExhaustedError) as lost:
            await sandbox.exec(["sh", "-c", fill], timeout_sec=120)
    # The whole sandbox or only guestd first, as the kernel picks.
    evidence = "its cgroup counted" if runtime == "kata" else "the Sentry counted"
    assert "after an OOM kill in it (its Task declares 512 MiB): " in str(lost.value)
    assert evidence in str(lost.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["runc", "gvisor", "kata"])
async def test_a_sandbox_lost_after_an_oom_kill_it_survived_is_lost_to_memory(
    runtime: str,
) -> None:
    """One process killed for the Sandbox's memory, the Sandbox answering after it, and then
    a kill from outside: an OOM kill since the Sandbox started counts."""
    if runtime == "gvisor":
        skip_without_gvisor()
    elif runtime == "kata":
        skip_without_kata()
    provider = DockerProvider(runtime=runtime)
    async with await provider.create(await _plain_request(provider, f"later-{runtime}")) as sandbox:
        allocated = await sandbox.exec(
            ["python3", "-c", _ALLOCATE], identity=Identity.AGENT, timeout_sec=300
        )
        assert not allocated.ok and not allocated.timed_out
        await asyncio.sleep(1)
        assert (await sandbox.exec(["true"])).ok  # it goes on, as an agent's would
        running = asyncio.ensure_future(sandbox.exec(["sleep", "60"], timeout_sec=120))
        await asyncio.sleep(2)
        code, _, stderr = await _docker("kill", sandbox.container)
        assert code == 0, stderr
        with pytest.raises(MemoryExhaustedError) as lost:
            await running
    assert str(lost.value).startswith("the sandbox itself died after an OOM kill in it"), lost


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["runc", "gvisor", "kata"])
async def test_a_docker_sandbox_killed_from_outside_says_so(runtime: str) -> None:
    """A kill from outside, with no OOM kill in the Sandbox before, is a lost Sandbox that
    says so: an environment error."""
    if runtime == "gvisor":
        skip_without_gvisor()
    elif runtime == "kata":
        skip_without_kata()
    provider = DockerProvider(runtime=runtime)
    async with await provider.create(
        await _plain_request(provider, f"killed-{runtime}")
    ) as sandbox:
        running = asyncio.ensure_future(sandbox.exec(["sleep", "60"], timeout_sec=120))
        await asyncio.sleep(2)
        code, _, stderr = await _docker("kill", sandbox.container)
        assert code == 0, stderr
        with pytest.raises(
            GuestUnreachableError, match="itself died: it was killed from outside"
        ) as lost:
            await running
        assert type(lost.value) is GuestUnreachableError


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
