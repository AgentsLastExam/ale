"""The container backend's Gateway routing and snapshots, against a scripted docker CLI."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, get_args

import pytest

import ale.run
from ale.core.config import ProviderId
from ale.core.errors import ProviderCapabilityError, ProviderStartError
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
)
from ale.core.taskspec import ContainerFeature, NetworkMode, NetworkPolicy, Resources
from ale.run.providers.docker import kata
from ale.run.providers.docker.provider import (
    GATEWAY_CHAIN,
    HOST_ALIAS,
    RUNTIMES,
    DockerProvider,
    DockerSandbox,
    _bridge_host_ip,
    _KataConfinement,
    _RuncConfinement,
    _sandbox_gateway,
)

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


def _scripted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:2] == ("network", "inspect"):
            return 0, "172.18.0.1\n", ""
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("network", ["ale-net-e1", None])
async def test_the_fixed_port_is_routed_to_the_gateway_and_an_isolated_sandbox_confined(
    monkeypatch: pytest.MonkeyPatch, network: str | None
) -> None:
    calls = _scripted(monkeypatch)
    await _RuncConfinement(network).wire("ale-e1", request())
    (inspect, helper) = calls
    assert inspect[-3] == (network or "bridge")
    rules = helper[-1].split(" && ")
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
async def test_a_reattaching_run_replaces_the_gateway_route_and_adds_its_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted(monkeypatch)
    later = request(gateway_url="http://0.0.0.0:42000")
    await _RuncConfinement("ale-net-e1").wire("ale-e1", later, reattached=True)
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
    await _RuncConfinement(None).wire("ale-e1", request(gateway_url=None))
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


@pytest.mark.asyncio
async def test_a_kata_sandbox_is_confined_by_tc_filters_outside_its_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kata passes frames between the VM and its veth with tc, past netfilter, so the
    Gateway port is rewritten both ways and an isolated sandbox starts sealed in tc."""
    calls = _scripted(monkeypatch)
    proxied = request(network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("a.b",)))
    proxied = proxied.model_copy(update={"proxy_url": "http://172.18.0.1:9443"})
    host = await _bridge_host_ip(None)
    await _KataConfinement(host).wire("ale-e1", proxied)
    assert host == "172.18.0.1"
    (inspect, helper) = calls
    assert inspect[-3] == "bridge"
    filters = _helper_commands(helper)
    tap, veth = kata.TAP, kata.VETH
    assert (
        f"tc filter add dev {tap} ingress pref 10 protocol ip flower dst_ip {host} ip_proto tcp "
        f"dst_port {SANDBOX_GATEWAY_PORT} action pedit ex munge tcp dport set 41000 pipe "
        f"action csum tcp pipe action mirred egress redirect dev {veth}"
    ) in filters
    assert (
        f"tc filter add dev {veth} ingress pref 10 protocol ip flower src_ip {host} ip_proto tcp "
        f"src_port 41000 action pedit ex munge tcp sport set {SANDBOX_GATEWAY_PORT} pipe "
        f"action csum tcp pipe action mirred egress redirect dev {tap}"
    ) in filters
    assert any("pref 11" in item and "dst_port 9443" in item for item in filters)
    # The Host's other ports and everything else are dropped, the seal before the Gateway.
    host_drop = filters.index(
        f"tc filter add dev {tap} ingress pref 12 protocol ip flower dst_ip {host} action drop"
    )
    seal = filters.index(
        f"tc filter add dev {tap} ingress pref 21 protocol all matchall action drop"
    )
    assert host_drop < seal < next(i for i, item in enumerate(filters) if "pref 10" in item)
    assert f"tc filter add dev {veth} ingress pref 22 protocol all matchall action drop" in filters


@pytest.mark.asyncio
async def test_an_open_kata_sandbox_gets_only_the_gateway_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted(monkeypatch)
    await _KataConfinement("172.18.0.1").wire(
        "ale-e1", request(network=NetworkPolicy(mode=NetworkMode.OPEN))
    )
    filters = _helper_commands(calls[-1])
    assert [item for item in filters if " add " in item] == [
        item for item in filters if " add " in item and "pref 10" in item
    ]
    assert len([item for item in filters if " add " in item]) == 2


@pytest.mark.asyncio
async def test_a_reattaching_run_replaces_only_the_kata_gateway_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _scripted(monkeypatch)
    later = request(gateway_url="http://0.0.0.0:42000")
    await _KataConfinement("172.18.0.1").wire("ale-e1", later, reattached=True)
    filters = _helper_commands(calls[-1])
    added = [item for item in filters if " add " in item]
    assert all("pref 10" in item for item in added) and len(added) == 2
    assert any("dport set 42000" in item for item in added)
    removed = [item for item in filters if " del " in item]
    assert {item.split(" pref ")[1].split()[0] for item in removed} == {"10", "11"}


@pytest.mark.asyncio
async def test_a_kata_sandbox_opens_and_seals_egress_with_its_tc_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filters seal it; the guest's default route goes too, so what they drop fails at
    once rather than after a timeout, and comes back only once the filters are gone."""
    calls = _scripted(monkeypatch)
    order: list[str] = []

    async def guest_exec(argv: list[str], **_: object) -> tuple[int, str, str, bool]:
        order.append(f"route {argv[-2]}")
        return 0, "", "", False

    sandbox = _sandbox()
    sandbox.container = "ale-e1"
    sandbox.confinement = _KataConfinement("172.17.0.1")
    sandbox._sealed = False
    sandbox._client = SimpleNamespace(exec=guest_exec)  # type: ignore[assignment]
    await sandbox.open_egress()
    opened = _helper_commands(calls[-1])
    assert not [item for item in opened if " add " in item]
    assert opened[-1] == f'test -z "$(tc filter show dev {kata.VETH} ingress pref 22)"'
    order.append("helper")
    await sandbox.close_egress()
    sealed = _helper_commands(calls[-1])
    assert sealed[-1].endswith("pref 22 protocol all matchall action drop")
    assert sandbox._sealed
    assert order == ["route add", "helper", "route del"]
    assert not [call for call in calls if call[0] == "network"]


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
    sandbox.confinement = _RuncConfinement("ale-net-e1")
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
    ("mode", "in_kata", "networks", "drop", "sealed"),
    [
        (NetworkMode.ALLOWLIST, False, ("ale-net-e1",), False, True),
        (NetworkMode.ALLOWLIST, False, ("ale-net-e1", "bridge"), False, False),
        (NetworkMode.OPEN, False, ("bridge",), False, False),
        (NetworkMode.ALLOWLIST, True, ("bridge",), True, True),
        (NetworkMode.ALLOWLIST, True, ("bridge",), False, False),
        (NetworkMode.OPEN, True, ("bridge",), True, False),
    ],
)
async def test_a_reattached_sandbox_is_sealed_as_its_container_was_left(
    monkeypatch: pytest.MonkeyPatch,
    mode: NetworkMode,
    in_kata: bool,
    networks: tuple[str, ...],
    drop: bool,
    sealed: bool,
) -> None:
    """An allowlist agent's commands get the proxy only while the Sandbox is sealed, which
    the container says, not the process driving it again: off the default bridge under runc,
    with the seal's drop filter on the VM's tap under Kata."""
    record = {
        "Name": "/ale-e1",
        "Image": DIGEST,
        "Config": {"Labels": {"ale.managed": "true", "ale.role": "solver"}},
        "State": {"Running": True},
        "HostConfig": {"Runtime": kata.RUNTIME if in_kata else "runc"},
        "NetworkSettings": {"Networks": {name: {} for name in networks}},
    }
    dropping = (  # as the helper image's tc (iproute2 6.15) shows it
        "filter protocol all matchall chain 0 \n"
        "filter protocol all matchall chain 0 handle 0x1 \n"
        "  not_in_hw\n"
        "\taction order 1: gact action drop\n"
    )
    calls: list[tuple[str, ...]] = []
    seen: list[dict[str, str] | None] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[0] == "inspect":
            return 0, json.dumps([record]), ""
        if argv[:2] == ("network", "inspect"):
            return 0, "172.17.0.1\n", ""
        if argv[-1].startswith("tc filter show"):
            return 0, dropping if drop else "", ""
        return 0, "", ""

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
    read = [call[-1] for call in calls if call[-1].startswith("tc filter show")]
    isolated = mode is not NetworkMode.OPEN
    assert read == (
        [f"tc filter show dev {kata.TAP} ingress pref 21"] if in_kata and isolated else []
    )


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
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:2] == ("network", "inspect"):
            return 0, "172.17.0.1\n", ""
        if argv[0] == "run":
            return 1, "", "stopped after the flags were read"
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
    provider = DockerProvider(runtime=runtime)
    monkeypatch.setattr(provider, "check_image", no_problems)
    monkeypatch.setattr(provider, "_framed", framed)
    asked = request(
        network=NetworkPolicy(mode=NetworkMode.OPEN),
        resources=Resources(container_features=features),
    )
    with pytest.raises(ProviderStartError, match="stopped after the flags were read"):
        await provider.create(asked)
    (run,) = [call for call in calls if call[0] == "run"]
    assert [run[index + 1] for index, flag in enumerate(run) if flag == "--cap-add"] == granted
    # Named even for runc, so a Host's default runtime never stands in for the leaf's.
    pinned = [run[index + 1] for index, flag in enumerate(run) if flag == "--runtime"]
    assert pinned == ["runc" if runtime == "runc" else kata.RUNTIME]


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
