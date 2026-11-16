"""The container backend's Gateway routing and snapshots, against a scripted docker CLI."""

from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, get_args

import pytest

import ale.run
import ale.run.providers.docker.provider as docker_provider
from ale.core.config import ProviderId
from ale.core.errors import GuestUnreachableError, ProviderCapabilityError, ProviderStartError
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
from ale.run import guestd
from ale.run.providers.docker import kata, tc
from ale.run.providers.docker.provider import (
    GATEWAY_CHAIN,
    HOST_ALIAS,
    RUNTIMES,
    DockerProvider,
    DockerSandbox,
    _DefaultBridge,
    _KataConfinement,
    _RuncConfinement,
    _sandbox_gateway,
    _Session,
)
from ale.run.transport import GuestClient

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "0" * 64


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


#: A sandbox's addresses on its own network, ``ale-net-e1``, whose bridge holds the Host at
#: 172.18.0.1, as a scripted docker CLI reports them.
SANDBOX_IP, SANDBOX_MAC, BRIDGE_MAC = "172.18.0.5", "02:42:ac:12:00:05", "02:42:aa:bb:cc:01"
PEERS = tc.Peers(host_ip="172.18.0.1", host_mac=BRIDGE_MAC, ip=SANDBOX_IP, mac=SANDBOX_MAC)
RESOLV_CONF = "nameserver 192.0.2.53\n"


def _scripted_reply(argv: tuple[str, ...]) -> tuple[int, str, str]:
    """What the scripted docker CLI answers about networks, addresses and resolv.conf."""
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
async def test_a_runc_sandbox_reads_the_hosts_address_on_its_own_network(
    monkeypatch: pytest.MonkeyPatch, mode: NetworkMode
) -> None:
    calls = _scripted(monkeypatch)
    started = await _RuncConfinement.start(
        "e1-abcdef", request(network=NetworkPolicy(mode=mode)), _DefaultBridge()
    )
    assert started.host_ip == "172.18.0.1"
    inspected = [call[2] for call in calls if call[:2] == ("network", "inspect")]
    assert inspected == (["ale-net-e1-abcdef"] if mode is NetworkMode.BLOCK else ["bridge"])


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
    sandbox._relay = None
    return sandbox


@pytest.mark.asyncio
async def test_a_sealed_snapshot_gives_the_account_its_settings_back_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The committed image outlives the relay its system proxy names."""
    calls = _scripted(monkeypatch)

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
    calls = _scripted(monkeypatch)
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
            f"LABEL ale.prepared={DIGEST}",
            "ale-e1-abcdef",
            "ale-snapshot:e1-abcdef",
        ),
    ]
    assert retained.handle == "docker:snapshot:e1-abcdef"
    assert retained.gpu_devices == ()


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
    image = await provider._snapshot_image("docker:snapshot:e1-abcdef", request())
    assert (image.observed_identity, image.observed_ref) == ("sha256:" + "5" * 64,) * 2
    assert image.prepared_identity == DIGEST

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


def _on_own_bridge(kind: type[_KataConfinement]) -> _KataConfinement:
    return kind(_DefaultBridge(), "ale-net-e1", "172.18.0.1", BRIDGE_MAC)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [NetworkMode.OPEN, NetworkMode.BLOCK])
async def test_a_kata_sandbox_boots_on_a_network_of_its_own(
    monkeypatch: pytest.MonkeyPatch, mode: NetworkMode
) -> None:
    """No other container shares its segment. The network is not internal, whatever the
    Task's mode: the runtime never sees a network attached later, so egress opens through
    this one's NAT. The Host is its gateway, whose MAC a helper on the network reads before
    the sandbox starts, the default bridge's resolv.conf replaces Docker's embedded DNS
    server, out of the sandbox's reach, once the sandbox is sealed, and destroying the
    sandbox removes the network."""
    calls = _scripted(monkeypatch)
    wanted = request(network=NetworkPolicy(mode=mode))
    confinement = await _KataConfinement.start("e1-abcdef", wanted, _DefaultBridge())
    network = "ale-net-e1-abcdef"
    assert calls[:2] == [
        ("network", "create", network),
        ("network", "inspect", network, "--format", "{{(index .IPAM.Config 0).Gateway}}"),
    ]
    # On the network itself, removed by name before the sandbox starts there.
    (helper,) = [call for call in calls if call[0] == "run"]
    name = helper[helper.index("--name") + 1]
    assert helper[helper.index("--network") + 1] == network and helper[-1] == "172.18.0.1"
    assert calls[-1] == ("rm", "-f", name)
    assert confinement.host_ip == "172.18.0.1" and confinement.host_mac == BRIDGE_MAC
    assert confinement.run_args(mode is not NetworkMode.OPEN)[:4] == [
        "--network", network, "--label", f"ale.bridge-mac={BRIDGE_MAC}"
    ]  # fmt: skip
    calls.clear()
    await confinement.wire("ale-e1", wanted)
    assert await confinement.peers("ale-e1") == PEERS
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
    sandbox._resource_lease = None
    calls.clear()
    await sandbox.destroy()
    assert calls == [("rm", "-f", "-v", "ale-e1-abcdef"), ("network", "rm", network)]


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
        await _KataConfinement.found(("ale-net-e1",), _DefaultBridge(), {})


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


def test_the_seal_probe_reads_the_drop_the_seal_puts_in_place() -> None:
    first_drop = next(item for item in kata.seal(PEERS) if item.endswith(tc.DROP_ALL))
    words = first_drop.split()
    device, hook, pref = words[words.index("dev") + 1], words[5], words[words.index("pref") + 1]
    assert f"tc filter show dev {device} {hook} pref {pref}" == kata.SEAL_PROBE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("waited", "state", "died"),
    [
        (0, {"Running": False, "OOMKilled": True, "ExitCode": 137}, "exceeded its memory limit"),
        (0, {"Running": False, "OOMKilled": False, "ExitCode": 137}, "killed from outside"),
        (0, {"Running": False, "OOMKilled": False, "ExitCode": 3}, "exited with code 3"),
        # Docker keeps OOMKilled once the OOM killer took any process; the main process
        # exited later of its own.
        (
            0,
            {"Running": False, "OOMKilled": True, "ExitCode": 3},
            "exited with code 3; the OOM killer had taken a process of it earlier",
        ),
        # The OOM killer took a process and the container lives on: no death.
        (0, {"Running": True, "OOMKilled": True, "ExitCode": 0}, None),
        (None, None, None),  # still running when the wait gave up
        (1, None, None),  # docker wait failed: the container is gone, ALE's to explain
    ],
)
async def test_a_sandbox_that_died_says_why_when_its_session_ends(
    monkeypatch: pytest.MonkeyPatch,
    waited: int | None,
    state: dict[str, object] | None,
    died: str | None,
) -> None:
    """One wait, which returns at once for a stopped container, then its state; no polling.
    It runs when the stream of a session that answered ends, as the client asks the session
    to explain the end: a real process answers and ends it here."""
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
    session = _Session(["sh", "-c", "echo {}; sleep 0.2; echo urpc EOF >&2"], "ale-e1")
    await session.start()
    assert await session.recv() == "{}"
    with pytest.raises(GuestUnreachableError) as ended:
        await session.recv()
    assert calls == []
    message = str(await session.explain(ended.value))
    assert ("the sandbox itself died" in message) is (died is not None)
    assert died is None or died in message
    assert "guest session closed: urpc EOF" in message
    assert [call[0] for call in calls] == (["wait", "inspect"] if waited == 0 else ["wait"])
    assert deadlines == [30.0]


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
    session = _Session(["sh", "-c", guestd], "ale-e1")
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
        def __init__(self, _argv: list[str], container: str) -> None:
            super().__init__(["sh", "-c", "echo 'guestd: bad interpreter' >&2; exit 1"], container)

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    monkeypatch.setattr("ale.run.providers.docker.provider._Session", Failing)
    started = time.monotonic()
    with pytest.raises(ProviderStartError) as failed:
        await DockerProvider()._connect("ale-e1", "user")
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
    unstarted = _Session(["cat"], "ale-e1")
    with pytest.raises(GuestUnreachableError, match="transport is not started") as raised:
        await unstarted.recv()
    assert await unstarted.explain(raised.value) is raised.value
    session = _Session(["sh", "-c", "echo {}; cat"], "ale-e1")
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
    client = await DockerProvider()._connect("ale-e1", "user")
    transport = client._transport
    assert isinstance(transport, _Session) and transport.container == "ale-e1"
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
    with the seal's drop filter on the VM's tap under Kata. Its own network mode, which it
    was started with, decides how it is confined, whatever the reattaching run asks."""
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

    async def connect(_provider: DockerProvider, _container: str, _user: str) -> object:
        return SimpleNamespace(exec=guest_exec, close=close, egress_relay=egress_relay)

    async def contract(_provider: DockerProvider, _client: object) -> tuple[str, bool]:
        return "/home/user", False

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    monkeypatch.setattr(DockerProvider, "_connect", connect)
    monkeypatch.setattr(DockerProvider, "_guest_contract", contract)
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
    assert read == ([kata.SEAL_PROBE] if isolated and runtime == kata.RUNTIME else [])


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
    assert (runc.provider, in_kata.provider) == ("docker:runc", "docker:kata")
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
        models = {0: a100, 1: "NVIDIA H100 80GB HBM3", 2: a100}
        return {
            index: GpuDevice(id=f"GPU-{index}", model=model, driver_version="570.1")
            for index, model in models.items()
        }

    (none,) = await DockerProvider().profiles()
    assert (none.gpus, none.gpu_models) == (False, ())
    monkeypatch.setattr(DockerProvider, "_host_gpus", host_gpus)
    every = DockerProvider()
    (runc,) = await every.profiles()
    assert (runc.gpus, runc.gpu_models) == (True, (a100, "NVIDIA H100 80GB HBM3"))
    await every.profiles()
    assert found == [1]  # read once
    (configured,) = await DockerProvider(gpus=(0, 2)).profiles()
    assert configured.gpu_models == (a100,)
    h100 = Resources(gpus=1, gpu_types=("H100",))
    assert await DockerProvider(gpus=(0, 2)).unserved(
        request(resources=h100).needs, user="user", role=SandboxRole.SHARED
    ) == (f"attaches {a100}, none of gpu_types ['H100']",)
    # Docker's GPU injection does not reach a Kata microVM.
    (in_kata,) = await DockerProvider(runtime="kata").profiles()
    assert (in_kata.gpus, in_kata.gpu_models) == (False, ())


async def test_a_docker_leaf_on_an_arm64_machine_serves_no_linux_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ALE's Linux images and the Tasks built on them are x86-64, which Docker runs
    natively."""
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    assert await DockerProvider().unserved(
        request().needs, user="user", role=SandboxRole.SHARED
    ) == ("runs arm64, not x86_64",)


@pytest.mark.parametrize(
    ("runtime", "known", "shims", "devices", "missing"),
    [
        ("runc", "io.containerd.runc.v2 runc ", (), (), None),
        ("runc", "io.containerd.runc.v2 runsc ", (), (), "needs the runc runtime"),
        ("kata", "runc ", ("containerd-shim-kata-v2",), ("/dev/kvm",), None),
        ("kata", "runc ", (), ("/dev/kvm",), "needs the io.containerd.kata.v2 runtime"),
        ("kata", "runc ", ("containerd-shim-kata-v2",), (), "needs /dev/kvm"),
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

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        assert argv[:2] == ("info", "--format")
        return 0, known, ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    monkeypatch.setattr(
        "ale.run.providers.docker.provider.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "docker" or name in shims else None,
    )
    monkeypatch.setattr(
        "ale.run.providers.docker.provider.Path.exists", lambda path: str(path) in devices
    )
    preflight = DockerProvider(runtime=runtime).preflight()
    if missing is None:
        await preflight
    else:
        with pytest.raises(ProviderStartError, match=missing):
            await preflight


@pytest.mark.asyncio
async def test_a_feature_its_runtime_does_not_give_is_refused_before_anything_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted(monkeypatch)
    nested = request(resources=Resources(container_features=(ContainerFeature.MOUNT,)))
    with pytest.raises(ProviderCapabilityError, match="gives no container features mount"):
        await DockerProvider().create(nested)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime", "features", "granted"),
    [
        ("runc", (), []),
        ("runc", (ContainerFeature.RAW_SOCKETS,), []),
        ("kata", (), []),
        ("kata", (ContainerFeature.PERF, ContainerFeature.USER_NAMESPACES), []),
        ("kata", (ContainerFeature.MOUNT,), ["SYS_ADMIN"]),
    ],
)
async def test_a_sandbox_is_granted_only_the_features_it_asked_for(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Literal["runc", "kata"],
    features: tuple[ContainerFeature, ...],
    granted: list[str],
) -> None:
    """Its network mode recorded, and, under Kata, a network of its own, which goes again
    when the Sandbox does not start."""
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
        resources=Resources(container_features=features),
    )
    with pytest.raises(ProviderStartError, match="stopped after the flags were read"):
        await provider.create(asked)
    (run,) = [call for call in calls if call[:2] == ("run", "-d")]
    assert [run[index + 1] for index, flag in enumerate(run) if flag == "--cap-add"] == granted
    # Named even for runc, so a Host's default runtime never stands in for the leaf's.
    pinned = [run[index + 1] for index, flag in enumerate(run) if flag == "--runtime"]
    assert pinned == ["runc" if runtime == "runc" else kata.RUNTIME]
    created = [call[2:] for call in calls if call[:2] == ("network", "create")]
    if runtime == "runc":
        assert "--network" not in run and created == []
    else:
        (network,) = [run[index + 1] for index, flag in enumerate(run) if flag == "--network"]
        assert network.startswith("ale-net-e1-") and created == [(network,)]
        assert calls[-1] == ("network", "rm", network)
    assert "ale.network=open" in run
    # The Host's address is read once, for the sandbox's confinement and its Gateway alias.
    gateway = [call for call in calls if call[:2] == ("network", "inspect") and "IPAM" in call[-1]]
    assert len(gateway) == 1
    assert run[run.index("--add-host") + 1] == f"{HOST_ALIAS}:172.18.0.1"


@pytest.mark.asyncio
async def test_a_sandbox_whose_gpus_cannot_be_leased_leaves_no_network_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Docker's default address pools hold about 31 networks, so a failed start must not
    keep the one it made."""
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[0] == "ps":
            return 0, "GPU-0,GPU-1\n", ""  # both held by other sandboxes
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
    provider = DockerProvider(gpu_lock_dir=tmp_path)
    monkeypatch.setattr(provider, "check_image", no_problems)
    monkeypatch.setattr(provider, "_framed", framed)
    with pytest.raises(ProviderCapabilityError, match="GPUs"):
        await provider.create(request(resources=Resources(gpus=1)))
    created = [call[-1] for call in calls if call[:2] == ("network", "create")]
    removed = [call[-1] for call in calls if call[:2] == ("network", "rm")]
    assert created == removed
    assert not [call for call in calls if call[:2] == ("run", "-d")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "unread"),
    [
        (_RuncConfinement, "Gateway"),
        (_KataConfinement, "Gateway"),
        (_KataConfinement, "172.18.0.1"),
    ],
)
async def test_a_network_whose_facts_cannot_be_read_is_removed_again(
    monkeypatch: pytest.MonkeyPatch,
    kind: type[_RuncConfinement] | type[_KataConfinement],
    unread: str,
) -> None:
    """The Host's address on it, or its bridge's MAC."""
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if unread in argv[-1]:
            return 1, "", "the daemon went away"
        return _scripted_reply(argv)

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    with pytest.raises(ProviderStartError, match="the daemon went away"):
        await kind.start("e1-abcdef", request(), _DefaultBridge())
    assert calls[0][:2] == ("network", "create")
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
