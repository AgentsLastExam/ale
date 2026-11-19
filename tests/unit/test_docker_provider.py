"""The Docker Provider against a scripted docker CLI: each runtime's leaf, preflight,
grants, confinement and Gateway routing, the sandbox's death, reattaching and snapshots."""

from __future__ import annotations

import ast
import asyncio
import contextvars
import dataclasses
import json
import re
import socket
import time
from ipaddress import IPv4Network
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, get_args
from unittest.mock import AsyncMock

import pytest
from tests.support import TraceSink

import ale.run
import ale.run.providers.docker.provider as docker_provider
from ale.core.capability import GpuHold, GpuModel
from ale.core.config import ProviderId
from ale.core.errors import (
    GuestUnreachableError,
    MemoryExhaustedError,
    ProviderCapabilityError,
    ProviderStartError,
    StorageExhaustedError,
)
from ale.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    DesktopResolution,
    GpuDevice,
    Identity,
    PreparedTaskImage,
    ResolvedImage,
    ResourceAllocation,
    SandboxRequest,
    SandboxRole,
    SandboxState,
)
from ale.core.taskspec import ContainerFeature, NetworkMode, NetworkPolicy, Resources
from ale.run import guestd, iptables
from ale.run.providers import memory
from ale.run.providers.docker import (
    gpu_sharing,
    gvisor,
    kata,
    rootless,
    storage,
    subnets,
    tc,
    volumes,
)
from ale.run.providers.docker.provider import (
    GATEWAY_CHAIN,
    HOST_ALIAS,
    RUNTIMES,
    DockerProvider,
    DockerSandbox,
    _GvisorConfinement,
    _KataConfinement,
    _Networks,
    _ports_on_host,
    _RuncConfinement,
    _sandbox_gateway,
    _Session,
)
from ale.run.recording import execution_logging
from ale.run.transport import GuestClient

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "0" * 64
H100 = "NVIDIA H100 80GB HBM3"


def request(**overrides: object) -> SandboxRequest:
    base: dict[str, object] = {
        "episode_id": "e1",
        "prepared_image": PreparedTaskImage(
            kind="container",
            source="external-ref",
            input_identity=DIGEST,
            runtime_ref="image@" + DIGEST,
            prepared_identity=DIGEST,
            resolved_reference="image@" + DIGEST,
        ),
        "resources": Resources(),
        "network": NetworkPolicy(mode=NetworkMode.BLOCK),
        "gateway_url": "http://0.0.0.0:41000",
    }
    return SandboxRequest(**(base | overrides))  # type: ignore[arg-type]


def test_every_container_dials_the_gateway_at_the_fixed_port() -> None:
    fixed = f"http://{HOST_ALIAS}:{SANDBOX_GATEWAY_PORT}"
    assert _sandbox_gateway("http://0.0.0.0:41000") == fixed
    assert _sandbox_gateway("http://127.0.0.1:52000/v1") == f"{fixed}/v1"
    assert _sandbox_gateway("https://gateway.example:443") == "https://gateway.example:443"
    assert _sandbox_gateway(None) is None


def test_only_the_ports_this_host_serves_are_relayed() -> None:
    served = request(
        gateway_url="http://0.0.0.0:41000",
        proxy_url="http://127.0.0.1:41001",
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("a.b",)),
    )
    assert _ports_on_host(served) == [41000, 41001]
    elsewhere = request(gateway_url="https://gateway.example:443")
    assert _ports_on_host(elsewhere) == []


@pytest.fixture
def ports_here(monkeypatch: pytest.MonkeyPatch) -> tuple[rootless.HostPorts, dict[int, int]]:
    """Host ports relayed from this process's namespace, each from a port of its own, which
    the returned map gives."""
    listening: dict[int, int] = {}

    async def listen(_namespaces: rootless.Namespaces, port: int) -> socket.socket:
        listener = socket.create_server(("127.0.0.1", 0))
        listening[port] = listener.getsockname()[1]
        return listener

    async def elsewhere() -> rootless.Namespaces:
        return rootless.Namespaces(user="/proc/1/ns/user", net="/proc/1/ns/net")

    monkeypatch.setattr(rootless, "_listen", listen)
    return rootless.HostPorts(elsewhere), listening


async def test_a_relay_carries_both_ways_and_passes_each_end_on(
    ports_here: tuple[rootless.HostPorts, dict[int, int]],
) -> None:
    ports, listening = ports_here
    sent = bytes(range(256)) * (32 << 10)  # 8 MiB, more than any buffer holds

    async def host(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        got = await reader.read()  # until the Sandbox's end arrives
        writer.write(str(len(got)).encode() + b"\n" + got)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(host, "0.0.0.0", 0)
    port = server.sockets[0].getsockname()[1]
    await ports.hold("ale-a", [port])
    reader, writer = await asyncio.open_connection("127.0.0.1", listening[port])
    writer.write(sent)
    writer.write_eof()
    assert await reader.readline() == f"{len(sent)}\n".encode()
    assert await reader.read() == sent
    writer.close()
    server.close()


async def test_a_relay_listens_while_any_holder_holds_its_port(
    ports_here: tuple[rootless.HostPorts, dict[int, int]],
) -> None:
    ports, listening = ports_here
    await ports.hold("ale-a", [41000])
    await ports.hold("ale-b", [41000])
    assert len(listening) == 1
    ports.release("ale-a")
    # Nothing serves 41000 on the Host: the relay takes the connection, then resets it.
    reader, writer = await asyncio.open_connection("127.0.0.1", listening[41000])
    with pytest.raises(ConnectionResetError):
        await reader.read()
    writer.close()
    ports.release("ale-b")
    await asyncio.sleep(0)
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", listening[41000])


async def test_nothing_is_relayed_where_the_bridges_are_in_this_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def listen(_namespaces: rootless.Namespaces, _port: int) -> socket.socket:
        raise AssertionError("listened")

    looked: list[None] = []

    async def here() -> None:
        looked.append(None)

    monkeypatch.setattr(rootless, "_listen", listen)
    ports = rootless.HostPorts(here)
    await ports.hold("ale-a", [41000])
    await ports.hold("ale-b", [41000, 41001])
    ports.release("ale-a")
    assert len(looked) == 1  # this process stays where it is


#: A sandbox's addresses on its own network, ``ale-net-e1``, whose bridge holds the Host at
#: 172.18.0.1, as a scripted docker CLI reports them.
SANDBOX_IP, SANDBOX_MAC, BRIDGE_MAC = "172.18.0.5", "02:42:ac:12:00:05", "02:42:aa:bb:cc:01"
PEERS = tc.Peers(host_ip="172.18.0.1", host_mac=BRIDGE_MAC, ip=SANDBOX_IP, mac=SANDBOX_MAC)
RESOLV_CONF = "nameserver 192.0.2.53\n"
#: The routes in the daemon's network namespace, as a helper there prints them.
ROUTES = json.dumps([{"dst": "default"}, {"dst": "192.0.2.0/24"}])


@pytest.fixture(autouse=True)
def _volumes_of_no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the Docker calls of the volumes module, which no test here wants made: what a
    probe finds is no directory, and a meter measures nothing written."""

    async def run(*argv: str, **_: object) -> tuple[int, str, str]:
        assert argv[0] == "docker"
        match argv[1:3]:
            case ("volume", "create"):
                return 0, argv[-1], ""
            case ("volume", "inspect"):
                return 0, f"/var/lib/docker/volumes/{argv[-1]}/_data", ""
            case ("exec", _):
                return 0, "0\n0\n", ""
        if argv[1] == "run" and "-I" in argv:
            return 0, "[]", ""
        return 0, "", ""

    monkeypatch.setattr(volumes, "run", run)


def _scripted_reply(argv: tuple[str, ...]) -> tuple[int, str, str]:
    """What the scripted docker CLI answers about networks, addresses, routes and
    resolv.conf."""
    if argv[-len(subnets.ROUTES) :] == subnets.ROUTES:
        return 0, ROUTES, ""
    if argv[:2] == ("network", "inspect"):
        return 0, "172.18.0.1\n", ""
    if argv[:2] == ("inspect", "--format") and "MacAddress" in argv[2]:
        return 0, f"{SANDBOX_IP} {SANDBOX_MAC}\n", ""
    if argv[:3] == ("run", "--rm", "--name") and argv[3].startswith("ale-mac-"):
        return 0, f"{BRIDGE_MAC}\n", ""
    if argv[-1] == "/etc/resolv.conf":
        return 0, RESOLV_CONF, ""
    return 0, "", ""


def _scripted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        return _scripted_reply(argv)

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    return calls


#: What a scripted container has written over its image, in bytes, as a commit measures it.
WRITTEN = 3 << 20


def _measured_at_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def written(container: str) -> int:
        return WRITTEN

    monkeypatch.setattr("ale.run.providers.docker.provider.storage.written", written)


def _created(calls: list[tuple[str, ...]]) -> list[tuple[str, IPv4Network, str, bool]]:
    """Each network the scripted docker CLI created: its name, subnet and gateway, and
    whether it is internal."""
    return [
        (
            call[-1],
            IPv4Network(call[call.index("--subnet") + 1]),
            call[call.index("--gateway") + 1],
            "--internal" in call,
        )
        for call in calls
        if call[:2] == ("network", "create")
    ]


def _helpers(calls: list[tuple[str, ...]]) -> list[list[str]]:
    """The commands of each run of the network helper in the sandbox's namespace."""
    return [call[-1].split(" && ") for call in calls if "container:ale-e1" in call]


@pytest.mark.asyncio
@pytest.mark.parametrize("network", ["ale-net-e1", None])
async def test_the_fixed_port_is_routed_to_the_gateway_and_an_isolated_sandbox_confined(
    monkeypatch: pytest.MonkeyPatch, network: str | None
) -> None:
    calls = _scripted(monkeypatch)
    await _RuncConfinement(network, "172.18.0.1").wire("ale-e1", request())
    (rules,) = _helpers(calls)
    assert len(calls) == 1  # the Host's address was read as the sandbox started
    assert rules[2] == (
        f"iptables -w -t nat -A {GATEWAY_CHAIN} -d 172.18.0.1 -p tcp "
        f"--dport {SANDBOX_GATEWAY_PORT} -j DNAT --to-destination 172.18.0.1:41000"
    )
    confined = [rule for rule in rules if "-t nat" not in rule]
    assert confined == (
        [
            "iptables -w -A OUTPUT -d 172.18.0.1 -p tcp -m multiport --dports 41000 -j ACCEPT",
            "iptables -w -A OUTPUT -d 172.18.0.1 -j REJECT",
        ]
        if network
        else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [NetworkMode.BLOCK, NetworkMode.OPEN])
async def test_a_runc_sandbox_reaches_the_host_at_its_own_networks_gateway(
    monkeypatch: pytest.MonkeyPatch, mode: NetworkMode
) -> None:
    """An isolated one's internal network is created with the Host as its gateway, which
    need not be read back; an ``open`` one reads the default bridge's."""
    calls = _scripted(monkeypatch)
    started = await _RuncConfinement.start(
        "e1-abcdef", request(network=NetworkPolicy(mode=mode)), _Networks()
    )
    inspected = [call[2] for call in calls if call[:2] == ("network", "inspect")]
    if mode is NetworkMode.OPEN:
        assert (started.network, started.host_ip) == (None, "172.18.0.1")
        assert inspected == ["bridge"] and _created(calls) == []
    else:
        ((name, subnet, gateway, internal),) = _created(calls)
        assert (started.network, internal, inspected) == (name, True, [])
        assert name == "ale-net-e1-abcdef" and started.host_ip == gateway == str(subnet[1])


@pytest.mark.asyncio
async def test_a_reattaching_run_replaces_the_gateway_route_and_adds_its_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted(monkeypatch)
    later = request(gateway_url="http://0.0.0.0:42000")
    await _RuncConfinement("ale-net-e1", "172.18.0.1").wire("ale-e1", later, reattached=True)
    rules = calls[-1][-1].split(" && ")
    assert rules[0] == (
        f"{{ iptables -w -t nat -N {GATEWAY_CHAIN} || iptables -w -t nat -F {GATEWAY_CHAIN}; }}"
    )
    assert rules[2].endswith("--to-destination 172.18.0.1:42000")
    assert rules[3:] == [
        "iptables -w -I OUTPUT 1 -d 172.18.0.1 -p tcp -m multiport --dports 42000 -j ACCEPT"
    ]


@pytest.mark.asyncio
async def test_an_open_sandbox_without_a_gateway_needs_no_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted(monkeypatch)
    await _RuncConfinement(None, "172.18.0.1").wire("ale-e1", request(gateway_url=None))
    assert calls == []


async def _in_our_namespace() -> rootless.Namespaces | None:
    return None


def _sandbox() -> DockerSandbox:
    async def close() -> None:
        pass

    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.sandbox_id = "e1-abcdef"
    sandbox.container = "ale-e1-abcdef"
    sandbox.request = request(retention="snapshot")
    sandbox.agent_user = "user"
    sandbox.allocation = ResourceAllocation(
        cpus=1,
        memory_mb=1024,
        sudo=False,
        network_mode=NetworkMode.BLOCK,
        provider="docker:runc",
        leaf="docker:runc",
    )
    sandbox._client = SimpleNamespace(close=close)  # type: ignore[assignment]
    sandbox._file_client = SimpleNamespace(close=close)  # type: ignore[assignment]
    sandbox._sessions = ()  # type: ignore[assignment]
    sandbox._watcher = None
    sandbox._stopping = None
    sandbox._watchdog = gpu_sharing.Watchdog()
    sandbox._storage = None
    sandbox._host_ports = rootless.HostPorts(_in_our_namespace)
    sandbox._context = contextvars.copy_context()
    sandbox.container_id = "f" * 64
    sandbox._relay = None
    sandbox.written = 0
    sandbox._committing = False
    sandbox.mounts = ()
    sandbox._kept = frozenset()
    sandbox._meter = None
    return sandbox


@pytest.mark.asyncio
async def test_a_sealed_snapshot_gives_the_account_its_settings_back_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The committed image outlives the relay its system proxy names."""
    calls = _scripted(monkeypatch)
    _measured_at_commit(monkeypatch)

    async def egress_relay(proxy: str | None, **_: object) -> str | None:
        calls.append(("egress_relay", str(proxy)))
        return None

    async def close() -> None:
        calls.append(("close",))

    sandbox = _sandbox()
    sandbox.request = request(
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("a.b",)),
        proxy_url="http://0.0.0.0:9443",
    )
    sandbox._client = SimpleNamespace(egress_relay=egress_relay, close=close)  # type: ignore[assignment]
    sandbox._relay = "127.0.0.1:41999"
    await sandbox.snapshot(roles=("solver",), reason="kept")
    assert calls[:2] == [("egress_relay", "None"), ("close",)]
    assert sandbox._relay is None


@pytest.mark.asyncio
async def test_a_snapshot_commits_the_stopped_container_without_its_gpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Sandbox booted from it, whose labels the image's are, holds and reserves nothing."""
    calls = _scripted(monkeypatch)
    _measured_at_commit(monkeypatch)
    retained = await _sandbox().snapshot(roles=("solver",), reason="kept")
    assert calls == [
        ("stop", "--time", "30", "ale-e1-abcdef"),
        (
            "commit",
            "--change",
            "LABEL ale.retention=snapshot",
            "--change",
            'LABEL ale.gpus=""',
            "--change",
            'LABEL ale.gpu_memory_mb=""',
            "--change",
            f"LABEL ale.prepared={DIGEST}",
            "--change",
            f"LABEL ale.written={WRITTEN}",
            "ale-e1-abcdef",
            "ale-snapshot:e1-abcdef",
        ),
    ]
    assert retained.handle == "docker:snapshot:e1-abcdef"
    assert retained.gpu_devices == ()


@pytest.mark.asyncio
async def test_a_snapshot_of_a_snapshot_holds_what_both_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its storage counts what the snapshot it booted from holds, as a disk of that size
    would, so the next snapshot holds both."""
    calls = _scripted(monkeypatch)
    _measured_at_commit(monkeypatch)
    sandbox = _sandbox()
    sandbox.written = 5 << 20
    await sandbox.snapshot(roles=("solver",), reason="kept")
    assert f"LABEL ale.written={(5 << 20) + WRITTEN}" in calls[1]


@pytest.mark.asyncio
async def test_a_commit_cut_short_leaves_no_snapshot_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit may have finished in the daemon as ALE gave up on it: destroying the Sandbox
    removes its container, which fails a commit still under way, then the image it named,
    and only one never handed back."""
    calls = _scripted(monkeypatch)
    _measured_at_commit(monkeypatch)

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[0] == "commit":
            raise asyncio.CancelledError
        return _scripted_reply(argv)

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    sandbox = _sandbox()
    sandbox.confinement = SimpleNamespace(remove=AsyncMock())  # type: ignore[assignment]
    sandbox.state = SandboxState.READY
    with pytest.raises(asyncio.CancelledError):
        await sandbox.snapshot(roles=("solver",), reason="kept")
    await sandbox.destroy()
    removed = [call[:2] for call in calls if call[0] in {"rm", "rmi"}]
    assert removed == [("rm", "-f"), ("rmi", "ale-snapshot:e1-abcdef")]

    kept = _sandbox()
    kept.confinement = SimpleNamespace(remove=AsyncMock())  # type: ignore[assignment]
    kept.state = SandboxState.READY
    calls = _scripted(monkeypatch)
    await kept.snapshot(roles=("solver",), reason="kept")
    await kept.destroy()
    assert calls[-1][:2] == ("rm", "-f")
    assert not any(call[0] == "rmi" for call in calls)


def _snapshot_inspect(labels: dict[str, str]) -> str:
    return json.dumps([{"Id": "sha256:" + "5" * 64, "Config": {"Labels": labels}}])


@pytest.mark.asyncio
async def test_a_snapshot_boots_only_for_the_tasks_prepared_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = {
        "ale.managed": "true",
        "ale.retention": "snapshot",
        "ale.episode": "e1",
        "ale.role": "solver",
        "ale.prepared": DIGEST,
    }

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        assert argv[:3] == ("image", "inspect", "ale-snapshot:e1-abcdef")
        return 0, _snapshot_inspect(labels), ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    provider = DockerProvider()
    image, written = await provider._snapshot_image("docker:snapshot:e1-abcdef", request())
    assert (image.observed_identity, image.observed_ref) == ("sha256:" + "5" * 64,) * 2
    assert image.prepared_identity == DIGEST
    assert written == 0
    labels["ale.written"] = "4096"
    _, written = await provider._snapshot_image("docker:snapshot:e1-abcdef", request())
    assert written == 4096

    labels["ale.prepared"] = "sha256:" + "1" * 64
    with pytest.raises(ProviderCapabilityError, match="prepared solver image"):
        await provider._snapshot_image("docker:snapshot:e1-abcdef", request())
    labels["ale.managed"] = "false"
    with pytest.raises(ProviderCapabilityError, match="not an ALE snapshot"):
        await provider.destroy_retained("docker:snapshot:e1-abcdef")


@pytest.mark.asyncio
async def test_a_snapshot_is_listed_and_destroyed_by_its_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed: list[str] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        if argv[0] == "ps":
            return 0, "", ""
        if argv[0] == "images":
            assert "reference=ale-snapshot:*" in argv
            return 0, "e1-abcdef\nby-hand\n", ""
        if argv[:2] == ("image", "inspect"):
            mine = argv[2] == "ale-snapshot:e1-abcdef"
            labels = {"ale.managed": "true", "ale.retention": "snapshot", "ale.episode": "e1"}
            return 0, _snapshot_inspect(labels if mine else {}), ""
        if argv[0] == "rmi":
            removed.append(argv[1])
            return (1, "", "conflict: image is being used") if len(removed) == 1 else (0, "", "")
        raise AssertionError(argv)

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    provider = DockerProvider()
    assert [record["handle"] for record in await provider.list_retained()] == [
        "docker:snapshot:e1-abcdef"
    ]
    with pytest.raises(ProviderStartError, match="being used"):
        await provider.destroy_retained("docker:snapshot:e1-abcdef")
    await provider.destroy_retained("docker:snapshot:e1-abcdef")
    assert removed == ["ale-snapshot:e1-abcdef"] * 2


async def test_a_provider_without_snapshots_refuses_them_before_anything_starts() -> None:
    from ale.run.providers.daytona import DaytonaProvider

    with pytest.raises(ProviderCapabilityError, match="saves and boots no snapshots"):
        await DaytonaProvider().accepts(request(retention="snapshot", network=NetworkPolicy()))
    with pytest.raises(ProviderCapabilityError, match="saves and boots no snapshots"):
        await DaytonaProvider().accepts(
            request(snapshot="docker:snapshot:e1", network=NetworkPolicy())
        )
    await DockerProvider().accepts(request(retention="snapshot"))


def test_every_helper_container_of_ales_runs_under_runc() -> None:
    """ALE's own containers, its runners, builders and netfilter helper, need what runc
    gives them of the Host, whatever runtime the daemon starts others with."""
    root = Path(ale.run.__file__).parent
    helpers: list[str] = []
    unpinned: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            # A command line: a call's arguments, or a list of them built first.
            if isinstance(node, ast.Call):
                argv = node.args
                through = isinstance(node.func, ast.Name) and node.func.id == "_docker"
            elif isinstance(node, ast.List):
                argv, through = node.elts, False
            else:
                continue
            words = [arg.value for arg in argv[:2] if isinstance(arg, ast.Constant)]
            if words != ["docker", "run"] and not (through and words[:1] == ["run"]):
                continue
            where = f"{path.relative_to(root)}:{node.lineno}"
            helpers.append(where)
            if not any(
                isinstance(arg, ast.Starred)
                and isinstance(arg.value, ast.Name)
                and arg.value.id == "HELPER_RUNTIME"
                for arg in argv
            ):
                unpinned.append(where)
    assert len(helpers) >= 7, helpers
    assert unpinned == []


def _helper_commands(call: tuple[str, ...]) -> list[str]:
    assert call[:7] == (
        "run", "--rm", "--runtime", "runc", "--network", "container:ale-e1", "--cap-add"
    )  # fmt: skip
    return call[-1].split(" && ")


def _on_own_bridge(kind: type[_KataConfinement] | type[_GvisorConfinement]) -> Any:
    return kind(_Networks(), "ale-net-e1", "172.18.0.1", BRIDGE_MAC)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [_KataConfinement, _GvisorConfinement])
@pytest.mark.parametrize("mode", [NetworkMode.OPEN, NetworkMode.BLOCK])
async def test_a_kata_or_gvisor_sandbox_boots_on_a_network_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
    kind: type[_KataConfinement] | type[_GvisorConfinement],
    mode: NetworkMode,
) -> None:
    """No other container shares its segment. The network is not internal, whatever the
    Task's mode: the runtime never sees a network attached later, so egress opens through
    this one's NAT. The Host is its gateway, whose MAC a helper on the network reads before
    the sandbox starts, the default bridge's resolv.conf replaces Docker's embedded DNS
    server, out of the sandbox's reach, once the sandbox is sealed, and destroying the
    sandbox removes the network."""
    calls = _scripted(monkeypatch)
    wanted = request(network=NetworkPolicy(mode=mode))
    confinement = await kind.start("e1-abcdef", wanted, _Networks())
    network = "ale-net-e1-abcdef"
    ((name, _, gateway, internal),) = _created(calls)
    assert (name, internal) == (network, False)
    # On the network itself, removed by name before the sandbox starts there.
    (helper,) = [call for call in calls if call[0] == "run" and "--name" in call]
    name = helper[helper.index("--name") + 1]
    assert helper[helper.index("--network") + 1] == network and helper[-1] == gateway
    assert calls[-1] == ("rm", "-f", name)
    assert confinement.host_ip == gateway and confinement.host_mac == BRIDGE_MAC
    assert confinement.run_args(mode is not NetworkMode.OPEN)[:4] == [
        "--network", network, "--label", f"ale.bridge-mac={BRIDGE_MAC}"
    ]  # fmt: skip
    calls.clear()
    await confinement.wire("ale-e1", wanted)
    assert await confinement.peers("ale-e1") == dataclasses.replace(PEERS, host_ip=gateway)
    (address,) = [call for call in calls if call[0] == "inspect"]
    assert f'index .NetworkSettings.Networks "{network}"' in address[2]
    assert not [call for call in calls if call[:2] == ("run", "--rm") and "--name" in call]
    (resolved,) = [call for call in calls if call[:3] == ("exec", "-u", "0")]
    assert resolved[3:7] == ("ale-e1", guestd.PYTHON["linux"], "-I", "-c")
    assert resolved[-1] == RESOLV_CONF
    if mode is not NetworkMode.OPEN:
        # Sealed first: nothing but the seal's own helper runs between the start and it.
        sealing = [index for index, call in enumerate(calls) if "container:ale-e1" in call]
        assert sealing and sealing[0] < calls.index(resolved)
    read = [call for call in calls if call[-1] == "/etc/resolv.conf"]
    assert read == [
        (
            "run", "--rm", "--runtime", "runc", "--network", "bridge", "--entrypoint", "cat",
            docker_provider.NETFILTER_IMAGE, "/etc/resolv.conf",
        )
    ]  # fmt: skip
    calls.clear()
    await confinement.wire("ale-e1", wanted, reattached=True)
    assert not [call for call in calls if call[:3] == ("exec", "-u", "0")]
    sandbox = _sandbox()
    sandbox.confinement = confinement
    sandbox.state = SandboxState.READY
    calls.clear()
    await sandbox.destroy()
    assert calls == [("rm", "-f", "-v", "ale-e1-abcdef"), ("network", "rm", network)]


@pytest.mark.asyncio
@pytest.mark.parametrize("removal", ["timed out", "refused"])
async def test_a_destroyed_sandbox_removes_its_network_whatever_its_container_removal_did(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, removal: str
) -> None:
    """A loaded daemon can time out removing the container; the network is still asked to
    go, and one Docker keeps, since the container still holds it, is logged by name."""
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:2] == ("rm", "-f"):
            if removal == "timed out":
                raise ProviderStartError("docker rm -f timed out after 300s")
            return 1, "", "Error response from daemon: cannot remove container"
        if argv[:2] == ("network", "rm"):
            return (
                1,
                "",
                "Error response from daemon: error while removing network: has active endpoints",
            )
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    sandbox = _sandbox()
    sandbox.confinement = _on_own_bridge(_KataConfinement)
    sandbox.state = SandboxState.READY
    with caplog.at_level("WARNING"):
        if removal == "timed out":
            with pytest.raises(ProviderStartError, match="timed out"):
                await sandbox.destroy()
        else:
            await sandbox.destroy()
    assert calls[-1] == ("network", "rm", "ale-net-e1")
    assert "could not remove network ale-net-e1: " in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "read"),
    [
        ((0, f"{BRIDGE_MAC}\n", ""), BRIDGE_MAC),
        ((0, "\n", ""), None),
        ((1, "", "no neighbour entry for 172.18.0.1"), None),
        (ProviderStartError("docker run timed out after 60s"), None),
    ],
)
async def test_the_bridges_mac_is_read_on_the_network_by_a_helper_gone_before_it_returns(
    monkeypatch: pytest.MonkeyPatch,
    answer: tuple[int, str, str] | ProviderStartError,
    read: str | None,
) -> None:
    """Not from the bridge's device, which a helper on ``--network host`` cannot see under
    rootless Docker with a detached network namespace: a runc helper on the network resolves
    the Host's address there, and is removed by name whatever came of it."""
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[0] == "run":
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return 1, "", "No such container"

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    if read is None:
        with pytest.raises(ProviderStartError, match=r"ale-net-e1's bridge|timed out"):
            await docker_provider._bridge_mac("ale-net-e1", "172.18.0.1")
    else:
        assert await docker_provider._bridge_mac("ale-net-e1", "172.18.0.1") == read
    run, removed = calls
    name = run[run.index("--name") + 1]
    assert run[:4] == ("run", "--rm", "--name", name) and name.startswith("ale-mac-")
    assert run[4:12] == (
        "--runtime", "runc", "--network", "ale-net-e1", "--entrypoint", "bash",
        docker_provider.NETFILTER_IMAGE, "-c",
    )  # fmt: skip
    assert run[12:] == (docker_provider._NEIGHBOUR_MAC, "bash", "172.18.0.1")
    assert removed == ("rm", "-f", name)


@pytest.mark.asyncio
async def test_a_sandbox_that_records_no_bridge_mac_is_not_reattached() -> None:
    """A helper on its network would be its neighbour, so the MAC comes from its label."""
    with pytest.raises(ProviderCapabilityError, match="records no MAC"):
        await _GvisorConfinement.found(("ale-net-e1",), _Networks(), {})


REPLACE = "tc filter replace dev"


@pytest.mark.asyncio
async def test_a_kata_sandbox_is_confined_by_tc_filters_outside_its_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kata passes frames between the VM and its veth with tc, past netfilter, so the
    Gateway port is rewritten both ways and an isolated sandbox starts sealed in tc. Root in
    the VM writes any frame, so a frame passes to the Host only from the sandbox's own
    addresses to the bridge's, and one passes to the VM only from the bridge's MAC and
    address to the sandbox's."""
    calls = _scripted(monkeypatch)
    proxied = request(
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("a.b",)),
        proxy_url="http://172.18.0.1:9443",
    )
    await _on_own_bridge(_KataConfinement).wire("ale-e1", proxied)
    (filters,) = _helpers(calls)
    tap, veth, host = kata.TAP, kata.VETH, "172.18.0.1"
    assert filters == [
        f"test -e /sys/class/net/{tap}",
        f"test -e /sys/class/net/{veth}",
        f"{REPLACE} {tap} ingress pref 12 handle 1 protocol ip flower dst_ip {host} action drop",
        f"{REPLACE} {tap} ingress pref 20 handle 1 protocol arp flower src_mac {SANDBOX_MAC} "
        f"arp_sip {SANDBOX_IP} arp_sha {SANDBOX_MAC} arp_tip {host} "
        f"action mirred egress redirect dev {veth}",
        f"{REPLACE} {tap} ingress pref 21 handle 1 protocol all flower action drop",
        f"{REPLACE} {veth} ingress pref 20 handle 1 protocol ip flower src_mac {BRIDGE_MAC} "
        f"dst_mac {SANDBOX_MAC} src_ip {host} action mirred egress redirect dev {tap}",
        f"{REPLACE} {veth} ingress pref 21 handle 1 protocol arp flower src_mac {BRIDGE_MAC} "
        f"arp_sip {host} arp_sha {BRIDGE_MAC} action mirred egress redirect dev {tap}",
        f"{REPLACE} {veth} ingress pref 22 handle 1 protocol all flower action drop",
        f"{REPLACE} {tap} ingress pref 10 handle 1 protocol ip flower src_mac {SANDBOX_MAC} "
        f"dst_mac {BRIDGE_MAC} src_ip {SANDBOX_IP} dst_ip {host} ip_proto tcp "
        f"dst_port {SANDBOX_GATEWAY_PORT} action pedit ex munge tcp dport set 41000 pipe "
        f"action csum tcp pipe action mirred egress redirect dev {veth}",
        f"{REPLACE} {veth} ingress pref 10 handle 1 protocol ip flower src_mac {BRIDGE_MAC} "
        f"dst_mac {SANDBOX_MAC} src_ip {host} ip_proto tcp src_port 41000 "
        f"action pedit ex munge tcp sport set {SANDBOX_GATEWAY_PORT} pipe "
        f"action csum tcp pipe action mirred egress redirect dev {tap}",
        f"{REPLACE} {tap} ingress pref 11 handle 1 protocol ip flower src_mac {SANDBOX_MAC} "
        f"dst_mac {BRIDGE_MAC} src_ip {SANDBOX_IP} dst_ip {host} ip_proto tcp dst_port 9443 "
        f"action mirred egress redirect dev {veth}",
    ]


@pytest.mark.asyncio
async def test_an_open_kata_sandbox_gets_only_the_gateway_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted(monkeypatch)
    await _on_own_bridge(_KataConfinement).wire(
        "ale-e1", request(network=NetworkPolicy(mode=NetworkMode.OPEN))
    )
    (filters,) = _helpers(calls)
    put = [item for item in filters if item.startswith(REPLACE)]
    assert len(put) == 2 and all(" pref 10 " in item for item in put)


@pytest.mark.asyncio
async def test_a_reattaching_run_replaces_only_the_kata_gateway_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seal and the Host's drop stay as the container has them; a proxy filter the
    blocked sandbox no longer has goes."""
    calls = _scripted(monkeypatch)
    later = request(gateway_url="http://0.0.0.0:42000")
    await _on_own_bridge(_KataConfinement).wire("ale-e1", later, reattached=True)
    (filters,) = _helpers(calls)
    put = [item for item in filters if item.startswith(REPLACE)]
    assert all(" pref 10 " in item for item in put) and len(put) == 2
    assert any("dport set 42000" in item for item in put)
    assert [item for item in filters if " del " in item] == [
        f"{{ tc filter del dev {kata.TAP} ingress pref 11 2>/dev/null || true; }}"
    ]


@pytest.mark.asyncio
async def test_a_kata_sandbox_opens_and_seals_egress_with_its_tc_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filters seal it; the guest's default route goes too, so what they drop fails at
    once rather than after a timeout, and comes back only once the filters are gone.
    Sealing again replaces the filters in place, never taking the drop away first."""
    calls = _scripted(monkeypatch)
    order: list[str] = []

    async def guest_exec(argv: list[str], **_: object) -> tuple[int, str, str, bool]:
        order.append(f"route {argv[-2]}")
        return 0, "", "", False

    sandbox = _sandbox()
    sandbox.container = "ale-e1"
    sandbox.confinement = _on_own_bridge(_KataConfinement)
    sandbox._sealed = False
    sandbox._client = SimpleNamespace(exec=guest_exec)  # type: ignore[assignment]
    await sandbox.open_egress()
    opened = _helper_commands(calls[-1])
    assert not [item for item in opened if item.startswith(REPLACE)]
    assert opened[-1] == f'test -z "$(tc filter show dev {kata.VETH} ingress pref 22)"'
    order.append("helper")
    await sandbox.close_egress()
    sealed = _helper_commands(calls[-1])
    assert sealed == kata.seal(PEERS)
    assert sandbox._sealed
    assert order == ["route add", "helper", "route del"]
    assert not [call for call in calls if call[:2] == ("network", "connect")]


@pytest.mark.parametrize("module", [kata, gvisor])
def test_the_seal_probe_reads_the_drop_the_seal_puts_in_place(module: Any) -> None:
    first_drop = next(item for item in module.seal(PEERS) if item.endswith(tc.DROP_ALL))
    words = first_drop.split()
    device, hook, pref = words[words.index("dev") + 1], words[5], words[words.index("pref") + 1]
    assert f"tc filter show dev {device} {hook} pref {pref}" == module.SEAL_PROBE


def _netstack_state(call: tuple[str, ...]) -> dict[str, Any]:
    """The state a privileged exec of the iptables writer sets, checked to be one it
    takes."""
    assert call[:5] == ("exec", "--privileged", "-u", "0", "ale-e1")
    assert call[5:8] == ("/opt/ale/python/bin/python3", "-I", "-c")
    assert call[8] == Path(iptables.__file__).read_text()
    iptables.plan(call[9])
    return json.loads(call[9])


def _netstack_calls(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [call for call in calls if call[:2] == ("exec", "--privileged")]


@pytest.mark.asyncio
async def test_a_gvisor_sandbox_is_sealed_outside_and_refused_inside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """netstack reads every frame through a packet socket before tc's ingress, so the seal
    is on the veth's egress, where the wire carries the Gateway's own port, and only from
    the sandbox's own addresses to the bridge's; ALE's chains in netstack route the fixed
    port and refuse what the seal drops."""
    calls = _scripted(monkeypatch)
    proxied = request(
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("a.b",)),
        proxy_url="http://172.18.0.1:9443",
    )
    await _on_own_bridge(_GvisorConfinement).wire("ale-e1", proxied)
    (filters,) = _helpers(calls)
    (netstack,) = _netstack_calls(calls)
    assert calls.index(netstack) > max(
        calls.index(call) for call in calls if "container:ale-e1" in call
    )
    egress = f"{REPLACE} {gvisor.VETH} egress"
    host = "172.18.0.1"
    to_host = f"src_mac {SANDBOX_MAC} dst_mac {BRIDGE_MAC} src_ip {SANDBOX_IP} dst_ip {host}"
    assert filters == [
        f"test -e /sys/class/net/{gvisor.VETH}",
        "tc qdisc replace dev eth0 clsact",
        f"{egress} pref 12 handle 1 protocol ip flower dst_ip {host} action drop",
        f"{egress} pref 20 handle 1 protocol arp flower src_mac {SANDBOX_MAC} "
        f"arp_sip {SANDBOX_IP} arp_sha {SANDBOX_MAC} arp_tip {host} action pass",
        f"{egress} pref 21 handle 1 protocol all flower action drop",
        f"{egress} pref 10 handle 1 protocol ip flower {to_host} ip_proto tcp dst_port 41000 "
        "action pass",
        f"{egress} pref 11 handle 1 protocol ip flower {to_host} ip_proto tcp dst_port 9443 "
        "action pass",
    ]
    tables = _netstack_state(netstack)["tables"]
    episode = {"dst": host, "proto": "tcp", "dports": [41000, 9443]}
    assert tables["nat"] == {
        "chains": {
            "ALE_GATEWAY": [
                {
                    "dst": host,
                    "proto": "tcp",
                    "dports": [SANDBOX_GATEWAY_PORT],
                    "target": {"DNAT": f"{host}:41000"},
                }
            ]
        },
        "jumps": {"OUTPUT": ["ALE_GATEWAY"]},
    }
    assert tables["filter"]["chains"] == {
        "ALE_HOST": [
            episode | {"target": "ACCEPT"},
            {"dst": host, "proto": "tcp", "target": "REJECT_TCP_RESET"},
            {"dst": host, "target": "REJECT"},
        ],
        "ALE_SEAL": [
            {"out": "lo", "target": "ACCEPT"},
            episode | {"target": "ACCEPT"},
            {"proto": "tcp", "target": "REJECT_TCP_RESET"},
            {"target": "REJECT"},
        ],
        "ALE_INPUT": [
            {"in": "lo", "target": "ACCEPT"},
            {"ctstate": ["ESTABLISHED", "RELATED"], "target": "ACCEPT"},
            {"target": "DROP"},
        ],
    }
    assert tables["filter"]["jumps"] == {
        "OUTPUT": ["ALE_HOST", "ALE_SEAL"],
        "FORWARD": ["ALE_HOST", "ALE_SEAL"],
        "INPUT": ["ALE_INPUT"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway_url", ["http://0.0.0.0:41000", None])
async def test_an_open_gvisor_sandbox_gets_only_the_gateway_route_in_netstack(
    monkeypatch: pytest.MonkeyPatch, gateway_url: str | None
) -> None:
    """No filter outside it, and the privileged writer runs only for a Gateway to route."""
    calls = _scripted(monkeypatch)
    await _on_own_bridge(_GvisorConfinement).wire(
        "ale-e1", request(network=NetworkPolicy(mode=NetworkMode.OPEN), gateway_url=gateway_url)
    )
    assert _helpers(calls) == []
    if gateway_url is None:
        assert _netstack_calls(calls) == []
        return
    (netstack,) = _netstack_calls(calls)
    tables = _netstack_state(netstack)["tables"]
    assert list(tables["nat"]["chains"]) == ["ALE_GATEWAY"] and tables["filter"] == {}


@pytest.mark.asyncio
async def test_a_reattaching_run_replaces_only_the_gvisor_gateway_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seal stays as the container has it, read once; netstack's chains follow it; a
    proxy filter the blocked sandbox no longer has goes."""
    calls = _scripted(monkeypatch)
    scripted = docker_provider._docker

    async def docker(*argv: str, **kwargs: object) -> tuple[int, str, str]:
        if argv[-1] == gvisor.SEAL_PROBE:
            calls.append(argv)
            return 0, "action order 1: gact action drop", ""
        return await scripted(*argv, **kwargs)

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    confinement = _on_own_bridge(_GvisorConfinement)
    later = request(gateway_url="http://0.0.0.0:42000")
    await confinement.wire("ale-e1", later, reattached=True)
    assert await confinement.sealed("ale-e1", {}) is True
    filters, probe = _helpers(calls)
    assert probe == [gvisor.SEAL_PROBE]
    put = [item for item in filters if item.startswith(REPLACE)]
    assert len(put) == 1 and "pref 10" in put[0] and "dst_port 42000" in put[0]
    assert [item for item in filters if " del " in item] == [
        f"{{ tc filter del dev {gvisor.VETH} egress pref 11 2>/dev/null || true; }}"
    ]
    (netstack,) = _netstack_calls(calls)
    tables = _netstack_state(netstack)["tables"]
    assert tables["filter"]["chains"]["ALE_SEAL"]  # still sealed
    assert tables["nat"]["chains"]["ALE_GATEWAY"][0]["target"] == {"DNAT": "172.18.0.1:42000"}


@pytest.mark.asyncio
async def test_a_gvisor_sandbox_opens_inside_first_and_seals_outside_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary is never open while netstack's chains still refuse, and never sealed
    while they still pass; sealing again replaces the filters, never removing the drop."""
    calls = _scripted(monkeypatch)
    sandbox = _sandbox()
    sandbox.container = "ale-e1"
    confinement = _on_own_bridge(_GvisorConfinement)
    await confinement.wire("ale-e1", request())
    calls.clear()
    sandbox.confinement = confinement
    sandbox._sealed = True
    sandbox._client = SimpleNamespace(egress_relay=None)  # type: ignore[assignment]

    async def nothing(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("ale.run.providers.docker.provider.relay.stop", nothing)
    monkeypatch.setattr("ale.run.providers.docker.provider.relay.start", nothing)
    await sandbox.open_egress()
    (inside, outside) = calls
    assert _netstack_state(inside)["tables"]["filter"]["chains"]["ALE_SEAL"] == []
    assert _helper_commands(outside)[-1] == (
        f'test -z "$(tc filter show dev {gvisor.VETH} egress pref 21)"'
    )
    calls.clear()
    await sandbox.close_egress()
    (outside, inside) = calls
    assert _helper_commands(outside) == gvisor.seal(PEERS)
    assert _netstack_state(inside)["tables"]["filter"]["chains"]["ALE_SEAL"]


@pytest.mark.parametrize("isolated", [True, False])
@pytest.mark.parametrize("sealed", [True, False])
@pytest.mark.parametrize("ports", [[], ["41000"], ["41000", "9443"]])
def test_every_netstack_state_is_one_the_writer_takes(
    isolated: bool, sealed: bool, ports: list[str]
) -> None:
    state = gvisor.netstack(
        "172.17.0.1",
        gateway_port=ports[0] if ports else "",
        ports=ports,
        isolated=isolated,
        sealed=sealed and isolated,
    )
    plans = iptables.plan(json.dumps(state))
    assert set(plans) == {"nat", "filter"}
    chains, _ = plans["filter"]
    assert set(chains) == ({"ALE_HOST", "ALE_SEAL", "ALE_INPUT"} if isolated else set())


def test_a_gvisor_sandbox_holds_the_declared_memory_and_what_the_sentry_takes() -> None:
    assert gvisor.memory_mb(512) == 620
    assert gvisor.memory_mb(8192) == 8308


def test_gvisor_is_registered_as_its_readme_shows() -> None:
    """Every name, with exactly these arguments: raw sockets but no packet socket writes,
    and nf_tables without raw sockets."""
    readme = (Path(ale.run.__file__).parents[5] / "images/runtimes/gvisor/README.md").read_text()
    shown = json.loads(readme.split("```json\n", 1)[1].split("```", 1)[0])["runtimes"]
    assert {name: tuple(entry["runtimeArgs"]) for name, entry in shown.items()} == {
        "runsc": ("--overlay2=none", "--allow-suid", "--nvproxy-allowed-driver-capabilities=all"),
        "runsc-net-raw": (
            "--overlay2=none",
            "--allow-suid",
            "--nvproxy-allowed-driver-capabilities=all",
            "--net-raw",
        ),
        "runsc-nested": (
            "--overlay2=none",
            "--allow-suid",
            "--nvproxy-allowed-driver-capabilities=all",
            "--TESTONLY-nftables",
        ),
    }
    assert {name: tuple(args) for name, args in gvisor.ARGUMENTS.items()} == {
        name: tuple(entry["runtimeArgs"]) for name, entry in shown.items()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime", "watched"),
    [
        ("runc", None),
        ("kata", (kata.OOM_KILLS, "its cgroup")),
        ("gvisor", (gvisor.OOM_KILLS, "the Sentry")),
    ],
)
async def test_a_watcher_counts_the_oom_kills_docker_does_not_record(
    monkeypatch: pytest.MonkeyPatch, runtime: str, watched: tuple[str, str] | None
) -> None:
    """Docker records the OOM kills of the Host's kernel; a watcher in the Sandbox counts
    those of Kata's guest kernel and of gVisor's Sentry, which kills below the container's
    limit on the Host."""
    started: list[tuple[str, str]] = []

    async def start(argv: list[str], *, counter: str) -> object:
        started.append((argv[-1], counter))
        return object()

    monkeypatch.setattr(docker_provider.memory.GuestEvents, "start", start)
    watcher = await docker_provider._watcher("ale-e1", docker_provider.RUNTIMES[runtime])
    assert started == ([watched] if watched else [])
    assert (watcher is None) is (watched is None)


def test_the_gvisor_build_ale_runs_is_the_one_its_build_script_makes() -> None:
    """And the one both READMEs name, in its version string and the path it installs to."""
    root = Path(ale.run.__file__).parents[5]
    build = (root / "images/runtimes/gvisor/build.sh").read_text()
    (version,) = re.findall(r"^VERSION=([0-9.]+)$", build, re.MULTILINE)
    assert version == gvisor.VERSION and f"+ale.{version}" == gvisor.BUILD
    for readme in ("images/runtimes/gvisor", "packages/ale-run/src/ale/run/providers"):
        path = root / readme / "README.md"
        text = path.read_text()
        named = re.findall(r"\+ale\.([0-9][0-9.]*[0-9])", text)
        named += re.findall(r"ale-gvisor/([0-9][0-9.]*[0-9])", text)
        named += re.findall(r"^version=([0-9.]+)$", text, re.MULTILINE)
        assert named and set(named) == {version}, f"{path} names {sorted(set(named))}"


#: What a container's state then says, as ``docker inspect`` reports it.
_KILLED, _RUNS = {"Running": False, "ExitCode": 137}, {"Running": True, "ExitCode": 0}
_OOM_KILL = "after an OOM kill in it (its Task declares 512 MiB): Docker recorded OOMKilled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("waited", "state", "lost"),
    [
        # An OOM kill in it, which Docker keeps from its start: lost to memory, whatever
        # ended it then.
        (
            0,
            {**_KILLED, "OOMKilled": True},
            f"MemoryExhaustedError: the sandbox itself died {_OOM_KILL}, and its main process "
            "exited with code 137",
        ),
        (
            0,
            {"Running": False, "ExitCode": 139, "OOMKilled": True},
            f"MemoryExhaustedError: the sandbox itself died {_OOM_KILL}, and its main process "
            "exited with code 139",
        ),
        # Only guestd went, and the Sandbox runs on: still memory.
        (
            None,
            {**_RUNS, "OOMKilled": True},
            f"MemoryExhaustedError: guestd in the sandbox ended {_OOM_KILL}",
        ),
        (
            0,
            {**_KILLED, "OOMKilled": False},
            "GuestUnreachableError: the sandbox itself died: it was killed from outside",
        ),
        (
            0,
            {"Running": False, "ExitCode": 3, "OOMKilled": False},
            "GuestUnreachableError: the sandbox itself died: its main process exited with code 3",
        ),
        (0, {**_RUNS, "OOMKilled": False}, None),  # running again by the time it was read
        (None, {**_RUNS, "OOMKilled": False}, None),  # still running when the wait gave up
        (1, None, None),  # docker wait failed: the container is gone
    ],
)
async def test_a_sandbox_that_died_says_why_when_its_session_ends(
    monkeypatch: pytest.MonkeyPatch,
    waited: int | None,
    state: dict[str, object] | None,
    lost: str | None,
) -> None:
    """One wait, which returns at once for a stopped container, then its state; no polling.
    It runs when the stream of a session that answered ends, as the client asks the session
    to explain the end: a real process answers and ends it here. Docker's record of an OOM
    kill then decides between memory, a kill from outside and an exit code."""
    calls: list[tuple[str, ...]] = []
    deadlines: list[float] = []

    async def docker(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[0] == "wait":
            deadlines.append(timeout)
            if waited is None:
                raise ProviderStartError(f"docker wait ale-e1 timed out after {timeout:g}s")
            return (
                waited,
                "137\n" if waited == 0 else "",
                "" if waited == 0 else "No such container",
            )
        assert argv == ("inspect", "--format", "{{json .State}}", "ale-e1")
        return 0, json.dumps(state), ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    session = _Session(["sh", "-c", "echo {}; sleep 0.2; echo urpc EOF >&2"], "ale-e1", 512)
    await session.start()
    assert await session.recv() == "{}"
    with pytest.raises(GuestUnreachableError) as ended:
        await session.recv()
    assert calls == []
    explained = await session.explain(ended.value)
    if lost is None:
        assert explained is ended.value
    else:
        named = f"{type(explained).__name__}: {explained}"
        assert named == f"{lost} (guest session closed: urpc EOF)"
        assert isinstance(explained, MemoryExhaustedError) is lost.startswith("Memory")
    assert [call[0] for call in calls] == (["wait"] if waited == 1 else ["wait", "inspect"])
    assert deadlines == [30.0]
    assert await session.explain(ended.value) is explained  # asked once, then kept
    assert len(calls) == (1 if waited == 1 else 2)


@pytest.mark.asyncio
async def test_a_kata_sandbox_lost_after_an_oom_kill_in_its_guest_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker records no OOM kill in a Kata guest: its watcher's session, a process here that
    writes what the watcher in the guest would and then ends with it, counts them from the
    Sandbox's start."""

    async def docker(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
        if argv[0] == "wait":
            return 0, "255\n", ""
        return 0, json.dumps({"Running": False, "ExitCode": 255, "OOMKilled": False}), ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    watcher = await memory.GuestEvents.start(
        ["sh", "-c", "echo oom 0 oom_kill 0; sleep 0.3; echo oom 0 oom_kill 13"],
        counter="its cgroup",
    )
    session = _Session(["sh", "-c", "echo {}; sleep 0.1"], "ale-e1", 512, watcher)
    await session.start()
    assert await session.recv() == "{}"
    with pytest.raises(GuestUnreachableError) as ended:
        await session.recv()
    explained = await session.explain(ended.value)
    await watcher.close()
    assert isinstance(explained, MemoryExhaustedError)
    assert str(explained) == (
        "the sandbox itself died after an OOM kill in it (its Task declares 512 MiB): its "
        "cgroup counted 13 OOM kills and 0 times its memory reached its limit, and its main "
        "process exited with code 255 (guest session closed)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("oom_killed", "sentry", "evidence"),
    [
        (False, 3, "the Sentry counted 3 OOM kills"),
        # The Host's OOM killer acts on memory the Sentry does not see, which Docker records.
        (True, 0, "Docker recorded OOMKilled"),
        (True, 3, "Docker recorded OOMKilled; the Sentry counted 3 OOM kills"),
    ],
)
async def test_a_gvisor_sandbox_lost_after_an_oom_kill_says_which_kernel_made_it(
    monkeypatch: pytest.MonkeyPatch, oom_killed: bool, sentry: int, evidence: str
) -> None:
    """The Sentry's kills reach neither the container's cgroup on the Host nor Docker: its
    watcher counts them, and Docker records those of the Host's OOM killer."""

    async def docker(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
        if argv[0] == "wait":
            return 0, "137\n", ""
        return 0, json.dumps({"Running": False, "ExitCode": 137, "OOMKilled": oom_killed}), ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    counts = "oom_kill_disable 0 under_oom 0 oom_kill"
    watcher = await memory.GuestEvents.start(
        ["sh", "-c", f"echo {counts} 0; sleep 0.3; echo {counts} {sentry}"], counter="the Sentry"
    )
    session = _Session(["sh", "-c", "echo {}; sleep 0.1"], "ale-e1", 512, watcher)
    await session.start()
    assert await session.recv() == "{}"
    with pytest.raises(GuestUnreachableError) as ended:
        await session.recv()
    explained = await session.explain(ended.value)
    await watcher.close()
    assert isinstance(explained, MemoryExhaustedError)
    assert str(explained) == (
        f"the sandbox itself died after an OOM kill in it (its Task declares 512 MiB): "
        f"{evidence}, and its main process exited with code 137 (guest session closed)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stopped",
    [
        GuestUnreachableError("the sandbox itself died: ALE's GPU memory watchdog stopped it"),
        StorageExhaustedError(
            "ALE stopped the sandbox: it wrote 1025 MiB over its image, past the 1024 MiB its "
            "Task declares"
        ),
    ],
)
async def test_a_sandbox_ale_stopped_is_no_memory_death_whatever_its_memory(
    monkeypatch: pytest.MonkeyPatch, stopped: GuestUnreachableError
) -> None:
    """The GPU watchdog's stop of a gVisor Sandbox whose GPU memory limit failed, or the
    storage watch's of one that wrote past its storage_mb, is ALE's doing, whatever Docker
    recorded: its OOMKilled, which an earlier kill may have set, is not even read."""
    calls = _scripted(monkeypatch)
    session = _Session(["sh", "-c", "echo {}"], "ale-e1", 512)
    session.stopped = stopped
    await session.start()
    assert await session.recv() == "{}"
    with pytest.raises(GuestUnreachableError) as ended:
        await session.recv()
    assert await session.explain(ended.value) is stopped
    assert calls == []


@pytest.mark.asyncio
async def test_a_death_is_explained_whatever_the_timeout_of_the_call_it_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call's timeout bounds guestd's answer; finding out why there is none runs after
    it, so a death that takes longer to read than the call allows still says why."""

    async def docker(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
        if argv[0] == "wait":
            await asyncio.sleep(0.5)
            return 0, "137\n", ""
        return 0, json.dumps({"Running": False, "OOMKilled": False, "ExitCode": 137}), ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    reply = json.dumps({"id": 1, "ok": True, "data": {}})
    guestd = f"read request; echo '{reply}'; read request; echo urpc EOF >&2"
    session = _Session(["sh", "-c", guestd], "ale-e1", 512)
    client = GuestClient(session)
    await session.start()
    try:
        await client.call("health", timeout_sec=10)
        with pytest.raises(GuestUnreachableError) as ended:
            await client.call("health", timeout_sec=0.2)
    finally:
        await client.close()
    assert str(ended.value) == (
        "the sandbox itself died: it was killed from outside (guest session closed: urpc EOF)"
    )


@pytest.mark.asyncio
async def test_a_guestd_that_fails_to_start_says_why_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its session ends before it ever answers while the container runs on: its stderr is
    the reason, at once, with no wait on the container."""
    asked: list[tuple[str, ...]] = []

    async def docker(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
        asked.append(argv)
        await asyncio.sleep(timeout)  # the container runs on
        raise ProviderStartError(f"docker {argv[0]} timed out after {timeout:g}s")

    class Failing(_Session):
        def __init__(self, _argv: list[str], container: str, *args: Any) -> None:
            argv = ["sh", "-c", "echo 'guestd: bad interpreter' >&2; exit 1"]
            super().__init__(argv, container, *args)

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    monkeypatch.setattr("ale.run.providers.docker.provider._Session", Failing)
    started = time.monotonic()
    with pytest.raises(ProviderStartError) as failed:
        await DockerProvider()._connect("ale-e1", "user", 512)
    assert str(failed.value) == (
        "guest service did not answer: guest session closed: guestd: bad interpreter"
    )
    assert time.monotonic() - started < 5 and asked == []


@pytest.mark.asyncio
async def test_a_session_ale_ends_or_that_never_answered_is_no_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing it, as ALE does before it destroys, keeps or snapshots a Sandbox, or using it
    unstarted asks nothing of Docker."""
    calls = _scripted(monkeypatch)
    unstarted = _Session(["cat"], "ale-e1", 512)
    with pytest.raises(GuestUnreachableError, match="transport is not started") as raised:
        await unstarted.recv()
    assert await unstarted.explain(raised.value) is raised.value
    session = _Session(["sh", "-c", "echo {}; cat"], "ale-e1", 512)
    await session.start()
    assert await session.recv() == "{}"
    pending = asyncio.ensure_future(session.recv())
    await asyncio.sleep(0.1)
    await session.close()
    with pytest.raises(GuestUnreachableError, match="guest session closed") as raised:
        await pending
    assert await session.explain(raised.value) is raised.value
    assert calls == []


@pytest.mark.asyncio
async def test_guestd_is_reached_isolated_in_the_root_directory_through_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def started(_self: object) -> None:
        return None

    async def healthy(_self: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(_Session, "start", started)
    monkeypatch.setattr("ale.run.transport.GuestClient.health", healthy)
    transport = await DockerProvider()._connect("ale-e1", "user", 512)
    assert transport.client._transport is transport and transport.container == "ale-e1"
    assert transport._argv == [
        "docker", "exec", "-i", "--user", "0", "--env", "ALE_AGENT_USER=user",
        "--workdir", "/", "ale-e1",
        "/opt/ale/python/bin/python3", "-I", "/opt/ale/guestd/main.py", "--stdio",
    ]  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize("desktop", [True, False])
async def test_an_allowlist_seal_relays_the_agent_once_the_container_is_confined(
    monkeypatch: pytest.MonkeyPatch, desktop: bool
) -> None:
    """The relay starts once the container is off the bridge, and on a desktop is the
    account's system proxy too; it stops, the account's settings back, before it rejoins."""
    calls = _scripted(monkeypatch)
    seen: list[dict[str, str] | None] = []

    async def guest_exec(_argv: list[str], **kwargs: object) -> tuple[int, str, str, bool]:
        seen.append(kwargs.get("env"))  # type: ignore[arg-type]
        return 0, "", "", False

    async def egress_relay(
        proxy: str | None, *, bypass: object = (), desktop: bool = False
    ) -> str | None:
        calls.append(("egress_relay", str(proxy), str(bypass), str(desktop)))
        return "127.0.0.1:41999" if proxy else None

    sandbox = _sandbox()
    sandbox.container = "ale-e1"
    sandbox.request = request(
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("a.b",)),
        proxy_url="http://0.0.0.0:9443",
        proxy_token="t0ken",
    )
    sandbox.allocation = sandbox.allocation.model_copy(
        update={"desktop_resolution": DesktopResolution() if desktop else None}
    )
    sandbox.confinement = _RuncConfinement("ale-net-e1", "172.18.0.1")
    sandbox._sealed = False
    sandbox._client = SimpleNamespace(exec=guest_exec, egress_relay=egress_relay)  # type: ignore[assignment]

    await sandbox.close_egress()
    await sandbox.exec(["true"], identity=Identity.AGENT)
    await sandbox.open_egress()
    await sandbox.exec(["true"], identity=Identity.AGENT)

    bypass = str([HOST_ALIAS, "localhost", "127.0.0.1"])
    assert calls == [
        ("network", "disconnect", "bridge", "ale-e1"),
        ("egress_relay", f"http://t0ken:@{HOST_ALIAS}:9443", bypass, str(desktop)),
        ("egress_relay", "None", "()", "False"),
        ("network", "connect", "bridge", "ale-e1"),
    ]
    assert seen[0] is not None and seen[0]["HTTPS_PROXY"] == "http://127.0.0.1:41999"
    assert seen[0]["NO_PROXY"] == f"{HOST_ALIAS},localhost,127.0.0.1"
    assert seen[1] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "runtime", "networks", "drop", "sealed"),
    [
        (NetworkMode.ALLOWLIST, "runc", ("ale-net-e1",), False, True),
        (NetworkMode.ALLOWLIST, "runc", ("ale-net-e1", "bridge"), False, False),
        (NetworkMode.OPEN, "runc", ("bridge",), False, False),
        (NetworkMode.ALLOWLIST, kata.RUNTIME, ("ale-net-e1",), True, True),
        (NetworkMode.ALLOWLIST, kata.RUNTIME, ("ale-net-e1",), False, False),
        (NetworkMode.OPEN, kata.RUNTIME, ("ale-net-e1",), True, False),
        (NetworkMode.ALLOWLIST, gvisor.RUNTIME, ("ale-net-e1",), True, True),
        (NetworkMode.ALLOWLIST, gvisor.NET_RAW, ("ale-net-e1",), True, True),
        (NetworkMode.ALLOWLIST, gvisor.RUNTIME, ("ale-net-e1",), False, False),
        (NetworkMode.OPEN, gvisor.RUNTIME, ("ale-net-e1",), True, False),
    ],
)
@pytest.mark.parametrize("asked", ["same", "open"])
async def test_a_reattached_sandbox_is_sealed_as_its_container_was_left(
    monkeypatch: pytest.MonkeyPatch,
    mode: NetworkMode,
    runtime: str,
    networks: tuple[str, ...],
    drop: bool,
    sealed: bool,
    asked: str,
) -> None:
    """An allowlist agent's commands get the proxy only while the Sandbox is sealed, which
    the container says, not the process driving it again: off the default bridge under runc,
    with the seal's drop filter on the VM's tap under Kata and on the veth under gVisor,
    whichever of its runtimes it runs under. Its own network mode, which it was started
    with, decides how it is confined, whatever the reattaching run asks."""
    record = {
        "Name": "/ale-e1",
        "Image": DIGEST,
        "Config": {
            "Labels": {
                "ale.managed": "true",
                "ale.role": "solver",
                "ale.network": mode.value,
                "ale.bridge-mac": BRIDGE_MAC,
            }
        },
        "State": {"Running": True},
        "HostConfig": {"Runtime": runtime},
        "NetworkSettings": {"Networks": {name: {} for name in networks}},
    }
    dropping = (  # as the helper image's tc (iproute2 6.15) shows it
        "filter protocol all flower chain 0 \n"
        "filter protocol all flower chain 0 handle 0x1 \n"
        "  not_in_hw\n"
        "\taction order 1: gact action drop\n"
    )
    calls: list[tuple[str, ...]] = []
    seen: list[dict[str, str] | None] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[0] == "inspect" and "MacAddress" not in " ".join(argv):
            return 0, json.dumps([record]), ""
        if (
            argv[0] == "run"
            and "--network" in argv
            and argv[argv.index("--network") + 1] != ("container:ale-e1")
        ):
            raise AssertionError(f"a helper joined the running sandbox's network: {argv}")
        if argv[-1].startswith("tc filter show"):
            return 0, dropping if drop else "", ""
        return _scripted_reply(argv)

    async def guest_exec(argv: list[str], **kwargs: object) -> tuple[int, str, str, bool]:
        seen.append(kwargs.get("env"))  # type: ignore[arg-type]
        return 0, "", "", False

    async def close() -> None:
        pass

    relays: list[str | None] = []

    async def egress_relay(proxy: str | None, **_: object) -> str | None:
        relays.append(proxy)
        return "127.0.0.1:41999" if proxy else None

    async def connect(_provider: DockerProvider, _container: str, *_: object) -> _Session:
        session = _Session([], "ale-e1", 1024)
        session.client = SimpleNamespace(exec=guest_exec, close=close, egress_relay=egress_relay)  # type: ignore[assignment]
        return session

    async def contract(_provider: DockerProvider, _client: object) -> tuple[str, bool]:
        return "/home/user", False

    async def no_watcher(*_: object) -> None:
        """Kata's memory watcher, which runs in the guest."""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    monkeypatch.setattr(DockerProvider, "_connect", connect)
    monkeypatch.setattr(DockerProvider, "_guest_contract", contract)
    monkeypatch.setattr("ale.run.providers.docker.provider._watcher", no_watcher)
    network = (
        NetworkPolicy(mode=mode, allowed_hosts=("a.b",))
        if mode is NetworkMode.ALLOWLIST
        else NetworkPolicy(mode=mode)
    )
    if asked == "open":
        network = NetworkPolicy(mode=NetworkMode.OPEN)
    wanted = request(network=network, proxy_url="http://0.0.0.0:9443")
    allocation = ResourceAllocation(
        cpus=1,
        memory_mb=1024,
        sudo=False,
        network_mode=mode,
        provider="docker:runc",
        leaf="docker:runc",
    )
    image = ResolvedImage(
        kind="container", prepared_identity=DIGEST, observed_identity=DIGEST, observed_ref=DIGEST
    )
    sandbox = await DockerProvider().reattach("docker:ale-e1", wanted, allocation, image)
    await sandbox.exec(["true"], identity=Identity.AGENT)
    assert (seen[-1] is not None) is sealed
    # The guestd that relayed the run which sealed it is gone; this run relays from its own.
    assert relays == ([f"http://{HOST_ALIAS}:9443"] if sealed else [])
    assert seen[-1] is None or seen[-1]["HTTPS_PROXY"] == "http://127.0.0.1:41999"
    # The seal is read once, by the helper.
    read = [call[-1] for call in calls if call[-1].startswith("tc filter show")]
    isolated = mode is not NetworkMode.OPEN
    probes = {kata.RUNTIME: kata.SEAL_PROBE, gvisor.RUNTIME: gvisor.SEAL_PROBE}
    probes[gvisor.NET_RAW] = gvisor.SEAL_PROBE
    assert read == ([probes[runtime]] if isolated and runtime in probes else [])
    if runtime in (gvisor.RUNTIME, gvisor.NET_RAW):
        # Its netstack chains are written again, sealed as the veth's filters say.
        (written,) = [json.loads(call[-1]) for call in calls if call[0] == "exec"]
        assert bool(written["tables"]["filter"].get("chains", {}).get("ALE_SEAL")) is sealed


@pytest.mark.asyncio
async def test_a_container_that_records_no_network_mode_is_not_reattached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {
        "Name": "/ale-e1",
        "Image": DIGEST,
        "Config": {"Labels": {"ale.managed": "true", "ale.role": "solver"}},
        "State": {"Running": True},
        "HostConfig": {"Runtime": "runc"},
        "NetworkSettings": {"Networks": {"bridge": {}}},
    }

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        return 0, json.dumps([record]), ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    allocation = ResourceAllocation(
        cpus=1, memory_mb=1024, sudo=False, network_mode=NetworkMode.BLOCK,
        provider="docker:runc", leaf="docker:runc",
    )  # fmt: skip
    image = ResolvedImage(
        kind="container", prepared_identity=DIGEST, observed_identity=DIGEST, observed_ref=DIGEST
    )
    with pytest.raises(ProviderCapabilityError, match="records no network mode"):
        await DockerProvider().reattach("docker:ale-e1", request(), allocation, image)


@pytest.mark.asyncio
async def test_kata_refuses_gpus_before_anything_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted(monkeypatch)
    with pytest.raises(ProviderCapabilityError, match=r"docker:kata .*: attaches no GPUs"):
        await DockerProvider(runtime="kata").create(request(resources=Resources(gpus=1)))
    assert calls == []


async def test_each_runtime_is_a_leaf_with_the_features_it_gives() -> None:
    (runc,) = await DockerProvider().profiles()
    (in_kata,) = await DockerProvider(runtime="kata").profiles()
    # A Sandbox runs under one of gVisor's registrations: raw sockets or a container engine.
    raw, nested = await DockerProvider(runtime="gvisor").profiles()
    assert (runc.provider, in_kata.provider) == ("docker:runc", "docker:kata")
    for in_gvisor in (raw, nested):
        assert in_gvisor.provider == "docker:gvisor" and in_gvisor.gui
    # A snapshot keeps a container engine's storage, which is on volumes, as copies.
    assert raw.snapshot and nested.snapshot
    common = {ContainerFeature.USER_NAMESPACES, ContainerFeature.FUSE, ContainerFeature.MOUNT}
    assert raw.container_features == {*common, ContainerFeature.RAW_SOCKETS}
    assert nested.container_features == {*common, ContainerFeature.NESTED_CONTAINERS}
    assert runc.container_features == {ContainerFeature.RAW_SOCKETS}
    assert in_kata.container_features == {
        ContainerFeature.PERF,
        ContainerFeature.BPF,
        ContainerFeature.IO_URING,
        ContainerFeature.RAW_SOCKETS,
        ContainerFeature.USER_NAMESPACES,
        ContainerFeature.MOUNT,
    }
    # The name keeps naming its handles and GPU locks, whatever the runtime.
    assert DockerProvider(runtime="kata").name == "docker"


def test_every_docker_runtime_is_a_provider_id_a_run_can_name() -> None:
    named = {name for name in get_args(ProviderId) if name.startswith("docker:")}
    assert named == {f"docker:{runtime}" for runtime in RUNTIMES}


async def test_a_leaf_offers_the_gpus_of_this_host_that_it_may_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GPU Task goes to a leaf that attaches a GPU it accepts, or to the next Provider."""
    a100 = "NVIDIA A100-SXM4-80GB"
    found: list[int] = []

    async def host_gpus(_provider: DockerProvider) -> dict[int, GpuDevice]:
        found.append(1)
        models = {0: (a100, 81920), 1: (H100, 81559), 2: (a100, 81920)}
        return {
            index: GpuDevice(
                id=f"GPU-{index}",
                model=model,
                driver_version="570.1",
                memory_total_mb=memory,
                memory_reserved_mb=470,
            )
            for index, (model, memory) in models.items()
        }

    (none,) = await DockerProvider().profiles()
    assert (none.gpus, none.gpu_models, none.gpu_hold) == (False, (), GpuHold.WHOLE)
    monkeypatch.setattr(DockerProvider, "_host_gpus", host_gpus)
    every = DockerProvider()
    (runc,) = await every.profiles()
    # Each model's memory is the most a shared Sandbox is admitted with on an empty GPU of it:
    # its total less what the driver reserves and the default margin of 4096 MiB.
    models = (GpuModel(name=a100, memory_mb=77354), GpuModel(name=H100, memory_mb=76993))
    assert (runc.gpus, runc.gpu_models) == (True, models)
    # runc holds no process to its share of a GPU: the watchdog watches it.
    assert runc.gpu_hold is GpuHold.WATCHED
    await every.profiles()
    assert found == [1]  # read once
    (configured,) = await DockerProvider(gpus=(0, 2)).profiles()
    assert configured.gpu_models == models[:1]
    h100 = Resources(gpus=1, gpu_types=("H100",))
    assert await DockerProvider(gpus=(0, 2)).unserved(
        request(resources=h100).needs, user="user", role=SandboxRole.SHARED
    ) == (f"attaches {a100} (77354 MiB), none of gpu_types ['H100']",)
    # A need admission could never admit is refused before anything starts, though the GPU
    # has that much memory; one it could admit is not.
    big = Resources(gpus=1, gpu_memory_mb=77000)
    assert (
        await every.unserved(request(resources=big).needs, user="user", role=SandboxRole.SHARED)
        == ()
    )
    assert await DockerProvider(gpus=(1,)).unserved(
        request(resources=big).needs, user="user", role=SandboxRole.SHARED
    ) == (f"attaches {H100} (76993 MiB), none with gpu_memory_mb 77000",)
    tighter = DockerProvider(gpus=(1,), gpu_memory_margin_mb=8192)
    smaller = request(resources=big.model_copy(update={"gpu_memory_mb": 73000}))
    assert await tighter.unserved(smaller.needs, user="user", role=SandboxRole.SHARED) == (
        f"attaches {H100} (72897 MiB), none with gpu_memory_mb 73000",
    )
    # Docker's GPU injection does not reach a Kata microVM; gVisor proxies the driver and
    # limits a shared Sandbox's memory.
    (in_kata,) = await DockerProvider(runtime="kata").profiles()
    assert (in_kata.gpus, in_kata.gpu_models, in_kata.gpu_hold) == (False, (), GpuHold.WHOLE)
    for in_gvisor in await DockerProvider(runtime="gvisor").profiles():
        assert (in_gvisor.gpus, in_gvisor.gpu_models) == (True, models)
        assert in_gvisor.gpu_hold is GpuHold.LIMIT


@pytest.mark.parametrize("storage_mb", [2048, None])
async def test_every_leaf_holds_a_sandbox_to_storage_mb_but_on_an_engines_volumes(
    monkeypatch: pytest.MonkeyPatch, storage_mb: int | None
) -> None:
    """Each runtime's writes land in the writable layer the storage watch measures, a
    container engine's on volumes it measures too; Docker is asked for no size, which its
    containerd image store ignores and rootless Docker refuses."""
    sized = request(resources=Resources(storage_mb=1024))
    for runtime in RUNTIMES:
        leaf = DockerProvider(runtime=runtime)
        assert all(profile.storage_limit for profile in await leaf.profiles())
        assert await leaf.unserved(sized.needs, user="user", role=SandboxRole.SHARED) == ()
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:2] == ("run", "-d"):
            return 1, "", "stopped after the flags were read"
        return _scripted_reply(argv)

    image = ResolvedImage(
        kind="container",
        prepared_identity=DIGEST,
        observed_identity=DIGEST,
        observed_ref="image@" + DIGEST,
    )

    async def resolved(_prepared: PreparedTaskImage) -> ResolvedImage:
        return image

    async def no_problems(_reference: str) -> list[str]:
        return []

    async def framed(found: ResolvedImage, _request: SandboxRequest) -> ResolvedImage:
        return found

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    monkeypatch.setattr(
        "ale.run.providers.docker.provider.resolve_prepared_container_image", resolved
    )
    provider = DockerProvider()
    monkeypatch.setattr(provider, "check_image", no_problems)
    monkeypatch.setattr(provider, "_framed", framed)
    asked = request(
        network=NetworkPolicy(mode=NetworkMode.OPEN), resources=Resources(storage_mb=storage_mb)
    )
    with pytest.raises(ProviderStartError, match="stopped after the flags were read"):
        await provider.create(asked)
    (run,) = [call for call in calls if call[:2] == ("run", "-d")]
    assert "--storage-opt" not in run


def _measured(
    monkeypatch: pytest.MonkeyPatch, *replies: tuple[int, str, str]
) -> list[tuple[str, ...]]:
    """What the storage watch asks of a scripted docker CLI, which gives ``replies`` in turn
    and then the last forever, with no wait between measurements."""
    asked: list[tuple[str, ...]] = []
    left = list(replies)

    async def run(*argv: str, **_: object) -> tuple[int, str, str]:
        asked.append(argv)
        return left.pop(0) if len(left) > 1 else left[0]

    monkeypatch.setattr(storage, "run", run)
    monkeypatch.setattr(storage, "WATCH_SEC", 0.0)
    return asked


async def _until(check: Any, timeout_sec: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "it never happened"
        await asyncio.sleep(0.01)


def _sized_sandbox(storage_mb: int = 1024) -> DockerSandbox:
    sandbox = _sandbox()
    sandbox.allocation = sandbox.allocation.model_copy(update={"storage_mb": storage_mb})
    sandbox._sessions = (
        _Session([], sandbox.container, 1024),
        _Session([], sandbox.container, 1024),
    )
    return sandbox


async def test_a_sandbox_booted_from_a_snapshot_counts_what_the_snapshot_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its storage_mb holds what the snapshot wrote over the Task's image as well as its own
    writes, as a disk of that size would."""
    calls = _scripted(monkeypatch)
    _measured(monkeypatch, (0, f"{200 << 20}\n", ""))
    sandbox = _sized_sandbox()
    sandbox.written = 900 << 20
    sandbox.watch()
    await _until(lambda: ("kill", sandbox.container) in calls)
    ended = await sandbox._sessions[0].explain(GuestUnreachableError("guest session closed"))
    assert str(ended) == (
        "ALE stopped the sandbox: it and the snapshot it booted from wrote 1100 MiB over its "
        "image, past the 1024 MiB its Task declares"
    )
    await sandbox._release()


async def test_a_sandbox_that_writes_past_its_storage_is_stopped_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured from outside until what it wrote over its image passes storage_mb: then the
    stop is logged in the phase of the command that ran when it was found, before the kill
    ends that command and with it, maybe, the episode; its next call raises
    StorageExhaustedError naming both sizes, and nothing measures it any more. Up to the
    limit itself, it goes on."""
    calls: list[tuple[str, ...]] = []
    sink = TraceSink()
    logged_at_kill: list[int] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        logged_at_kill.append(len(sink.events))
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    asked = _measured(
        monkeypatch,
        (0, f"{500 << 20}\n", ""),
        (0, f"{1024 << 20}\n", ""),
        (0, f"{(1024 << 20) + 4096}\n", ""),
    )
    sandbox = _sized_sandbox()
    with execution_logging(sink, episode_id="e1", phase="agent", component="test"):
        sandbox._context = contextvars.copy_context()
    sandbox.watch()
    await _until(lambda: ("kill", sandbox.container) in calls)
    await asyncio.sleep(0.05)
    assert logged_at_kill == [1]
    assert (
        asked
        == [("docker", "container", "inspect", "--size", "--format", "{{json .SizeRw}}", "f" * 64)]
        * 3
    )
    assert calls == [("kill", sandbox.container)]
    stopped = (
        "ALE stopped the sandbox: it wrote 1025 MiB over its image, past the 1024 MiB its "
        "Task declares"
    )
    for session in sandbox._sessions:
        ended = await session.explain(GuestUnreachableError("guest session closed"))
        assert isinstance(ended, StorageExhaustedError)
        assert str(ended) == stopped
        # A call made before the kill takes effect asks nothing of the Sandbox.
        with pytest.raises(StorageExhaustedError, match="past the 1024 MiB its Task declares"):
            await session.client.exec(["true"])
    (logged,) = sink.events
    assert (logged.level, logged.phase) == ("warning", "agent")
    assert logged.message == (
        "ALE's storage watch stopped the Sandbox: it wrote 1025 MiB over its image, past the "
        "1024 MiB its Task declares"
    )
    assert logged.data == {"written_mb": 1025, "storage_mb": 1024}
    await sandbox._release()


async def test_the_storage_watch_measures_only_while_a_sandbox_is_driven(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A Sandbox that names no storage_mb is not measured; one that does is until ALE lets
    go of it or it is gone, and a measurement that fails is warned of once and tried again."""
    calls = _scripted(monkeypatch)
    asked = _measured(monkeypatch, (0, "0\n", ""))
    plain = _sandbox()
    plain.watch()
    assert plain._storage is None
    sandbox = _sized_sandbox()
    sandbox.watch()
    await _until(lambda: len(asked) >= 3)
    await sandbox._release()
    measured = len(asked)
    await asyncio.sleep(0.05)
    assert len(asked) == measured
    assert ("kill", sandbox.container) not in calls
    asked = _measured(
        monkeypatch,
        (1, "", "Cannot connect to the Docker daemon"),
        (1, "", "Cannot connect to the Docker daemon"),
        (0, "0\n", ""),
        (1, "", "Error response from daemon: No such container: " + "f" * 64),
    )
    sandbox = _sized_sandbox()
    with caplog.at_level("WARNING", logger="ale.run.providers.docker.storage"):
        sandbox.watch()
        await _until(lambda: sandbox._storage is not None and sandbox._storage._task.done())
    assert len(asked) == 4
    assert [record.getMessage() for record in caplog.records] == [
        f"cannot measure what {'f' * 64} wrote; goes on trying",
        f"measures what {'f' * 64} wrote again",
    ]
    assert ("kill", sandbox.container) not in calls
    await sandbox._release()


async def test_a_docker_leaf_on_an_arm64_machine_serves_no_linux_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ALE's Linux images and the Tasks built on them are x86-64, which Docker runs
    natively."""
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    assert await DockerProvider().unserved(
        request().needs, user="user", role=SandboxRole.SHARED
    ) == ("runs arm64, not x86_64",)


_RUNSC = "/usr/local/lib/ale-gvisor/0.1.4/runsc"
_REGISTERED = dict(gvisor.ARGUMENTS)
#: Part of what ``runsc flags`` of ALE's build lists.
_FLAGS = (
    "  -nvproxy-driver-version string\n"
    "  -nvproxy-gpu-memory-limit uint\n"
    "    \tthe most device memory, in bytes, that the sandbox may allocate through nvproxy\n"
)


def _known(*names: str, arguments: dict[str, tuple[str, ...]] | None = None) -> str:
    """``docker info``'s Runtimes: each name registered by path, some with arguments."""
    known: dict[str, dict[str, object]] = {name: {"path": name} for name in names}
    for name, args in (arguments or {}).items():
        known[name] = {"path": _RUNSC, "runtimeArgs": list(args)}
    return json.dumps(known)


def _preflight_host(
    monkeypatch: pytest.MonkeyPatch,
    known: str,
    *,
    shims: tuple[str, ...] = (),
    devices: tuple[str, ...] = (),
    version: str = "runsc version release-20261109.0+ale.0.1.4\nspec: 1.2.1\n",
    drivers: str = "570.124.06\n580.95.05\n",
    flags: str = _FLAGS,
    flags_code: int = 0,
    gpus: bool = True,
) -> list[tuple[str, ...]]:
    """A Host whose daemon knows ``known``; what it ran of a runtime's own commands."""
    ran: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        assert argv == ("info", "--format", "{{json .Runtimes}}")
        return 0, known, ""

    async def run(*argv: str, timeout: float) -> tuple[int, str, str]:
        ran.append(argv)
        if argv[1] == "flags":
            return flags_code, "", flags  # Go's flag package writes them to stderr
        return 0, version if argv[1] == "--version" else drivers, ""

    async def host_gpus(_provider: DockerProvider) -> dict[int, GpuDevice]:
        if not gpus:
            raise ProviderCapabilityError("nvidia-smi is not installed on the Docker host")
        h100 = GpuDevice(id="GPU-0", model="NVIDIA H100 80GB HBM3", driver_version="570.124.06")
        return {0: h100}

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    monkeypatch.setattr("ale.run.providers.docker.provider.run", run)
    monkeypatch.setattr(DockerProvider, "_host_gpus", host_gpus)
    monkeypatch.setattr(
        "ale.run.providers.docker.provider.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "docker" or name in shims else None,
    )
    monkeypatch.setattr(
        "ale.run.providers.docker.provider.Path.exists", lambda path: str(path) in devices
    )
    return ran


@pytest.mark.parametrize(
    ("runtime", "known", "shims", "devices", "missing"),
    [
        ("runc", _known("io.containerd.runc.v2", "runc"), (), (), None),
        ("runc", _known("io.containerd.runc.v2", "runsc"), (), (), "needs the runc runtime"),
        ("kata", _known("runc"), ("containerd-shim-kata-v2",), ("/dev/kvm",), None),
        ("kata", _known("runc"), (), ("/dev/kvm",), "needs the io.containerd.kata.v2 runtime"),
        ("kata", _known("runc"), ("containerd-shim-kata-v2",), (), "needs /dev/kvm"),
    ],
)
async def test_a_leafs_preflight_finds_its_runtime(
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
    known: str,
    shims: tuple[str, ...],
    devices: tuple[str, ...],
    missing: str | None,
) -> None:
    """A leaf whose runtime is missing here refuses the Run before any Sandbox starts."""
    _preflight_host(monkeypatch, known, shims=shims, devices=devices)
    preflight = DockerProvider(runtime=runtime).preflight()
    if missing is None:
        assert await preflight is None  # nothing left for a Sandbox to show
    else:
        with pytest.raises(ProviderStartError, match=missing):
            await preflight


_PLAIN = gvisor.ARGUMENTS[gvisor.RUNTIME]
_RAW = gvisor.ARGUMENTS[gvisor.NET_RAW]
_NESTED = gvisor.ARGUMENTS[gvisor.NESTED]
_ENGINE = ContainerFeature.NESTED_CONTAINERS


@pytest.mark.parametrize(
    ("arguments", "features", "missing"),
    [
        (_REGISTERED, (), None),
        # In any order.
        ({name: tuple(reversed(args)) for name, args in _REGISTERED.items()}, None, None),
        ({gvisor.RUNTIME: _PLAIN}, (), None),
        ({gvisor.RUNTIME: _PLAIN}, (ContainerFeature.RAW_SOCKETS,), "needs the runsc-net-raw"),
        ({gvisor.RUNTIME: _PLAIN, gvisor.NESTED: _NESTED}, (_ENGINE,), None),
        ({gvisor.RUNTIME: _PLAIN, gvisor.NET_RAW: _RAW}, (_ENGINE,), "needs the runsc-nested"),
        # A Node lends it to any Sandbox.
        ({gvisor.RUNTIME: _PLAIN}, None, "needs the runsc-nested runtime"),
        ({gvisor.RUNTIME: _PLAIN, gvisor.NET_RAW: _RAW}, None, "needs the runsc-nested runtime"),
        ({gvisor.RUNTIME: _PLAIN, gvisor.NESTED: _NESTED}, None, "needs the runsc-net-raw runtime"),
        ({gvisor.RUNTIME: _PLAIN[1:]}, (), "runsc runtime registered with --overlay2=none"),
        # No more than its arguments, an operator's GPU memory limit included.
        (
            {**_REGISTERED, gvisor.RUNTIME: (*_PLAIN, "--nvproxy-gpu-memory-limit=1")},
            (),
            "not --overlay2=none --allow-suid",
        ),
        (
            {**_REGISTERED, gvisor.NET_RAW: (*_RAW, "--allow-packet-socket-write")},
            (ContainerFeature.RAW_SOCKETS,),
            "runsc-net-raw runtime registered with",
        ),
        # Raw sockets break a dockerd that may open a packet socket but not write one.
        (
            {**_REGISTERED, gvisor.NESTED: (*_NESTED, "--net-raw")},
            (_ENGINE,),
            "runsc-nested runtime registered with",
        ),
    ],
)
async def test_the_gvisor_preflight_finds_the_runtimes_its_sandboxes_need(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, tuple[str, ...]],
    features: tuple[ContainerFeature, ...] | None,
    missing: str | None,
) -> None:
    """Registered with exactly the arguments its README gives: the raw sockets or the
    container engine runtime only when a Sandbox needs it, or for a Node, which lends the
    leaf to any."""
    ran = _preflight_host(monkeypatch, _known("runc", arguments=arguments), gpus=False)
    needs = (
        ()
        if features is None
        else [request(resources=Resources(container_features=features)).needs]
    )
    preflight = DockerProvider(runtime="gvisor").preflight(needs)
    if missing is not None:
        with pytest.raises(ProviderStartError, match=missing):
            await preflight
        return
    assert await preflight is not None  # what only a Sandbox of it shows
    assert ran == [(_RUNSC, "--version")]  # one binary, under every name


@pytest.mark.parametrize(
    ("version", "drivers", "needs_gpus", "refused"),
    [
        ("runsc version release-20261109.0\nspec: 1.2.1\n", "", 0, "needs ALE's build"),
        ("runsc version release-20261109.0+ale.0.1.41\n", "", 0, "ale.0.1.4 of"),
        ("runsc version release-20261109.0+ale.0.1.1\n", "", 0, "ale.0.1.4 of"),
        ("runsc version release-20261109.0+ale.0.1.4\n", "580.95.05\n", 0, None),
        ("runsc version release-20261109.0+ale.0.1.4\n", "580.95.05\n", 1, "driver 570.124.06"),
        ("runsc version release-20261109.0+ale.0.1.4\n", "570.124.06\n", 1, None),
        # With no needs, as a Node lends it, its profile offers the Host's GPUs.
        ("runsc version release-20261109.0+ale.0.1.4\n", "580.95.05\n", None, "570.124.06"),
    ],
)
async def test_a_gvisor_leaf_runs_the_build_of_ale_which_must_support_the_gpu_driver_here(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    drivers: str,
    needs_gpus: int | None,
    refused: str | None,
) -> None:
    """A stock runsc lacks ALE's patches, and another build of ALE's is not the one this
    engine was tested with; the GPU driver is checked whenever GPUs may be attached."""
    ran = _preflight_host(
        monkeypatch, _known("runc", arguments=_REGISTERED), version=version, drivers=drivers
    )
    needs = [] if needs_gpus is None else [request(resources=Resources(gpus=needs_gpus)).needs]
    preflight = DockerProvider(runtime="gvisor").preflight(needs)
    if refused is not None:
        with pytest.raises((ProviderStartError, ProviderCapabilityError), match=refused):
            await preflight
    else:
        await preflight
    if needs_gpus != 0 and "+ale.0.1.4\n" in version:
        assert ran[-1] == (_RUNSC, "nvproxy", "list-supported-drivers")
    else:
        assert all(argv[1] == "--version" for argv in ran)


@pytest.mark.parametrize(
    ("memory", "flags", "code", "refused"),
    [
        (None, "", 0, None),  # a Sandbox that takes whole GPUs needs no limit
        (8192, _FLAGS, 0, None),
        (8192, "  -nvproxy-driver-version string\n", 0, "lists no -nvproxy-gpu-memory-limit"),
        # A runsc whose flags cannot be listed is refused as one that lacks the flag.
        (8192, "flag provided but not defined", 2, "lists no -nvproxy-gpu-memory-limit"),
        # A Node lends the leaf to any Sandbox, shared ones too.
        ("node", "", 0, "lists no -nvproxy-gpu-memory-limit"),
    ],
)
async def test_a_gvisor_leaf_whose_sandboxes_share_gpus_runs_a_runsc_that_limits_them(
    monkeypatch: pytest.MonkeyPatch,
    memory: int | str | None,
    flags: str,
    code: int,
    refused: str | None,
) -> None:
    """``runsc flags`` lists the flag patch 0004 adds, which a shared Sandbox's annotation
    sets."""
    ran = _preflight_host(
        monkeypatch,
        _known("runc", arguments=_REGISTERED),
        drivers="570.124.06\n",
        flags=flags,
        flags_code=code,
    )
    needs = (
        []
        if memory == "node"
        else [request(resources=Resources(gpus=1, gpu_memory_mb=memory)).needs]
    )
    preflight = DockerProvider(runtime="gvisor").preflight(needs)
    if refused is not None:
        with pytest.raises(ProviderStartError, match=refused):
            await preflight
    else:
        await preflight
    assert ((_RUNSC, "flags") in ran) is (memory is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("log", "copied", "refused"),
    [
        ("<6>[   0.000000] Starting gVisor...\n", "contents", None),
        ("<6>[   0.000000] Linux version 7.0.0\n", "contents", "does not say 'Starting gVisor'"),
        # docker cp's archive names the file even when it lost what the Sandbox wrote.
        ("<6>[   0.000000] Starting gVisor...\n", "name", "is runsc-net-raw registered with"),
    ],
)
async def test_a_gvisor_sandbox_shows_it_runs_on_gvisor_and_writes_where_docker_sees(
    monkeypatch: pytest.MonkeyPatch, log: str, copied: str, refused: str | None
) -> None:
    """The check its preflight leaves for the Run's first Sandbox of it."""
    written: dict[str, bytes] = {}

    async def guest_exec(argv: list[str], **_: object) -> tuple[int, str, str, bool]:
        assert argv[:3] == ["/opt/ale/python/bin/python3", "-I", "-c"]
        return 0, log, "", False

    async def write_file(path: str, data: bytes, **_: object) -> None:
        written[path] = data

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        assert argv[0] == "cp" and argv[-1] == "-"
        container, _, path = argv[1].partition(":")
        assert container == "ale-e1-abcdef" and path.startswith("/opt/ale/preflight-")
        archive = path.rpartition("/")[2] + "\0" * 100
        return 0, archive + (written[path].decode() if copied == "contents" else ""), ""

    _preflight_host(monkeypatch, _known("runc", arguments=_REGISTERED), gpus=False)
    check = await DockerProvider(runtime="gvisor").preflight()
    assert check is not None
    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    sandbox = _sandbox()
    sandbox.allocation = sandbox.allocation.model_copy(update={"runtime": gvisor.NET_RAW})
    sandbox._client = SimpleNamespace(exec=guest_exec, write_file=write_file)  # type: ignore[assignment]
    if refused is None:
        await check(sandbox)
    else:
        with pytest.raises(ProviderStartError, match=refused):
            await check(sandbox)
    for path, contents in written.items():
        assert path.rpartition("/")[2] not in contents.decode()


@pytest.mark.asyncio
async def test_a_feature_its_runtime_does_not_give_is_refused_before_anything_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted(monkeypatch)
    nested = request(resources=Resources(container_features=(ContainerFeature.MOUNT,)))
    with pytest.raises(ProviderCapabilityError, match="gives no container features mount"):
        await DockerProvider().create(nested)
    # gVisor gives each under a registration of its own, and no profile both.
    both = (ContainerFeature.NESTED_CONTAINERS, ContainerFeature.RAW_SOCKETS)
    with pytest.raises(ProviderCapabilityError, match="gives no container features"):
        await DockerProvider(runtime="gvisor").create(
            request(resources=Resources(container_features=both))
        )
    # A commit would keep none of a container engine's storage.
    engine = request(
        resources=Resources(container_features=(ContainerFeature.NESTED_CONTAINERS,))
    ).model_copy(update={"snapshot": "ale-snapshot:x"})
    with pytest.raises(ProviderCapabilityError):
        await DockerProvider(runtime="gvisor").create(engine)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime", "features", "granted", "registered", "memory"),
    [
        ("runc", (), [], "runc", "512m"),
        ("runc", (ContainerFeature.RAW_SOCKETS,), [], "runc", "512m"),
        ("kata", (), [], kata.RUNTIME, "620m"),
        (
            "kata",
            (ContainerFeature.PERF, ContainerFeature.USER_NAMESPACES),
            [],
            kata.RUNTIME,
            "620m",
        ),
        ("kata", (ContainerFeature.MOUNT,), ["SYS_ADMIN"], kata.RUNTIME, "620m"),
        ("gvisor", (), [], "runsc", "620m"),
        ("gvisor", (ContainerFeature.USER_NAMESPACES, ContainerFeature.FUSE), [], "runsc", "620m"),
        ("gvisor", (ContainerFeature.MOUNT,), ["SYS_ADMIN"], "runsc", "620m"),
        ("gvisor", (ContainerFeature.RAW_SOCKETS,), [], "runsc-net-raw", "620m"),
        (
            "gvisor",
            (ContainerFeature.RAW_SOCKETS, ContainerFeature.MOUNT),
            ["SYS_ADMIN"],
            "runsc-net-raw",
            "620m",
        ),
        (
            "gvisor",
            (ContainerFeature.NESTED_CONTAINERS,),
            ["NET_ADMIN", "SYS_ADMIN", "SYS_RESOURCE"],
            "runsc-nested",
            "620m",
        ),
        (
            "gvisor",
            (ContainerFeature.NESTED_CONTAINERS, ContainerFeature.MOUNT),
            ["NET_ADMIN", "SYS_ADMIN", "SYS_RESOURCE"],
            "runsc-nested",
            "620m",
        ),
    ],
)
async def test_a_sandbox_is_granted_only_the_features_it_asked_for(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Literal["runc", "kata", "gvisor"],
    features: tuple[ContainerFeature, ...],
    granted: list[str],
    registered: str,
    memory: str,
) -> None:
    """The union of their grants, the whole Sandbox under the runtime one of them needs, its
    memory with what the runtime itself takes and no swap, its network mode recorded, and,
    under Kata or gVisor, a network of its own, which goes again when the Sandbox does not
    start. A container engine's storage is on volumes of its own."""
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:2] == ("run", "-d"):
            return 1, "", "stopped after the flags were read"
        return _scripted_reply(argv)

    image = ResolvedImage(
        kind="container",
        prepared_identity=DIGEST,
        observed_identity=DIGEST,
        observed_ref="image@" + DIGEST,
    )

    async def resolved(_prepared: PreparedTaskImage) -> ResolvedImage:
        return image

    async def no_problems(_reference: str) -> list[str]:
        return []

    async def framed(found: ResolvedImage, _request: SandboxRequest) -> ResolvedImage:
        return found

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    monkeypatch.setattr(
        "ale.run.providers.docker.provider.resolve_prepared_container_image", resolved
    )
    provider = DockerProvider(runtime=runtime)
    monkeypatch.setattr(provider, "check_image", no_problems)
    monkeypatch.setattr(provider, "_framed", framed)
    asked = request(
        network=NetworkPolicy(mode=NetworkMode.OPEN),
        resources=Resources(memory_mb=512, container_features=features),
    )
    with pytest.raises(ProviderStartError, match="stopped after the flags were read"):
        await provider.create(asked)
    (run,) = [call for call in calls if call[:2] == ("run", "-d")]
    assert [run[index + 1] for index, flag in enumerate(run) if flag == "--cap-add"] == granted
    engine = ContainerFeature.NESTED_CONTAINERS in features
    mounted = [run[index + 1] for index, flag in enumerate(run) if flag == "--mount"]
    assert [re.sub(r"src=[^,]+,", "", spec) for spec in mounted] == [
        f"type=volume,dst={path}" for path in sorted(gvisor.ENGINE_STORAGE) if engine
    ]
    # Named even for runc, so a Host's default runtime never stands in for the leaf's.
    pinned = [run[index + 1] for index, flag in enumerate(run) if flag == "--runtime"]
    assert pinned == [registered]
    assert run[run.index("--memory") + 1] == memory
    # No swap on any runtime: the Task's memory is the same whatever swap the Host has.
    assert run[run.index("--memory-swap") + 1] == memory
    # An application, not gVisor's Sentry, is what the OOM killer takes.
    assert ("--oom-score-adj" in run) is (runtime == "gvisor")
    created = _created(calls)
    host_ip = "172.18.0.1"
    if runtime == "runc":
        assert "--network" not in run and created == []
    else:
        (network,) = [run[index + 1] for index, flag in enumerate(run) if flag == "--network"]
        ((name, _, host_ip, internal),) = created
        assert network.startswith("ale-net-e1-") and (name, internal) == (network, False)
        assert calls[-1] == ("network", "rm", network)
    assert "ale.network=open" in run
    # The Host's address, the default bridge's read once or set on the sandbox's own
    # network, serves its confinement and its Gateway alias.
    gateway = [call for call in calls if call[:2] == ("network", "inspect") and "IPAM" in call[-1]]
    assert len(gateway) == (runtime == "runc")
    assert run[run.index("--add-host") + 1] == f"{HOST_ALIAS}:{host_ip}"


@pytest.mark.asyncio
async def test_a_gvisor_sandbox_gets_its_gpus_as_a_runc_one_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--gpus`` turns nvproxy on for that container alone; ``--nvproxy`` would for all."""
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:2] == ("run", "-d"):
            return 1, "", "stopped after the flags were read"
        if argv[0] == "info":
            return 0, "DAEMON-1\n", ""
        return _scripted_reply(argv)

    async def host_gpus(_provider: DockerProvider) -> dict[int, GpuDevice]:
        return {
            index: GpuDevice(
                id=f"GPU-{index}", model="NVIDIA H100 80GB HBM3", driver_version="570.1"
            )
            for index in (0, 1)
        }

    image = ResolvedImage(
        kind="container",
        prepared_identity=DIGEST,
        observed_identity=DIGEST,
        observed_ref="image@" + DIGEST,
    )

    async def resolved(_prepared: PreparedTaskImage) -> ResolvedImage:
        return image

    async def no_problems(_reference: str) -> list[str]:
        return []

    async def framed(found: ResolvedImage, _request: SandboxRequest) -> ResolvedImage:
        return found

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    monkeypatch.setattr(
        "ale.run.providers.docker.provider.resolve_prepared_container_image", resolved
    )
    monkeypatch.setattr(DockerProvider, "_host_gpus", host_gpus)
    provider = DockerProvider(runtime="gvisor", gpu_lock_dir=tmp_path)
    monkeypatch.setattr(provider, "check_image", no_problems)
    monkeypatch.setattr(provider, "_framed", framed)
    with pytest.raises(ProviderCapabilityError, match="stopped after the flags were read"):
        await provider.create(request(resources=Resources(gpus=2)))
    (run,) = [call for call in calls if call[:2] == ("run", "-d")]
    assert run[run.index("--gpus") + 1] == '"device=GPU-0,GPU-1"'
    assert "NVIDIA_DRIVER_CAPABILITIES=all" in run
    assert run[run.index("--runtime") + 1] == "runsc"
    assert not [flag for flag in run if "nvproxy" in flag]


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["runc", "gvisor"])
async def test_a_sandbox_whose_gpus_cannot_be_leased_leaves_no_network_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtime: str
) -> None:
    """A failed start does not keep the network it made, whose subnet it would hold."""
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[0] == "ps":
            return 0, "c0 ale-other GPU-0,GPU-1/\n", ""  # both held whole by another sandbox
        if argv[:3] == ("info", "--format", "{{.ID}}"):
            return 0, "DAEMON-1\n", ""
        return _scripted_reply(argv)

    async def host_gpus(_provider: DockerProvider) -> dict[int, GpuDevice]:
        return {
            index: GpuDevice(id=f"GPU-{index}", model="NVIDIA H100", driver_version="570.1")
            for index in (0, 1)
        }

    async def resolved(_prepared: PreparedTaskImage) -> ResolvedImage:
        return ResolvedImage(
            kind="container",
            prepared_identity=DIGEST,
            observed_identity=DIGEST,
            observed_ref="image@" + DIGEST,
        )

    async def no_problems(_reference: str) -> list[str]:
        return []

    async def framed(found: ResolvedImage, _request: SandboxRequest) -> ResolvedImage:
        return found

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    monkeypatch.setattr(
        "ale.run.providers.docker.provider.resolve_prepared_container_image", resolved
    )
    monkeypatch.setattr(DockerProvider, "_host_gpus", host_gpus)
    provider = DockerProvider(runtime=runtime, gpu_lock_dir=tmp_path)
    monkeypatch.setattr(provider, "check_image", no_problems)
    monkeypatch.setattr(provider, "_framed", framed)
    with pytest.raises(ProviderCapabilityError, match="GPUs"):
        await provider.create(request(resources=Resources(gpus=1)))
    created = [call[-1] for call in calls if call[:2] == ("network", "create")]
    removed = [call[-1] for call in calls if call[:2] == ("network", "rm")]
    assert created == removed
    assert not [call for call in calls if call[:2] == ("run", "-d")]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [_KataConfinement, _GvisorConfinement])
async def test_a_network_whose_bridges_mac_cannot_be_read_is_removed_again(
    monkeypatch: pytest.MonkeyPatch, kind: type[_KataConfinement] | type[_GvisorConfinement]
) -> None:
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:3] == ("run", "--rm", "--name"):
            return 1, "", "the daemon went away"
        return _scripted_reply(argv)

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    with pytest.raises(ProviderStartError, match="the daemon went away"):
        await kind.start("e1-abcdef", request(), _Networks())
    assert [name for name, *_ in _created(calls)] == ["ale-net-e1-abcdef"]
    assert calls[-1] == ("network", "rm", "ale-net-e1-abcdef")


@pytest.mark.asyncio
async def test_a_sandbox_started_under_another_runtime_is_refused_and_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:2] == ("network", "inspect"):
            return 0, "172.17.0.1\n", ""
        if argv[0] == "inspect":
            # What a daemon whose default runtime is gVisor's reports for an unpinned start.
            return 0, f"{DIGEST} runsc\n", ""
        return 0, "", ""

    image = ResolvedImage(
        kind="container",
        prepared_identity=DIGEST,
        observed_identity=DIGEST,
        observed_ref="image@" + DIGEST,
    )

    async def resolved(_prepared: PreparedTaskImage) -> ResolvedImage:
        return image

    async def no_problems(_reference: str) -> list[str]:
        return []

    async def framed(found: ResolvedImage, _request: SandboxRequest) -> ResolvedImage:
        return found

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    monkeypatch.setattr(
        "ale.run.providers.docker.provider.resolve_prepared_container_image", resolved
    )
    provider = DockerProvider()
    monkeypatch.setattr(provider, "check_image", no_problems)
    monkeypatch.setattr(provider, "_framed", framed)
    with pytest.raises(ProviderStartError, match="under runtime 'runsc', not 'runc'"):
        await provider.create(request(network=NetworkPolicy(mode=NetworkMode.OPEN)))
    (run,) = [call for call in calls if call[0] == "run"]
    container = run[run.index("--name") + 1]
    assert ("rm", "-f", "-v", container) in calls


def test_a_kata_vm_holds_the_declared_memory_and_its_guests_own() -> None:
    assert kata.memory_mb(512) == 620
    assert kata.memory_mb(8192) == 8492


@pytest.mark.asyncio
async def test_a_kata_sandbox_that_stopped_ends_its_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A microVM killed from outside, as by the Host's OOM killer, stops its container while
    ``docker exec`` runs on: the sessions end once ``docker wait`` returns, and say why."""

    async def docker(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
        if argv[0] == "wait":
            return 0, "255\n", ""
        assert argv == ("inspect", "--format", "{{json .State}}", "ale-e1")
        return 0, json.dumps({"Running": False, "ExitCode": 255}), ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    session = _Session(["sh", "-c", "echo {}; exec sleep 60"], "ale-e1", 1024)
    await session.start()
    assert await session.recv() == "{}"
    pending = asyncio.ensure_future(session.recv())
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.container = "ale-e1"
    sandbox._sessions = (session, session)
    await asyncio.wait_for(sandbox._end_sessions(), timeout=10)
    with pytest.raises(GuestUnreachableError) as ended:
        await asyncio.wait_for(pending, timeout=10)
    explained = await session.explain(ended.value)
    assert str(explained).startswith(
        "the sandbox itself died: its main process exited with code 255"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "refused"),
    [
        ('[["Backing Filesystem","extfs"]]', True),
        ('[["driver-type","io.containerd.snapshotter.v1"]]', False),
    ],
)
async def test_a_sandbox_that_would_save_past_the_layer_stores_limit_is_refused_up_front(
    monkeypatch: pytest.MonkeyPatch, status: str, refused: bool
) -> None:
    """Each commit stacks one more layer: Docker's layer store chains 125 at most, under any
    graph driver, so a Sandbox whose image and planned saves exceed that never starts; the
    containerd store's snapshotter stacks far more."""
    layers = "120\n"

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        if argv[:3] == ("info", "--format", "{{json .DriverStatus}}"):
            return 0, f"{status}\n", ""
        if argv[:2] == ("image", "inspect"):
            return 0, layers, ""
        raise AssertionError(argv)

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    provider = DockerProvider()
    if refused:
        with pytest.raises(ProviderCapabilityError, match="more than the 125"):
            await provider._refuse_deep_stacks("image@sha256:abc", 6)
    else:
        await provider._refuse_deep_stacks("image@sha256:abc", 6)
    await provider._refuse_deep_stacks("image@sha256:abc", 5)
    layers = "<no value>\n"
    if refused:
        with pytest.raises(ProviderStartError, match="could not count the layers"):
            await provider._refuse_deep_stacks("image@sha256:abc", 1)
