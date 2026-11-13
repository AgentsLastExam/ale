"""The container backend's Gateway routing and snapshots, against a scripted docker CLI."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    Identity,
    PreparedTaskImage,
    ResolvedImage,
    ResourceAllocation,
    SandboxRequest,
)
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.run.providers.docker import (
    GATEWAY_CHAIN,
    HOST_ALIAS,
    KATA_RUNTIME,
    KATA_TAP,
    KATA_VETH,
    DockerProvider,
    DockerSandbox,
    _kata_memory_mb,
    _proxy_env,
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


def test_an_allowlist_container_gets_the_proxy_token_as_its_password() -> None:
    """Python's urllib sends no proxy credentials without a password."""
    env = _proxy_env(
        request(
            network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)),
            proxy_url="http://0.0.0.0:9443",
            proxy_token="episode-token",
        )
    )
    assert env["HTTPS_PROXY"] == f"http://ale:episode-token@{HOST_ALIAS}:9443"


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

    monkeypatch.setattr("ale.run.providers.docker._docker", docker)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("network", ["ale-net-e1", None])
async def test_the_fixed_port_is_routed_to_the_gateway_and_an_isolated_sandbox_confined(
    monkeypatch: pytest.MonkeyPatch, network: str | None
) -> None:
    calls = _scripted(monkeypatch)
    await DockerProvider()._wire_host("ale-e1", network, request())
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
    await DockerProvider()._wire_host("ale-e1", "ale-net-e1", later, reattached=True)
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
    await DockerProvider()._wire_host("ale-e1", None, request(gateway_url=None))
    assert calls == []


def _sandbox() -> DockerSandbox:
    async def close() -> None:
        pass

    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.sandbox_id = "e1-abcdef"
    sandbox.container = "ale-e1-abcdef"
    sandbox.request = request(retention="snapshot")
    sandbox._client = SimpleNamespace(close=close)  # type: ignore[assignment]
    sandbox._file_client = SimpleNamespace(close=close)  # type: ignore[assignment]
    return sandbox


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

    monkeypatch.setattr("ale.run.providers.docker._docker", docker)
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

    monkeypatch.setattr("ale.run.providers.docker._docker", docker)
    provider = DockerProvider()
    assert [record["handle"] for record in await provider.list_retained()] == [
        "docker:snapshot:e1-abcdef"
    ]
    with pytest.raises(ProviderStartError, match="being used"):
        await provider.destroy_retained("docker:snapshot:e1-abcdef")
    await provider.destroy_retained("docker:snapshot:e1-abcdef")
    assert removed == ["ale-snapshot:e1-abcdef"] * 2


def test_a_provider_without_snapshots_refuses_them_before_anything_starts() -> None:
    from ale.run.providers.daytona import DaytonaProvider

    with pytest.raises(ProviderCapabilityError, match="does not save or boot snapshots"):
        DaytonaProvider().accepts(request(retention="snapshot", network=NetworkPolicy()))
    with pytest.raises(ProviderCapabilityError, match="does not save or boot snapshots"):
        DaytonaProvider().accepts(request(snapshot="docker:snapshot:e1", network=NetworkPolicy()))
    DockerProvider().accepts(request(retention="snapshot"))


def _helper_commands(call: tuple[str, ...]) -> list[str]:
    assert call[:5] == ("run", "--rm", "--network", "container:ale-e1", "--cap-add")
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
    host = await DockerProvider(runtime="kata")._wire_kata("ale-e1", proxied)
    assert host == "172.18.0.1"
    (inspect, helper) = calls
    assert inspect[-3] == "bridge"
    filters = _helper_commands(helper)
    tap, veth = KATA_TAP, KATA_VETH
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
    await DockerProvider(runtime="kata")._wire_kata(
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
    await DockerProvider(runtime="kata")._wire_kata("ale-e1", later, reattached=True)
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
    sandbox.kata_host_ip = "172.17.0.1"
    sandbox._sealed = False
    sandbox._client = SimpleNamespace(exec=guest_exec)  # type: ignore[assignment]
    await sandbox.open_egress()
    opened = _helper_commands(calls[-1])
    assert not [item for item in opened if " add " in item]
    assert opened[-1] == f'test -z "$(tc filter show dev {KATA_VETH} ingress pref 22)"'
    order.append("helper")
    await sandbox.close_egress()
    sealed = _helper_commands(calls[-1])
    assert sealed[-1].endswith("pref 22 protocol all matchall action drop")
    assert sandbox._sealed
    assert order == ["route add", "helper", "route del"]
    assert not [call for call in calls if call[0] == "network"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "kata", "networks", "drop", "sealed"),
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
    kata: bool,
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
        "HostConfig": {"Runtime": KATA_RUNTIME if kata else "runc"},
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

    async def connect(_provider: DockerProvider, _container: str, _user: str) -> object:
        return SimpleNamespace(exec=guest_exec, close=close)

    async def contract(_provider: DockerProvider, _client: object) -> tuple[str, bool]:
        return "/home/user", False

    monkeypatch.setattr("ale.run.providers.docker._docker", docker)
    monkeypatch.setattr(DockerProvider, "_connect", connect)
    monkeypatch.setattr(DockerProvider, "_guest_contract", contract)
    network = (
        NetworkPolicy(mode=mode, allowed_hosts=("a.b",))
        if mode is NetworkMode.ALLOWLIST
        else NetworkPolicy(mode=mode)
    )
    wanted = request(network=network, proxy_url="http://0.0.0.0:9443")
    allocation = ResourceAllocation(
        cpus=1, memory_mb=1024, sudo=False, network_mode=mode, provider="docker"
    )
    image = ResolvedImage(
        kind="container", prepared_identity=DIGEST, observed_identity=DIGEST, observed_ref=DIGEST
    )
    sandbox = await DockerProvider().reattach("docker:ale-e1", wanted, allocation, image)
    await sandbox.exec(["true"], identity=Identity.AGENT)
    assert (seen[-1] is not None) is sealed
    read = [call[-1] for call in calls if call[-1].startswith("tc filter show")]
    isolated = mode is not NetworkMode.OPEN
    assert read == ([f"tc filter show dev {KATA_TAP} ingress pref 21"] if kata and isolated else [])


@pytest.mark.asyncio
async def test_kata_refuses_gpus_before_anything_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted(monkeypatch)
    with pytest.raises(ProviderCapabilityError, match="Kata"):
        await DockerProvider(runtime="kata").create(request(resources=Resources(gpus=1)))
    assert calls == []


def test_a_kata_vm_holds_the_declared_memory_and_its_guests_own() -> None:
    assert _kata_memory_mb(512) == 620
    assert _kata_memory_mb(8192) == 8492
