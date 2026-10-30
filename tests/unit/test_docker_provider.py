"""The container backend's Gateway routing and snapshots, against a scripted docker CLI."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    PreparedTaskImage,
    SandboxRequest,
)
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.run.providers.docker import (
    GATEWAY_CHAIN,
    HOST_ALIAS,
    DockerProvider,
    DockerSandbox,
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
